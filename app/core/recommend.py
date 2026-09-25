"""推荐方案（阶段5）：在同一起点上比较多策略结果并给出推荐。

为什么单独成模块
----------------
三种策略（剧情/悬念/时长）与规则基线各有取舍，把"选哪个"这件事从
调用方挪到一个可测试的模块里，能保证比较口径一致：**全部用同一个代价
函数、同一套可行性约束**，只比总代价。

推荐规则（§20.3 的延伸）
------------------------
- 在**所有非阻断**的候选结果里选总代价最低者；
- 比较记录完整保留（每个策略的代价、集数、切点差异），推荐不隐藏落选者；
- 全部阻断时返回 None 并汇总各策略的失败成因（§10.4 诊断原样透出）；
- 推荐不声称"最优的剧情判断"——它只是同一代价函数下的数值最优（§11.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .candidates import CandidateSet
from .probe import MediaInfo
from .settings import DerivedParams
from .solver import SolverWeights, solve_from_candidates

__all__ = ["StrategyComparison", "Recommendation", "recommend_plan"]

DEFAULT_STRATEGIES = ("story", "suspense", "duration")


@dataclass
class StrategyComparison:
    """一个策略的求解结果摘要。"""

    name: str
    ok: bool = False
    total_cost: float = 0.0
    episode_count: int = 0
    cut_times: list[Fraction] = field(default_factory=list)
    plan: object | None = None          # BoundaryPlan
    solution: object | None = None      # GraphSolution
    problems: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.ok:
            failed = self.problems[0].message if self.problems else "无解"
            return f"{self.name}：失败（{failed}）"
        cuts = "、".join(f"{float(t):.2f}s" for t in self.cut_times)
        return (
            f"{self.name}：{self.episode_count} 集，代价 {self.total_cost:.4f}，"
            f"切点 {cuts}"
        )


@dataclass
class Recommendation:
    comparisons: list[StrategyComparison] = field(default_factory=list)
    chosen: StrategyComparison | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = ["策略比较："]
        for comparison in self.comparisons:
            lines.append(f"  {comparison.describe()}")
        if self.chosen is not None:
            lines.append(f"推荐：{self.chosen.name}（代价 {self.chosen.total_cost:.4f}）")
        else:
            lines.append("无可用方案：所有策略均无解，请查看各自成因")
        for note in self.notes:
            lines.append(f"  {note}")
        return "\n".join(lines)


def recommend_plan(
    candidates: CandidateSet,
    media: MediaInfo,
    derived: DerivedParams,
    *,
    settings_count_exact: bool,
    target_episode_count: int,
    allowed_min: int,
    allowed_max: int,
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
    ends_with_question: dict[int, bool] | None = None,
) -> Recommendation:
    """用同一口径跑多个策略并推荐代价最低者。

    每个策略独立求解、独立校验；阻断性问题不传染给其他策略。
    """
    recommendation = Recommendation()

    for strategy in strategies:
        comparison = StrategyComparison(name=strategy)
        try:
            plan, solution, problems, notes = solve_from_candidates(
                candidates,
                media,
                derived,
                settings_count_exact=settings_count_exact,
                target_episode_count=target_episode_count,
                allowed_min=allowed_min,
                allowed_max=allowed_max,
                weights=SolverWeights.for_strategy(strategy),
                strategy=strategy,
                ends_with_question=ends_with_question,
            )
        except Exception as exc:  # noqa: BLE001 - 单策略失败不应影响其他策略
            comparison.ok = False
            from .plan import PlanProblem

            comparison.problems = [
                PlanProblem("STRATEGY_FAILED", "block", f"{strategy} 求解失败：{exc}")
            ]
            recommendation.comparisons.append(comparison)
            continue

        comparison.notes = notes
        blocking = [p for p in problems if p.is_blocking]
        if blocking or solution.is_empty:
            comparison.ok = False
            comparison.problems = blocking
            recommendation.comparisons.append(comparison)
            continue

        comparison.ok = True
        comparison.total_cost = solution.total_cost
        comparison.episode_count = solution.episode_count
        comparison.cut_times = list(solution.cut_times)
        comparison.plan = plan
        comparison.solution = solution
        comparison.problems = problems
        recommendation.comparisons.append(comparison)

    usable = [c for c in recommendation.comparisons if c.ok]
    if usable:
        recommendation.chosen = min(usable, key=lambda c: c.total_cost)
        others = [c.name for c in usable if c is not recommendation.chosen]
        if others:
            recommendation.notes.append(
                f"落选：{'、'.join(others)}（比较口径：同一代价函数下的总代价）"
            )
    return recommendation
