"""规则分集规划器：不依赖任何 AI 接口，按约束生成合法方案。

设计依据：
- §5 可行性与末集处理；§5.3 剩余时长约束必须提前排除"前几集切上限、末集剩碎片"；
- §16 阶段1 要求"不依赖AI也能正确剪出指定区间"；
- §6.1 接口不可用时应能生成基于规则的草案，但必须标注尚未进行剧情审核。

这是阶段1/2 的规划器。阶段4 接入多模态剧情判断后，由候选图 + 动态规划替换，
本模块保留为兜底路径与对照基线（§20.3 的"规则基线"）。
"""

from __future__ import annotations

from fractions import Fraction

from .plan import BoundaryPlan, PlanProblem
from .probe import MediaInfo
from .settings import (
    CountPolicy,
    FeasibilityReport,
    SplitMode,
    SplitSettings,
    check_feasibility,
)

__all__ = ["PlanningError", "choose_episode_count", "plan_rule_based"]


class PlanningError(RuntimeError):
    """参数层面就无法得到方案（与"候选点不足"不同，见 §10.4）。"""


def choose_episode_count(settings: SplitSettings, report: FeasibilityReport) -> int:
    """确定集数。

    - 严格模式：必须恰好等于用户指定值，无解则报错，绝不用 N±1 冒充完成（§4.2）。
    - 弹性模式：在允许区间内取最接近名义目标的值。
    - 按目标时长 / 推荐：取使每集时长最接近目标值的集数。
    """
    if report.derived is None:
        raise PlanningError("缺少派生参数")

    low, high = report.allowed_min, report.allowed_max
    if low > high:
        raise PlanningError("可行集数范围为空")

    if settings.split_mode == SplitMode.TARGET_EPISODE_COUNT:
        nominal = settings.target_episode_count or 0
        if settings.count_policy == CountPolicy.EXACT:
            if not report.exact_count_ok:
                message = report.issues[0].message if report.issues else "严格集数无解"
                hint = report.issues[0].hint if report.issues else ""
                raise PlanningError(f"{message}{(' ' + hint) if hint else ''}")
            return nominal
        return max(low, min(high, nominal))

    # 按目标时长：可行范围内挑时长最接近目标的集数
    target = report.derived.target_duration
    total = report.total_seconds
    best = low
    best_gap: Fraction | None = None
    for count in range(low, high + 1):
        gap = abs(total / count - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = count, gap
    return best


def plan_rule_based(
    media: MediaInfo,
    settings: SplitSettings,
    *,
    version: int = 1,
) -> tuple[BoundaryPlan, FeasibilityReport, list[PlanProblem]]:
    """按规则生成分集方案。

    返回 (方案, 可行性报告, 方案层问题)。调用方必须先看可行性报告：
    参数层面无解时本函数抛出 PlanningError，不返回半成品。
    """
    total_seconds = media.timeline_duration
    if total_seconds <= 0:
        raise PlanningError("源片时长不可用，无法规划")

    report = check_feasibility(settings, total_seconds)
    if report.blocking:
        detail = "；".join(f"{i.message}{(' ' + i.hint) if i.hint else ''}" for i in report.issues)
        raise PlanningError(detail or "参数无解")

    derived = report.derived
    assert derived is not None

    episode_count = choose_episode_count(settings, report)

    # 起点用等分：在可行集数下，T/N 本身落在 [L, U] 内，因此等分是合法解；
    # 后续吸附到帧边界只会在一个帧时长内微调，不会越界。
    plan = BoundaryPlan.uniform(
        episode_count,
        media.video_time_base,
        media.duration_ticks(),
        version=version,
        source="auto",
        strategy=settings.strategy.value,
        semantic_review_status="rule_based_not_reviewed",
    )

    snap_issues = plan.snap_to_frames(media)
    if snap_issues:
        raise PlanningError("；".join(p.message for p in snap_issues))

    problems = plan.validate(settings, derived)
    return plan, report, problems


def estimate_rule_based_durations(
    total_seconds: Fraction,
    settings: SplitSettings,
) -> list[Fraction]:
    """在真正分析前估算各集时长（§4.3 分析前的时长建议）。

    只做算术，不判断剧情，因此调用方必须把它标注为"仅根据时长估算"。
    """
    report = check_feasibility(settings, total_seconds)
    if report.derived is None or report.blocking:
        return []
    count = choose_episode_count(settings, report)
    base = total_seconds / count
    return [base] * count
