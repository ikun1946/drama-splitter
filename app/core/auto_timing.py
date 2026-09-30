"""推荐方案（模式C）：分析后自动判定每集时长与集数。

模式C 现在要自己决定 D 与 N，不再要求用户先填一个目标时长。

为什么不能直接比 ``total_cost``
--------------------------------
代价函数逐边累加（``solver.py``）：

    total_cost = Σ_edges [ |时长 − D| / D × w_duration + (1 − 切点评分) × w_boundary ]

两项都随集数 N 线性累加，而且**绝对值变大不代表变差**。直接拿不同 N 的
``total_cost`` 相比是无效的：15 秒一集能把所有候选点都用上，每集代价极低，
纯比适配度会一路滑向"集数越多越好"的退化解。

三项正交的分数
--------------
    per_episode_cost = total_cost / N                    # 切点配不配合（跨 N 可比）
    beat_penalty(N)  = |ln(N / N_beat)|                  # 偏离内容粒度的程度
    range_penalty(N) = 0 或 |ln(N / N 最近端点)|          # 你的 L/U 意图

    score = per_episode_cost + w_beat × beat_penalty + w_user × range_penalty

节拍锚点从素材算出来，不写死
---------------------------
**不预设"短剧该多长"**。写死一个未经确认的秒数（哪怕 60–120 这种看起来很合理的）
等于拿猜测冒充智能。这里只从**候选点自身的时间分布**推出两个量：

    D_floor = clamp(CONTENT_UNITS_PER_EPISODE_FLOOR × 候选间隔中位数, 10s, T/2)
    D_beat  = clamp(CONTENT_UNITS_PER_EPISODE × D_floor, D_floor, T/2)

含义很直白：**一集至少要装下 8 个内容单元**（低于此，片子切得比它自己的呼吸还碎）；
软中心是 24 个单元。候选点是对白句末/镜头边界，所以"一个内容单元"就是一句台词左右
——全部量纲都来自素材，与平台无关。

节拍先验一直在线，但权重随可信度浮动
------------------------------
先验**不能关**：代价项 ``total_cost / N`` 里的边界惩罚是 ``(N−1)·p / N``，
**N 越小它越小**——也就是说单集代价天然偏向少集。关掉先验，最优解会一路退到
"能切几集切几集"（实测就是 2 集）。必须有东西从另一侧拉住它。

间隔变异系数衡量的是"一个内容单元"这个量有多不可靠：系数越大，"中位数 × 8"
这个估计越粗糙，先验权重按比例下降——**但不归零**。只有压根没有候选点
（一个间隔都统计不到）才真的没有依据。

L/U 是软参考
------------
不再硬性排除任何长度（那要先靠放宽求解窗口才谈得上"软"）：

    solver_L = min(L_user,  D × (1 − SOLVE_TOLERANCE))
    solver_U = max(U_user,  D × (1 + SOLVE_TOLERANCE))

用户的意思只体现在 ``range_penalty`` 上。百分比浮动模式在自动模式下**不承载**用户
意图：浮动比例是围绕自动 D 的容差，D 还没定，套它只会自我循环。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from .candidates import CandidateSet
from .probe import MediaInfo
from .settings import DerivedParams, RangeMode, SplitSettings, UnresolvedState
from .solver import GraphDiagnostics, GraphSolution, solve_with_strategy

__all__ = [
    "BeatProfile",
    "TimingCandidate",
    "TimingDecision",
    "estimate_beat",
    "auto_decide_timing",
    "DEFAULT_STRATEGIES",
    "STRATEGY_LABELS",
    "DEFAULT_BUDGET",
]

DEFAULT_STRATEGIES = ("story", "suspense", "duration")
STRATEGY_LABELS = {"story": "剧情优先", "suspense": "悬念优先", "duration": "时长优先"}

#: 一集至少要装下多少个内容单元（候选间隔中位数）。低于此就切得比片子自己的
#: 呼吸还碎——这是全模块唯一的"经验值"，且只有这一个。
CONTENT_UNITS_PER_EPISODE_FLOOR = 8
#: 软中心对应的单元数。
CONTENT_UNITS_PER_EPISODE = 24

#: 单集时长下限（秒），防止候选点极密时算出荒谬的集数
MIN_EPISODE_SECONDS = 10.0
#: 集数硬上限，兼作 DP 开销的闸门
MAX_EPISODES = 400
#: 最少集数：1 集不叫分集
MIN_EPISODES = 2

#: 求解窗口相对 D 的放宽。L/U 降级为软参考后，窗口必须比用户设定更宽，
#: 否则"软"只是嘴上说说——被排除的长度根本没机会参与竞争。
SOLVE_TOLERANCE = 0.5

#: 节拍先验的权重上限
BEAT_WEIGHT = 0.6
#: 变异系数从 0 升到 :data:`SPREAD_HARD` 时，权重线性降到这个比例（**不是 0**）
BEAT_WEIGHT_MIN_RATIO = 0.4
#: 变异系数的两个刻度：超过 START 开始降权，达到 HARD 降到下限
SPREAD_START = 0.6
SPREAD_HARD = 2.0
#: 用户 L/U 软偏好的权重。**刻意高于**节拍先验：
#: 节拍是先验，是系统替用户猜的；手动上下限是用户亲手填的。
#: 两者都在对数空间度量偏离，量纲相同，所以谁的权重大谁说了算——
#: 用户填了 170–190s 却被判成 72s，那就是界���在替用户做决定。
USER_WEIGHT = 1.2

#: 粗筛采样点上限 / 精筛保留的 N 个数
COARSE_SAMPLES = 16
FINE_KEEP = 3
#: 求解次数预算
DEFAULT_BUDGET = 40


# ---------------------------------------------------------------------------
# 节拍估计
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BeatProfile:
    """内容粒度估计的结果。全部由候选点的时间分布推出。"""

    median_gap: Fraction = Fraction(0)      # 候选点间隔中位数（≈一句台词）
    spread: float = 0.0                     # 间隔变异系数
    low_confidence: bool = False            # 间隔分布很乱 → "内容单元"这个量不可靠
    floor_duration: Fraction = Fraction(0)  # D_floor：一集能切动的最短长度
    anchor_duration: Fraction = Fraction(0)  # D_beat：软中心
    anchor_episode_count: int = 0           # N_beat = round(T / D_beat)
    sample_count: int = 0                   # 参与统计的间隔数

    @property
    def weight(self) -> float:
        """节拍先验的生效权重。

        只有一个数据都没有（候选点为空）时才为 0。有数据就至少留
        :data:`BEAT_WEIGHT_MIN_RATIO` —— 代价项天生偏向少集，先验一旦归零，
        最优解会一路退到"能切几集切几集"。
        """
        if self.sample_count == 0:
            return 0.0
        if not self.low_confidence:
            return BEAT_WEIGHT
        span = SPREAD_HARD - SPREAD_START
        ratio = 1.0 - (self.spread - SPREAD_START) / span if span > 0 else 1.0
        return BEAT_WEIGHT * max(BEAT_WEIGHT_MIN_RATIO, min(1.0, ratio))

    def describe(self) -> str:
        if self.sample_count == 0:
            return "候选点不足，未能估计内容节奏"
        text = (
            f"候选间隔中位数 {float(self.median_gap):.1f}s"
            f"（{self.sample_count} 个间隔，变异系数 {self.spread:.2f}）"
        )
        if self.low_confidence:
            return (
                f"{text}；间隔很乱，内容单元不稳定，"
                f"一集按 {float(self.anchor_duration):.0f}s 估"
                f"（先验权重降到 {self.weight:.2f}）"
            )
        return (
            f"{text}；一集约 {float(self.anchor_duration):.0f}s"
            f"（≈{CONTENT_UNITS_PER_EPISODE} 个内容单元），"
            f"对应 {self.anchor_episode_count} 集"
        )


def _interval_gaps(candidates: CandidateSet, media: MediaInfo) -> np.ndarray:
    """候选点相邻间隔（秒），含片头与片尾之间的首尾两段。

    把片头（帧 0）与片尾（总帧）也当成"点"，这样首尾两段不会被漏掉——
    漏掉的话分布会系统性偏短，节拍锚点跟着偏。
    """
    fps = int(round(float(media.video.nominal_fps))) if media.video else 25
    total = media.video.nb_frames if media.video else 0
    frames = sorted({int(p.frame_index) for p in candidates.points} | {0, total})
    if len(frames) < 2 or total <= 0:
        return np.empty(0, dtype=np.float64)
    return np.diff(np.asarray(frames, dtype=np.int64)).astype(np.float64) / float(fps)


def estimate_beat(
    candidates: CandidateSet,
    media: MediaInfo,
    total_seconds: Fraction,
    n_floor: int,
    n_ceil: int,
) -> BeatProfile:
    """从候选点间隔估计内容粒度，给出 ``D_floor`` 与软中心 ``D_beat``。"""
    if candidates.is_empty:
        # 没有真实候选点就别估：`_interval_gaps` 至少会拿「片头 → 片尾」凑出
        # 一个间隔，统计出来「中位数 = 全片时长」——看着有数，其实毫无依据。
        return BeatProfile(anchor_episode_count=n_floor)
    gaps = _interval_gaps(candidates, media)
    if gaps.size == 0 or n_ceil < n_floor:
        return BeatProfile(anchor_episode_count=n_floor)

    median = float(np.median(gaps))
    mean = float(np.mean(gaps))
    spread = float(np.std(gaps) / mean) if mean > 0 else 0.0
    low_confidence = spread > SPREAD_START

    total = float(total_seconds)
    # 下限：一集至少要装下 CONTENT_UNITS_PER_EPISODE_FLOOR 个内容单元，
    # 低于此就切得比片子自己的呼吸还碎。
    floor_seconds = min(
        max(CONTENT_UNITS_PER_EPISODE_FLOOR * median, MIN_EPISODE_SECONDS), total / 2
    )
    # 软中心：一集装 CONTENT_UNITS_PER_EPISODE 个内容单元。
    # 注意乘的是**中位数**，不是 floor_seconds——后者会把它放大
    # CONTENT_UNITS_PER_EPISODE 倍，变成 192 个单元（实测会算出 576 秒一集）。
    anchor_seconds = min(CONTENT_UNITS_PER_EPISODE * median, total / 2)
    anchor_seconds = max(anchor_seconds, floor_seconds)
    anchor_n = int(round(total / anchor_seconds)) if anchor_seconds > 0 else n_floor
    anchor_n = max(n_floor, min(n_ceil, anchor_n))

    return BeatProfile(
        median_gap=Fraction(median).limit_denominator(1000),
        spread=spread,
        low_confidence=low_confidence,
        floor_duration=Fraction(floor_seconds).limit_denominator(1000),
        anchor_duration=Fraction(anchor_seconds).limit_denominator(1000),
        anchor_episode_count=anchor_n,
        sample_count=int(gaps.size),
    )


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


@dataclass
class TimingCandidate:
    """一个 (N, 策略) 的求解结果。落选者也全部保留——推荐不隐藏比较记录。"""

    episode_count: int
    target_duration: Fraction
    strategy: str
    ok: bool = False
    total_cost: float = 0.0
    per_episode_cost: float = 0.0
    beat_penalty: float = 0.0
    range_penalty: float = 0.0
    score: float = 0.0
    solution: GraphSolution | None = None
    diagnostics: GraphDiagnostics | None = None
    phase: str = "fine"   # coarse / fine

    @property
    def label(self) -> str:
        return STRATEGY_LABELS.get(self.strategy, self.strategy)

    def describe(self) -> str:
        if not self.ok:
            return f"{self.episode_count} 集 × {self.label}：无解"
        return (
            f"{self.episode_count} 集（{float(self.target_duration):.1f}s）× {self.label}"
            f"：单集代价 {self.per_episode_cost:.4f}"
            f" + 节拍 {self.beat_penalty:.3f} + 区间 {self.range_penalty:.3f}"
            f" = {self.score:.4f}"
        )


@dataclass
class TimingDecision:
    chosen: TimingCandidate | None = None
    candidates: list[TimingCandidate] = field(default_factory=list)
    beat: BeatProfile = field(default_factory=BeatProfile)
    feasible_range: tuple[int, int] = (0, 0)
    user_range: tuple[int, int] | None = None
    probed: int = 0
    budget: int = DEFAULT_BUDGET
    state: UnresolvedState = UnresolvedState.NONE
    notes: list[str] = field(default_factory=list)

    @property
    def episode_count(self) -> int:
        return self.chosen.episode_count if self.chosen is not None else 0

    @property
    def target_duration(self) -> Fraction | None:
        return self.chosen.target_duration if self.chosen is not None else None

    @property
    def diagnostics(self) -> GraphDiagnostics | None:
        return self.chosen.diagnostics if self.chosen is not None else None

    def describe(self) -> str:
        lines = [f"内容节拍：{self.beat.describe()}"]
        if self.chosen is None:
            lines.append("自动判定失败：没有任何集数在当前约束下可行。")
        else:
            lines.append(
                f"自动判定：每集 {float(self.chosen.target_duration):.1f}s，"
                f"共 {self.chosen.episode_count} 集，{self.chosen.label}"
            )
            lines.append(
                f"可行集数区间 {self.feasible_range[0]}–{self.feasible_range[1]}，"
                f"共求解 {self.probed} 次（预算 {self.budget}）"
            )
        lines.append("集数 / 策略比较（落选者也列出来）：")
        for candidate in self.candidates:
            lines.append("  " + candidate.describe())
        for note in self.notes:
            lines.append(f"  {note}")
        return "\n".join(lines)


def _user_range(
    settings: SplitSettings, total_seconds: Fraction
) -> tuple[Fraction, Fraction] | None:
    """手动上下限映射成的集数区间；百分比模式返回 None（理由见模块文档）。"""
    if settings.range.mode != RangeMode.MANUAL:
        return None
    low, high = settings.range.manual_min, settings.range.manual_max
    if low is None or high is None or low <= 0 or high < low:
        return None
    total = float(total_seconds)
    n_lo = max(1, math.ceil(total / float(high)))
    n_hi = max(n_lo, math.floor(total / float(low)))
    return n_lo, n_hi


def _solve_window(
    target: Fraction,
    user: tuple[Fraction, Fraction] | None,
) -> tuple[Fraction, Fraction]:
    """求解窗口 = 用户设定 ∪「D 中心 ± SOLVE_TOLERANCE」的并集。"""
    centre = float(target)
    lo = centre * (1.0 - SOLVE_TOLERANCE)
    hi = centre * (1.0 + SOLVE_TOLERANCE)
    if user is not None:
        lo = min(lo, float(user[0]))
        hi = max(hi, float(user[1]))
    return (
        Fraction(lo).limit_denominator(1000),
        Fraction(hi).limit_denominator(1000),
    )


def _sample_counts(n_lo: int, n_hi: int, anchor: int, limit: int) -> list[int]:
    """log(N) 空间采样，并在节拍锚点邻域内加密。"""
    if n_hi <= n_lo:
        return [n_lo]
    picked: set[int] = set()
    picked.add(anchor)
    for offset in (-2, -1, 1, 2):
        value = anchor + offset
        if n_lo <= value <= n_hi:
            picked.add(value)
    if n_hi - n_lo + 1 <= limit:
        picked.update(range(n_lo, n_hi + 1))
    else:
        for point in np.geomspace(n_lo, n_hi, num=limit):
            picked.add(int(round(float(point))))
    return sorted(value for value in picked if n_lo <= value <= n_hi)


def auto_decide_timing(
    candidates: CandidateSet,
    media: MediaInfo,
    settings: SplitSettings,
    total_seconds: Fraction,
    *,
    ends_with_question: "dict[int, bool] | None" = None,
    budget: int = DEFAULT_BUDGET,
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
) -> TimingDecision:
    """在可行集数区间内搜索，取归一化分数最低的 (N, 策略)。

    粗到精：粗筛在 log(N) 空间采样（每点只跑一种策略），精筛对最好的几个 N
    跑全部策略。**必须限流**——换一个 N 就要重建代价矩阵（求解窗口随 D 缩放，
    没法复用），而 DP 是 O(集数 × 候选数²)。
    """
    decision = TimingDecision(budget=budget)
    total = float(total_seconds)
    if total <= 0:
        decision.state = UnresolvedState.MATH_INFEASIBLE
        decision.notes.append("总时长无效，无法判定集长。")
        return decision

    decision.user_range = _user_range(settings, total_seconds)
    n_ceil = max(MIN_EPISODES, min(MAX_EPISODES, math.floor(total / MIN_EPISODE_SECONDS)))
    n_floor = MIN_EPISODES

    # 集数区间先按"内容粒度下限"收：低于 floor_duration 的一集切得比片子
    # 自己的呼吸还碎，不该进候选。注意 floor 要用候选点估，所以先估节拍。
    beat = estimate_beat(candidates, media, total_seconds, n_floor, n_ceil)
    floor_seconds = float(beat.floor_duration)
    if floor_seconds > 0:
        n_ceil = max(n_floor, min(n_ceil, math.floor(total / floor_seconds)))
    decision.beat = beat
    decision.feasible_range = (n_floor, n_ceil)
    if n_ceil < n_floor:
        decision.state = UnresolvedState.MATH_INFEASIBLE
        decision.notes.append(
            f"素材太短（{total:.1f}s），连 {n_floor} 集都切不出来。"
        )
        return decision

    user = (Fraction(settings.range.manual_min), Fraction(settings.range.manual_max)) \
        if settings.range.mode == RangeMode.MANUAL \
        and settings.range.manual_min and settings.range.manual_max else None
    samples = _sample_counts(n_floor, n_ceil, beat.anchor_episode_count, COARSE_SAMPLES)

    coarse: list[TimingCandidate] = []
    for count in samples:
        if decision.probed >= budget:
            decision.state = UnresolvedState.BUDGET_EXHAUSTED
            decision.notes.append(
                f"已按预算 {budget} 次求解停止粗筛。"
            )
            break
        candidate = _try(
            candidates, media, total_seconds, count, "duration",
            beat, decision, user, ends_with_question, phase="coarse",
        )
        if candidate is not None:
            coarse.append(candidate)

    if not coarse:
        decision.state = UnresolvedState.NO_PATH_IN_GRAPH
        decision.notes.append(
            f"在 {n_floor}–{n_ceil} 集之间没有任何一组能切出来，"
            "通常是候选点太稀疏（对白句末不落在可切的位置）。"
        )
        for sample in coarse:
            decision.candidates.append(sample)
        return decision

    # 粗筛只跑了时长优先；精筛对最好的几个 N 跑全部策略
    coarse.sort(key=lambda c: c.score)
    finalists = sorted({c.episode_count for c in coarse[:FINE_KEEP]})
    for count in finalists:
        for strategy in strategies:
            if decision.probed >= budget:
                decision.state = UnresolvedState.BUDGET_EXHAUSTED
                decision.notes.append(f"已按预算 {budget} 次求解停止精筛。")
                break
            candidate = _try(
                candidates, media, total_seconds, count, strategy,
                beat, decision, user, ends_with_question, phase="fine",
            )
            if candidate is not None:
                decision.candidates.append(candidate)

    usable = [c for c in decision.candidates if c.ok]
    if not usable:
        decision.state = UnresolvedState.NO_PATH_IN_GRAPH
        decision.notes.append("粗筛找到了可行集数，但精筛时全部策略都无解。")
        return decision

    # 平局先取集数少的（长集比碎集更接近成片意图），再比总代价
    decision.chosen = min(
        usable, key=lambda c: (round(c.score, 6), c.episode_count, c.total_cost)
    )
    losers = [c for c in usable if c is not decision.chosen]
    if losers:
        decision.notes.append(
            "落选：" + "、".join(
                f"{c.episode_count} 集×{c.label}({c.score:.4f})" for c in losers
            )
        )
    if decision.state == UnresolvedState.BUDGET_EXHAUSTED:
        decision.notes.append("以上是在预算内找到的最优解。")
    return decision


def _try(
    candidates: CandidateSet,
    media: MediaInfo,
    total_seconds: Fraction,
    count: int,
    strategy: str,
    beat: BeatProfile,
    decision: TimingDecision,
    user: "tuple[Fraction, Fraction] | None",
    ends_with_question: "dict[int, bool] | None",
    *,
    phase: str,
) -> TimingCandidate | None:
    """解一个 (N, 策略)，失败返回 None（不算作候选）。"""
    decision.probed += 1
    target = Fraction(total_seconds) / count
    low, high = _solve_window(target, user)
    derived = DerivedParams(
        target_duration=target,
        min_duration=low,
        max_duration=high,
        total_seconds=total_seconds,
    )
    solution, diagnostics = solve_with_strategy(
        candidates, media, derived,
        strategy=strategy,
        episode_count=count,
        ends_with_question=ends_with_question,
    )
    candidate = TimingCandidate(
        episode_count=count,
        target_duration=target,
        strategy=strategy,
        diagnostics=diagnostics,
        phase=phase,
    )
    if solution is None or solution.is_empty:
        return None
    candidate.ok = True
    candidate.solution = solution
    candidate.total_cost = solution.total_cost
    candidate.per_episode_cost = solution.total_cost / max(1, solution.episode_count)
    candidate.beat_penalty = _beat_penalty(count, beat)
    candidate.range_penalty = _range_penalty(count, decision.user_range)
    candidate.score = (
        candidate.per_episode_cost
        + beat.weight * candidate.beat_penalty
        + USER_WEIGHT * candidate.range_penalty
    )
    return candidate


def _beat_penalty(count: int, beat: BeatProfile) -> float:
    anchor = beat.anchor_episode_count
    if anchor <= 0 or count <= 0:
        return 0.0
    return abs(math.log(count / anchor))


def _range_penalty(count: int, user: "tuple[int, int] | None") -> float:
    if user is None:
        return 0.0
    low, high = user
    if low <= count <= high:
        return 0.0
    edge = low if count < low else high
    if edge <= 0:
        return 0.0
    return abs(math.log(count / edge))
