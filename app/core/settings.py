"""分集参数规则与可行性检查。

设计依据：方案 §4（模式与参数规则）、§5（可行性检查与末集处理）、
§10.4（无解状态必须区分）、§11.2（评分归一化与除零处理）。

核心原则（§3）：
- 硬约束与优化偏好严格分开，硬约束不参加加分抵消。
- 内部全精度计算，仅在界面显示时四舍五入。
- 手动范围与百分比浮动互斥，配置中只保留一个当前有效来源。
- 无解时必须区分成因，不得把"当前搜索没找到"伪装成"数学上不存在"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction

from .timebase import format_timecode

__all__ = [
    "SplitMode",
    "CountPolicy",
    "RangeMode",
    "Strategy",
    "UnresolvedState",
    "RangeSpec",
    "SplitSettings",
    "DerivedParams",
    "FeasibilityIssue",
    "FeasibilityReport",
    "SettingAdvice",
    "applicable_setting_advice",
    "apply_setting_advice",
    "ceiling_div",
]


class SplitMode(str, Enum):
    """§4 三种分集模式。"""

    TARGET_DURATION = "target_duration"      # 模式A：按目标时长
    TARGET_EPISODE_COUNT = "target_episode_count"  # 模式B：按目标集数
    RECOMMEND = "recommend"                  # 模式C：推荐方案


class CountPolicy(str, Enum):
    """§4.2 模式B的集数策略。"""

    EXACT = "exact"        # 严格集数：必须恰好N集或明确无解
    FLEXIBLE = "flexible"  # 弹性集数：在用户允许区间内比较方案


class RangeMode(str, Enum):
    """§4.1 两种互斥的范围来源。"""

    MANUAL = "manual"    # 手动上下限
    PERCENT = "percent"  # 百分比浮动


class Strategy(str, Enum):
    """§11.1 三种策略，均受相同硬约束控制。"""

    STORY = "story"
    SUSPENSE = "suspense"
    DURATION = "duration"


class UnresolvedState(str, Enum):
    """§10.4 无解状态。四种成因必须分开报告。"""

    NONE = "none"
    MATH_INFEASIBLE = "math_infeasible"          # 参数数学无解
    NO_PATH_IN_GRAPH = "no_path_in_graph"        # 候选图无路径
    CONSTRAINT_CONFLICT = "constraint_conflict"  # 禁切/锁定冲突
    BUDGET_EXHAUSTED = "budget_exhausted"        # 分析预算用尽


def ceiling_div(numerator: Fraction, denominator: Fraction) -> int:
    """向上取整的除法，全精度实现（用于 §5.2 最少集数 = ceil(T/U)）。"""
    if denominator <= 0:
        raise ValueError("除数必须为正")
    quotient = Fraction(numerator) / Fraction(denominator)
    return -((-quotient.numerator) // quotient.denominator)


@dataclass
class RangeSpec:
    """集长范围来源。两种方式不可同时生效（§4.1）。"""

    mode: RangeMode = RangeMode.PERCENT
    manual_min: Fraction | None = None
    manual_max: Fraction | None = None
    tolerance: Fraction | None = Fraction(1, 5)  # p，默认 20%

    # 切换方式时保留旧输入供切回使用（§4.1），但不参与计算
    _stashed_manual: tuple[Fraction, Fraction] | None = None
    _stashed_tolerance: Fraction | None = None

    def stash(self) -> None:
        """切换到另一种方式前，保存当前输入。"""
        if self.mode == RangeMode.MANUAL:
            if self.manual_min is not None and self.manual_max is not None:
                self._stashed_manual = (self.manual_min, self.manual_max)
        else:
            self._stashed_tolerance = self.tolerance

    def switch_to(self, mode: RangeMode) -> None:
        """切换范围来源，并恢复该方式上次的输入。"""
        if mode == self.mode:
            return
        self.stash()
        self.mode = mode
        if mode == RangeMode.MANUAL and self._stashed_manual:
            self.manual_min, self.manual_max = self._stashed_manual
        if mode == RangeMode.PERCENT and self._stashed_tolerance is not None:
            self.tolerance = self._stashed_tolerance

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode == RangeMode.MANUAL:
            if self.manual_min is None or self.manual_max is None:
                problems.append("手动范围模式必须同时提供最短与最长集长。")
            else:
                if self.manual_min <= 0:
                    problems.append("最短集长必须大于 0。")
                if self.manual_max < self.manual_min:
                    problems.append("最长集长不能小于最短集长。")
        else:
            if self.tolerance is None:
                problems.append("百分比浮动模式必须提供浮动比例。")
            elif not (0 <= self.tolerance < 1):
                problems.append("浮动比例必须在 [0, 1) 区间内。")
        return problems

    def resolve(self, target_duration: Fraction) -> tuple[Fraction, Fraction]:
        """按目标时长解出 (L, U)。"""
        if self.mode == RangeMode.MANUAL:
            if self.manual_min is None or self.manual_max is None:
                raise ValueError("手动范围未设置")
            return self.manual_min, self.manual_max
        if self.tolerance is None:
            raise ValueError("浮动比例未设置")
        return (
            target_duration * (1 - self.tolerance),
            target_duration * (1 + self.tolerance),
        )

    def to_json(self) -> dict:
        if self.mode == RangeMode.MANUAL:
            return {
                "mode": RangeMode.MANUAL.value,
                "min_seconds": _frac_to_json(self.manual_min),
                "max_seconds": _frac_to_json(self.manual_max),
            }
        return {
            "mode": RangeMode.PERCENT.value,
            "tolerance": _frac_to_json(self.tolerance),
        }


@dataclass
class SplitSettings:
    """分集设置（方案 §18.1 的 settings 段）。"""

    split_mode: SplitMode = SplitMode.TARGET_DURATION
    count_policy: CountPolicy = CountPolicy.EXACT
    target_episode_count: int | None = None
    allowed_count_min: int | None = None   # 弹性模式可接受集数下限
    allowed_count_max: int | None = None   # 弹性模式可接受集数上限
    target_duration_seconds: Fraction | None = None  # 模式A的用户输入
    range: RangeSpec = field(default_factory=RangeSpec)
    strategy: Strategy = Strategy.STORY
    duration_policy: str = "hard"          # 集长上下限是否硬约束（默认硬）
    allow_episode_exceptions: bool = False  # §4.4 默认关闭

    # ---- 模式C 的判定结果（由 auto_timing 回填，只用于展示与快照）--------
    # 单独存而不是直接改 target_duration_seconds：用户当初**没有**填过这个值，
    # 把它写成"用户的输入"会让下次打开分集设置时误以为可以照着改。
    decided_episode_count: int | None = None
    decided_target_seconds: Fraction | None = None
    decided_strategy: str | None = None
    decided_beat_seconds: Fraction | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        problems.extend(self.range.validate())

        if self.split_mode == SplitMode.TARGET_DURATION:
            if self.target_duration_seconds is None:
                problems.append("按目标时长模式必须提供目标时长 D。")
            elif self.target_duration_seconds <= 0:
                problems.append("目标时长必须大于 0。")

        if self.split_mode == SplitMode.TARGET_EPISODE_COUNT:
            if self.target_episode_count is None:
                problems.append("按目标集数模式必须提供目标集数 N。")
            elif self.target_episode_count <= 0:
                problems.append("目标集数必须为正整数。")
            elif self.count_policy == CountPolicy.FLEXIBLE:
                if self.allowed_count_min is None or self.allowed_count_max is None:
                    problems.append("弹性集数模式必须提供可接受集数区间。")
                else:
                    if self.allowed_count_min <= 0:
                        problems.append("可接受集数下限必须为正整数。")
                    if self.allowed_count_max < self.allowed_count_min:
                        problems.append("可接受集数上限不能小于下限。")
                    if self.target_episode_count is not None and not (
                        self.allowed_count_min <= self.target_episode_count <= self.allowed_count_max
                    ):
                        problems.append("目标集数必须落在可接受集数区间内。")
        return problems

    def derive(self, total_seconds: Fraction) -> "DerivedParams":
        """解算目标时长与范围（§4.1、§4.2）。

        §4.2 特别规定：弹性模式的名义目标 D = T/N 由用户的目标 N 计算，
        范围 L/U 在本次求解中保持不变，不随算法尝试不同集数而漂移。
        """
        if total_seconds <= 0:
            raise ValueError("总时长必须大于 0")

        if self.split_mode == SplitMode.TARGET_DURATION:
            if self.target_duration_seconds is None:
                raise ValueError("目标时长未设置")
            target = self.target_duration_seconds
        elif self.split_mode == SplitMode.TARGET_EPISODE_COUNT:
            if not self.target_episode_count:
                raise ValueError("目标集数未设置")
            target = Fraction(total_seconds, self.target_episode_count)
        else:
            # 模式C：D 通常是空的（要等分析后自动判定）。走到这里说明调用方
            # 在自动判定之前就来要派生参数了——那是个真错误，不该悄悄给个默认值。
            if self.target_duration_seconds is None:
                raise ValueError(
                    "推荐方案的目标时长由分析后自动判定；"
                    "调用方应改用 auto_timing.auto_decide_timing()"
                )
            target = self.target_duration_seconds

        low, high = self.range.resolve(target)
        return DerivedParams(
            target_duration=target,
            min_duration=low,
            max_duration=high,
            total_seconds=total_seconds,
        )

    def to_json(self) -> dict:
        """序列化（§ 任务恢复：快照必须能完整还原参数）。"""
        return {
            "split_mode": self.split_mode.value,
            "count_policy": self.count_policy.value,
            "target_episode_count": self.target_episode_count,
            "allowed_count_min": self.allowed_count_min,
            "allowed_count_max": self.allowed_count_max,
            "target_duration_seconds": (
                _frac_to_json(self.target_duration_seconds)
                if self.target_duration_seconds is not None
                else None
            ),
            "range": self.range.to_json(),
            "strategy": self.strategy.value,
            "duration_policy": self.duration_policy,
            "allow_episode_exceptions": self.allow_episode_exceptions,
            "decided_episode_count": self.decided_episode_count,
            "decided_target_seconds": _frac_to_json(self.decided_target_seconds),
            "decided_strategy": self.decided_strategy,
            "decided_beat_seconds": _frac_to_json(self.decided_beat_seconds),
        }

    @classmethod
    def from_json(cls, data: dict) -> "SplitSettings":
        """从 to_json 的产物还原（§ 任务恢复）。键名与 to_json 一一对应。"""
        range_data = data.get("range") or {}
        mode = RangeMode(range_data.get("mode", RangeMode.PERCENT.value))
        if mode == RangeMode.MANUAL:
            range_spec = RangeSpec(
                mode=mode,
                manual_min=_parse_frac_json(range_data["min_seconds"]),
                manual_max=_parse_frac_json(range_data["max_seconds"]),
                tolerance=Fraction(1, 5),
            )
        else:
            range_spec = RangeSpec(
                mode=mode,
                tolerance=_parse_frac_json(range_data["tolerance"])
                if range_data.get("tolerance") is not None
                else Fraction(1, 5),
            )
        return cls(
            split_mode=SplitMode(data.get("split_mode", SplitMode.TARGET_DURATION.value)),
            count_policy=CountPolicy(data.get("count_policy", CountPolicy.EXACT.value)),
            target_episode_count=data.get("target_episode_count"),
            allowed_count_min=data.get("allowed_count_min"),
            allowed_count_max=data.get("allowed_count_max"),
            target_duration_seconds=(
                _parse_frac_json(data["target_duration_seconds"])
                if data.get("target_duration_seconds") is not None
                else None
            ),
            range=range_spec,
            strategy=Strategy(data.get("strategy", Strategy.STORY.value)),
            duration_policy=data.get("duration_policy", "hard"),
            allow_episode_exceptions=bool(data.get("allow_episode_exceptions", False)),
            decided_episode_count=data.get("decided_episode_count"),
            decided_target_seconds=(
                _parse_frac_json(data["decided_target_seconds"])
                if data.get("decided_target_seconds") is not None
                else None
            ),
            decided_strategy=data.get("decided_strategy"),
            decided_beat_seconds=(
                _parse_frac_json(data["decided_beat_seconds"])
                if data.get("decided_beat_seconds") is not None
                else None
            ),
        )


@dataclass
class DerivedParams:
    """派生参数。duration_seconds 仅供显示，不得作为第二套时间轴（§18.1）。"""

    target_duration: Fraction
    min_duration: Fraction
    max_duration: Fraction
    total_seconds: Fraction

    def to_json(self) -> dict:
        return {
            "target_duration_seconds": _frac_to_json(self.target_duration),
            "min_duration_seconds": _frac_to_json(self.min_duration),
            "max_duration_seconds": _frac_to_json(self.max_duration),
        }

    def describe(self) -> str:
        return (
            f"目标 {format_timecode(self.target_duration)}｜"
            f"范围 {format_timecode(self.min_duration)}–{format_timecode(self.max_duration)}"
        )


@dataclass
class FeasibilityIssue:
    code: str
    level: str  # info / warn / block
    message: str
    hint: str = ""


@dataclass
class FeasibilityReport:
    """可行性检查结果（§5）。必须在调用 AI 之前给出（§17.4）。"""

    total_seconds: Fraction = Fraction(0)
    derived: DerivedParams | None = None
    min_episodes: int = 0
    max_episodes: int = 0
    allowed_min: int = 0
    allowed_max: int = 0
    exact_count_ok: bool | None = None
    state: UnresolvedState = UnresolvedState.NONE
    issues: list[FeasibilityIssue] = field(default_factory=list)
    #: 推荐方案（模式C）把 D 交给分析后的自动判定：此时**还没有**派生参数，
    #: 也没有可信的可行集数区间（要等 N 定下来才有 L/U）。界面必须认这条，
    #: 否则会把默认的 0 打印成"0 – 0 集"——把"还没算"当成"算出来是 0"。
    auto_pending: bool = False

    @property
    def blocking(self) -> bool:
        return any(i.level == "block" for i in self.issues)

    @property
    def count_range_empty(self) -> bool:
        return self.allowed_min > self.allowed_max

    def summary(self) -> str:
        if self.blocking or self.state != UnresolvedState.NONE:
            return self.issues[0].message if self.issues else "参数无法满足"
        if self.count_range_empty:
            return "可行集数范围为空"
        return f"可行集数 {self.allowed_min}–{self.allowed_max} 集"


def check_feasibility(
    settings: SplitSettings,
    total_seconds: Fraction,
    *,
    has_candidates: bool = True,
) -> FeasibilityReport:
    """§5 可行性检查。

    这里只做**数学与参数**层面的检查。候选点不足导致的"无路径"属于
    §10.4 的另一类状态，由分集引擎在求解阶段判定，二者不得混为一谈。
    """
    report = FeasibilityReport(total_seconds=total_seconds)

    problems = settings.validate()
    if problems:
        report.state = UnresolvedState.MATH_INFEASIBLE
        for text in problems:
            report.issues.append(FeasibilityIssue("PARAM_INVALID", "block", text))
        return report

    # 模式C 的 D 由分析后的自动判定给出（见 auto_timing），**无条件**如此——
    # 不能写成"if D 为空"。用户从「按目标时长」切过来时 D 还留着，切走再回来
    # 还得能用；靠"D 是不是 None"来判断，切过去的那一刻判定就被绕过了。
    if settings.split_mode == SplitMode.RECOMMEND:
        report.auto_pending = True
        report.issues.append(
            FeasibilityIssue(
                "AUTO_TIMING_PENDING",
                "info",
                "推荐方案：每集时长与集数将在分析后自动判定。",
                "判定依据是素材自身的内容节奏（候选点分布），"
                "不套用任何预设秒数；手动上下限作为软偏好参与打分。",
            )
        )
        return report

    derived = settings.derive(total_seconds)
    report.derived = derived

    low, high = derived.min_duration, derived.max_duration

    # §5.2 可行集数范围：最少 = ceil(T/U)，最多 = floor(T/L)
    #
    # 必须排在所有"提前返回"的分支之前：界面会直接显示这两个数，
    # 若某个分支先返回，它们还是默认的 0，界面就会打印出 "0 – 0 集" 这种
    # 把"未计算"当成"计算结果"的假信息。
    report.min_episodes = ceiling_div(total_seconds, high)
    report.max_episodes = _floor_div(total_seconds, low)

    # §4.1 本版要求 L ≤ D ≤ U
    #
    # 仅在"目标时长由用户给定"时检查（模式A）。模式B 的 D = T/N 是派生值，
    # 此时 L ≤ D ≤ U 与 §5.1 的 N×L ≤ T ≤ N×U 等价，而后者能给出
    # "分成 N 集至少需要多少秒"这种可操作的提示，故交给 §5.1 处理；
    # 模式C 已经走到上面那个 auto_pending 分支去了。
    user_supplied_target = settings.split_mode == SplitMode.TARGET_DURATION
    if user_supplied_target and not (low <= derived.target_duration <= high):
        report.state = UnresolvedState.MATH_INFEASIBLE
        report.issues.append(
            FeasibilityIssue(
                "TARGET_OUT_OF_RANGE",
                "block",
                "目标时长不在集长范围内。",
                f"目标 {float(derived.target_duration):.3f}s，"
                f"范围 {float(low):.3f}–{float(high):.3f}s。",
            )
        )
        return report

    if settings.range.mode == RangeMode.PERCENT and settings.range.tolerance == 0:
        report.issues.append(
            FeasibilityIssue(
                "ZERO_TOLERANCE",
                "warn",
                "浮动比例为 0%，实际切点可能因帧边界无法精确落在目标时长上。",
                "允许输入，但请预期部分集长与目标存在一帧以内的差异。",
            )
        )

    # §5.1 严格集数的必要条件 N×L ≤ T ≤ N×U
    #
    # 这一步必须排在"可行集数范围为空"之前：用户明确要求 N 集时，
    # "N 集至少需要多少秒"远比"可行集数范围为空"可操作。
    if settings.split_mode == SplitMode.TARGET_EPISODE_COUNT and settings.count_policy == CountPolicy.EXACT:
        n = settings.target_episode_count or 0
        lower_needed = n * low
        upper_allowed = n * high
        ok = lower_needed <= total_seconds <= upper_allowed
        report.exact_count_ok = ok
        if not ok:
            report.state = UnresolvedState.MATH_INFEASIBLE
            if total_seconds < lower_needed:
                report.issues.append(
                    FeasibilityIssue(
                        "EXACT_COUNT_TOO_SHORT",
                        "block",
                        f"总时长不足，无法分成 {n} 集。",
                        f"{n} 集至少需要 {float(lower_needed):.1f} 秒（每集不低于 "
                        f"{float(low):.1f} 秒），当前只有 {float(total_seconds):.1f} 秒"
                        f"（缺口 {float(lower_needed - total_seconds):.1f} 秒）。"
                        f"可减少集数，或放宽最短集长。",
                    )
                )
            else:
                report.issues.append(
                    FeasibilityIssue(
                        "EXACT_COUNT_TOO_LONG",
                        "block",
                        f"总时长超出 {n} 集允许的上限。",
                        f"{n} 集最多容纳 {float(upper_allowed):.1f} 秒（每集不超过 "
                        f"{float(high):.1f} 秒），当前有 {float(total_seconds):.1f} 秒"
                        f"（超出 {float(total_seconds - upper_allowed):.1f} 秒）。"
                        f"可增加集数，或放宽最长集长。",
                    )
                )
            return report

    allowed_min, allowed_max = report.min_episodes, report.max_episodes
    if settings.split_mode == SplitMode.TARGET_EPISODE_COUNT and settings.count_policy == CountPolicy.FLEXIBLE:
        if settings.allowed_count_min is not None:
            allowed_min = max(allowed_min, settings.allowed_count_min)
        if settings.allowed_count_max is not None:
            allowed_max = min(allowed_max, settings.allowed_count_max)
    report.allowed_min, report.allowed_max = allowed_min, allowed_max

    if allowed_min > allowed_max:
        report.state = UnresolvedState.MATH_INFEASIBLE
        report.issues.append(
            FeasibilityIssue(
                "COUNT_RANGE_EMPTY",
                "block",
                "在任何集数下都无法满足当前集长范围。",
                _explain_empty_range(total_seconds, low, high, report),
            )
        )
        return report

    if settings.split_mode == SplitMode.TARGET_EPISODE_COUNT and settings.count_policy == CountPolicy.FLEXIBLE:
        # 弹性模式：给出名义目标与理论集数供参考
        nominal = settings.target_episode_count or 0
        report.issues.append(
            FeasibilityIssue(
                "NOMINAL_TARGET",
                "info",
                f"名义目标 {nominal} 集，可行区间 {allowed_min}–{allowed_max} 集。",
                "弹性模式下 L/U 保持不变，不随算法尝试的集数漂移（§4.2）。",
            )
        )

    return report


def _explain_empty_range(
    total_seconds: Fraction,
    low: Fraction,
    high: Fraction,
    report: FeasibilityReport,
) -> str:
    """为空区间给出具体到相邻集数的解释。

    直接打印"可行集数范围 X–Y"在 X > Y 时毫无意义，必须换成
    "分成 1 集是 120 秒（超上限）、分成 2 集平均 60 秒（低于下限）"这种可操作的说明。
    """
    parts: list[str] = []
    fewer = report.max_episodes  # floor(T/L)：按最短集长算出的最大集数
    more = report.min_episodes   # ceil(T/U)：按最长集长算出的最小集数

    if fewer >= 1:
        average = total_seconds / fewer
        parts.append(
            f"分成 {fewer} 集时每集 {float(average):.1f} 秒"
            + ("，超过最长集长。" if average > high else "。")
        )
    else:
        parts.append(
            f"整片仅 {float(total_seconds):.1f} 秒，尚不足最长集长 {float(high):.1f} 秒的一半。"
        )
    if more >= 2:
        average = total_seconds / more
        parts.append(
            f"分成 {more} 集时每集 {float(average):.1f} 秒"
            + ("，低于最短集长。" if average < low else "。")
        )

    return (
        f"总时长 {float(total_seconds):.1f} 秒，集长范围 "
        f"{float(low):.1f}–{float(high):.1f} 秒；"
        + "".join(parts)
        + "请放宽集长范围，或改用按目标时长的模式（由系统自行确定集数）。"
    )


def remaining_feasible(
    total_seconds: Fraction,
    end_boundary_seconds: Fraction,
    remaining_episodes: int,
    low: Fraction,
    high: Fraction,
) -> bool:
    """§5.3 剩余时长约束。

    严格 N 集模式下，若第 k 集结束于 b、剩余 r 集，必须满足 r×L ≤ T−b ≤ r×U。
    整体算法必须提前排除"前几集都切上限、末集只剩碎片"的路径。
    """
    remaining = total_seconds - end_boundary_seconds
    if remaining < 0:
        return False
    return remaining_episodes * low <= remaining <= remaining_episodes * high


def normalized_duration_distance(
    duration: Fraction,
    target: Fraction,
    low: Fraction,
    high: Fraction,
) -> Fraction:
    """§11.2 按允许区间归一化的时长偏差，返回 0–1。

    浮动为 0（low == high == target）时特殊处理，避免除零：完全相等得 0，
    否则按一帧量级（约 1/25 秒）归一化，保证硬约束仍能区分优劣。
    """
    if low < target < high:
        span = high - target if duration >= target else target - low
        if span <= 0:
            return Fraction(0)
        return min(Fraction(1), abs(duration - target) / span)

    # 退化情形：区间塌缩为一点，或时长落在区间外
    if duration == target:
        return Fraction(0)
    tolerance = Fraction(1, 25)
    return min(Fraction(1), abs(duration - target) / tolerance)


# ---------------------------------------------------------------------------
# 无解建议的一键应用（§10.4）
# ---------------------------------------------------------------------------

#: 建议里的参数名。求解器生成建议、界面应用建议必须用同一套写法——
#: 否则「建议：最长集长放宽到 ≥ 5.4s」与实际被改的字段会各说各话。
ADVICE_MIN_DURATION = "最短集长"
ADVICE_MAX_DURATION = "最长集长"
ADVICE_EPISODE_COUNT = "目标集数"


@dataclass(frozen=True)
class SettingAdvice:
    """一条可照做的设置调整：把 <参数> <关系> <值>。

    由无解诊断（``solver._explain_no_path``）产出：报错框里的「建议：」文案与
    框上的「应用建议」按钮来自同一份数据——按钮应用的和用户读到的必须是
    同一件事，不能一个是文字一个是另算一套。
    """

    parameter: str      # ADVICE_* 之一
    relation: str       # "≥" / "≤"
    value: Fraction     # 秒（集长）或集数

    def describe(self) -> str:
        """与「建议：」文案里的一项完全一致。"""
        if self.parameter == ADVICE_EPISODE_COUNT:
            verb = "提到" if self.relation == "≥" else "降到"
            return f"{self.parameter}{verb} {self.relation} {int(self.value)} 集"
        return f"{self.parameter}放宽到 {self.relation} {float(self.value):.1f}s"


def applicable_setting_advice(advice: list[SettingAdvice]) -> list[SettingAdvice]:
    """其中能一键应用的部分：只认集长上下限。

    诊断里集数与集长是「或」的关系（减少集数 **或** 放宽集长），一键应用只走
    集长这一侧——它不改变用户指定的集数；集数项仍留在建议文字里由用户自己权衡。
    """
    return [
        item for item in advice
        if item.parameter in (ADVICE_MIN_DURATION, ADVICE_MAX_DURATION) and item.value > 0
    ]


def apply_setting_advice(
    settings: SplitSettings, advice: list[SettingAdvice], total_seconds: Fraction
) -> str | None:
    """把建议里的集长上下限写进设置，返回一句变更说明；无可应用项时返回 None。

    两个参数都只往**更松**的方向改（上限取更大、下限取更小），改动基于当前
    生效的 L/U。建议给的是绝对秒数，浮动比例表达不了这种单边要求，因此来源
    切成手动上下限；原先的比例留在 `RangeSpec` 的暂存里，仍可切回。
    """
    items = applicable_setting_advice(advice)
    if not items:
        return None
    if settings.split_mode == SplitMode.RECOMMEND:
        # 模式C 没有"目标时长"可供放宽——集长区间是随自动判定一起定的。
        # 到这一步还拿到建议，说明调用方用错了场景，不该把建议写进设置。
        return None
    derived = settings.derive(total_seconds)
    low, high = derived.min_duration, derived.max_duration
    for item in items:
        if item.parameter == ADVICE_MAX_DURATION:
            high = max(high, item.value)
        else:
            low = min(low, item.value)

    changes: list[str] = []
    if low != derived.min_duration:
        changes.append(
            f"{ADVICE_MIN_DURATION} {_brief_seconds(derived.min_duration)} → {_brief_seconds(low)}"
        )
    if high != derived.max_duration:
        changes.append(
            f"{ADVICE_MAX_DURATION} {_brief_seconds(derived.max_duration)} → {_brief_seconds(high)}"
        )
    if not changes:
        return None

    source_switched = settings.range.mode != RangeMode.MANUAL
    if source_switched:
        settings.range.switch_to(RangeMode.MANUAL)
    settings.range.manual_min, settings.range.manual_max = low, high

    note = "已应用建议：" + "、".join(changes)
    if source_switched:
        note += "（集长来源改为手动，原浮动比例已保留，可在分集设置里切回）"
    return note


def _brief_seconds(seconds: Fraction) -> str:
    return f"{float(seconds):.1f}s"


def _floor_div(numerator: Fraction, denominator: Fraction) -> int:
    if denominator <= 0:
        raise ValueError("除数必须为正")
    quotient = Fraction(numerator) / Fraction(denominator)
    return quotient.numerator // quotient.denominator


def _parse_frac_json(value) -> Fraction:
    """_frac_to_json 的逆运算：int / 十进制字符串 / "n/d" → 精确 Fraction。"""
    if value is None:
        raise ValueError("分数字段为空")
    if isinstance(value, bool):
        raise ValueError("分数字段不能是布尔值")
    if isinstance(value, int):
        return Fraction(value)
    text = str(value).strip()
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(text)


def _frac_to_json(value: Fraction | None):
    """Fraction → JSON 可存形式，保持全精度（§4.1）。

    整数存 int；分母只含 2 和 5 的分数存精确十进制字符串（如 "121.6"）；
    其余存 "n/d" 形式。任何情况下都不经过 float，避免精度损失。
    """
    if value is None:
        return None
    if value.denominator == 1:
        return value.numerator

    den = value.denominator
    twos = fives = 0
    while den % 2 == 0:
        den //= 2
        twos += 1
    while den % 5 == 0:
        den //= 5
        fives += 1
    if den == 1:
        digits = max(twos, fives)
        scaled = value.numerator * (10**digits) // value.denominator
        sign = "-" if scaled < 0 else ""
        scaled = abs(scaled)
        text = str(scaled).rjust(digits + 1, "0")
        return f"{sign}{text[:-digits]}.{text[-digits:]}"
    return f"{value.numerator}/{value.denominator}"
