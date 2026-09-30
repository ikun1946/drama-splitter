"""候选图与动态规划求解（§10.1–10.2）。

把候选点组成一张图，用动态规划在**全局**意义上选出最优的集间边界集合。
这解决了贪心法的固有缺陷：贪心逐集取最优会把剩余时长挤到末集只剩碎片
（§5.3 明确要求提前排除这种路径），而 DP 天然全局最优。

图结构
------
- 源点 S 固定在片头（帧 0），汇点 E 固定在片尾（帧 T）。
- 候选点是中间节点；同帧的多个候选只保留评分最高的一个。
- 边 u→v 存在当且仅当两帧之差落在 [⌈L·fps⌉, ⌊U·fps⌋] 内——这是硬约束，
  不满足的边**不存在**，而不是"代价很高"。

为什么在帧号空间做整数运算
--------------------------
所有候选时间都已吸附到帧边界，因此两帧之差就是整数。把 L、U 换算成帧数后，
边的有效性是**纯整数比较**：既满足 §9.1"内部时间运算不用浮点累加"，
又比 Fraction 快几个数量级（V² 量级的边检查在 Python 里是负担）。

代价函数（§11.2 可比较的评分）
------------------------------
    edge_cost(u→v) = |时长 − 目标时长| / 目标时长 × w_duration
                   + (1 − 候选质量分(v)) × w_boundary

两项都可解释：一项惩罚集长偏离目标，一项偏好证据更强的切点。
**评分是同一体系内的排序分，不是正确率概率（§11.3）**；DP 是对规则分的
全局优化，**不是剧情判断**——边代价的语义部分属于阶段4。

严格集数用"恰好 N 条边"的 DP；弹性集数在 [min, max] 内取总代价最小者（§4.2）。
严格模式无解时必须给出**成因诊断**（§10.4），绝不用 N±1 冒充完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

import numpy as np

from .candidates import CandidatePoint, CandidateSet
from .plan import BoundaryPlan, PlanProblem
from .probe import MediaInfo
from .settings import (
    ADVICE_EPISODE_COUNT,
    ADVICE_MAX_DURATION,
    ADVICE_MIN_DURATION,
    DerivedParams,
    SettingAdvice,
)

__all__ = [
    "SolverWeights",
    "GraphDiagnostics",
    "GraphSolution",
    "CandidateGraphSolver",
    "build_strategy_solver",
    "solve_with_strategy",
    "solve_flexible_with_strategy",
    "solve_from_candidates",
]

# 分块宽度：DP 每层按列分块计算，避免 (V,V) 临时数组一次性吃掉内存
_COLUMN_BLOCK = 512


@dataclass(frozen=True)
class SolverWeights:
    """求解器权重。默认值是先验设定，尚未用真实短剧标定（§11.2）。

    三种策略（§ 策略）对应不同的权重取向：
    - 剧情优先：边界质量与集长均衡；
    - 悬念优先：更看重切点证据（悬念点通常就是强句末）；
    - 时长优先：集长偏差主导，边界质量只作次要参考。
    """

    duration: float = 0.7   # 集长偏离目标的权重
    boundary: float = 0.3   # 切点质量的权重

    def __post_init__(self) -> None:
        if self.duration < 0 or self.boundary < 0:
            raise ValueError("权重不能为负")
        total = self.duration + self.boundary
        if total <= 0:
            raise ValueError("权重之和必须大于 0")

    @classmethod
    def for_strategy(cls, strategy: str) -> "SolverWeights":
        mapping = {
            "story": (0.6, 0.4),
            "suspense": (0.55, 0.45),
            "duration": (0.85, 0.15),
        }
        duration, boundary = mapping.get(strategy, (0.7, 0.3))
        return cls(duration=duration, boundary=boundary)


def strategy_boundary_bonus(
    strategy: str,
    cut_scores: dict[int, float],
    ends_with_question: dict[int, bool],
) -> dict[int, float]:
    """按策略给切点附加奖励/惩罚（叠加在代价上，负值 = 更优）。

    - 悬念优先：前一句以问号/叹号收尾的切点获得奖励——"问完就切"是短剧
      最常用的悬念手法，这是**标点层面的机械判定**，不涉及语义理解；
    - 剧情优先 / 时长优先：不加策略性奖惩，完全依赖候选评分与集长。
    """
    if strategy != "suspense":
        return {}
    bonus: dict[int, float] = {}
    for frame, _score in cut_scores.items():
        if ends_with_question.get(frame):
            bonus[frame] = -0.15  # 负代价 = 更优
    return bonus


@dataclass
class GraphDiagnostics:
    """图的结构信息与无解成因（§10.4 要求分开报告）。"""

    node_count: int = 0
    edge_count: int = 0
    largest_gap_frames: int = 0
    largest_gap_seconds: Fraction = Fraction(0)
    blocking_gaps: list[tuple[Fraction, Fraction]] = field(default_factory=list)
    unreachable_layers: list[int] = field(default_factory=list)
    #: 可一键应用的设置调整（§10.4）：报错框里的「建议：」文案与框上的
    #: 「应用建议」按钮同源——按钮应用的和用户读到的必须是同一件事
    advice: list[SettingAdvice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"节点 {self.node_count}，可行边 {self.edge_count}"]
        if self.blocking_gaps:
            detail = "、".join(
                f"{float(a):.1f}s–{float(b):.1f}s" for a, b in self.blocking_gaps
            )
            parts.append(f"阻断区间：{detail}")
        if self.unreachable_layers:
            parts.append(f"第 {self.unreachable_layers} 层无可达节点")
        return "；".join(parts)


@dataclass
class GraphSolution:
    """DP 求解结果。"""

    cut_frames: list[int] = field(default_factory=list)   # 中间切点（不含片头片尾）
    cut_times: list[Fraction] = field(default_factory=list)
    episode_count: int = 0
    durations: list[Fraction] = field(default_factory=list)
    total_cost: float = 0.0
    layers_explored: int = 0
    diagnostics: GraphDiagnostics = field(default_factory=GraphDiagnostics)
    limitations: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.cut_frames


@dataclass(frozen=True)
class _GraphNode:
    frame: int
    score: float
    kind: str  # source / sink / candidate
    time: Fraction


class CandidateGraphSolver:
    """从候选点构建候选图并求解最优边界集合。"""

    def __init__(
        self,
        candidates: CandidateSet,
        media: MediaInfo,
        derived: DerivedParams,
        *,
        weights: SolverWeights | None = None,
    ) -> None:
        if candidates.is_empty:
            raise ValueError("候选点为空，无法构建候选图")
        self.candidates = candidates
        self.media = media
        self.derived = derived
        self.weights = weights or SolverWeights()
        self.diagnostics = GraphDiagnostics()
        self.limitations: list[str] = list(candidates.limitations)

        self._fps = int(round(float(media.video.nominal_fps))) if media.video else 25
        self._total_frames = media.video.nb_frames if media.video else 0
        if self._total_frames <= 0:
            raise ValueError("源片帧数不可用，无法构建候选图")

        self._nodes: list[_GraphNode] = []
        self._cost: np.ndarray | None = None

    # ---- 构图 ----------------------------------------------------------

    def build(self) -> None:
        """构建节点与代价矩阵。

        节点按帧号排序；同帧候选只保留评分最高的一个；与片头/片尾重合的
        候选丢弃（源点与汇点已覆盖该位置）。
        """
        by_frame: dict[int, CandidatePoint] = {}
        for point in self.candidates.points:
            frame = point.frame_index
            if frame <= 0 or frame >= self._total_frames:
                # 过于贴近片头片尾的点没有意义，源点/汇点已覆盖
                self.limitations.append(
                    f"候选 {float(point.time):.3f}s 贴近片头或片尾，已忽略"
                )
                continue
            current = by_frame.get(frame)
            if current is None or point.score > current.score:
                by_frame[frame] = point

        frames = sorted(by_frame)
        self._nodes = [_GraphNode(0, 1.0, "source", Fraction(0))]
        for frame in frames:
            point = by_frame[frame]
            self._nodes.append(
                _GraphNode(frame, max(0.0, min(1.0, point.score)), "candidate", point.time)
            )
        self._nodes.append(
            _GraphNode(
                self._total_frames,
                1.0,
                "sink",
                Fraction(self._total_frames, self._fps),
            )
        )

        self.diagnostics.node_count = len(self._nodes)
        self._build_cost_matrix()

    def _build_cost_matrix(self) -> None:
        """构建代价矩阵（不可行的边记为 +inf）。

        时长有效区间换算成帧数：L_frames = ⌈L·fps⌉，U_frames = ⌊U·fps⌋。
        注意必须用 ceiling/floor 的**正确方向**：一个集至少要 L_frames 帧才不短于 L，
        至多 U_frames 帧才不长于 U。
        """
        fps = Fraction(self._fps)
        low = self.derived.min_duration * fps
        high = self.derived.max_duration * fps
        low_frames = -((-low.numerator) // low.denominator)     # ceil
        high_frames = high.numerator // high.denominator        # floor

        if low_frames < 1:
            low_frames = 1
        if high_frames < low_frames:
            # L > U 在可行性检查里已拦截，这里兜底并说明
            self.diagnostics.notes.append(
                f"集长区间换算后为空（{low_frames}–{high_frames} 帧），参数本身无解"
            )
            high_frames = low_frames

        count = len(self._nodes)
        frames = np.array([node.frame for node in self._nodes], dtype=np.int64)
        # 每个节点作为"切点"的质量惩罚：汇点不计（片尾是强制终点）
        boundary_penalty = np.array(
            [(1.0 - node.score) * self.weights.boundary for node in self._nodes],
            dtype=np.float64,
        )
        # 时长代价只与两帧之差有关，可按差值预计算，避免 V² 次 float 除法
        max_span = int(frames[-1])
        span_cost = np.abs(np.arange(max_span + 1, dtype=np.float64) / self._fps
                           - float(self.derived.target_duration)) / float(
            self.derived.target_duration
        )
        span_cost *= self.weights.duration

        cost = np.full((count, count), np.inf, dtype=np.float64)
        edge_count = 0
        for i in range(count):
            begin = frames[i] + low_frames
            finish = min(frames[i] + high_frames, max_span)
            if begin > finish:
                continue
            # 用搜索找出该区间内的节点（frames 有序）
            left = int(np.searchsorted(frames, begin, side="left"))
            right = int(np.searchsorted(frames, finish, side="right"))
            if right <= left:
                continue
            targets = frames[left:right]
            spans = targets - frames[i]
            cost[i, left:right] = span_cost[spans] + boundary_penalty[left:right]
            edge_count += right - left

        self._cost = cost
        self.diagnostics.edge_count = edge_count
        self._low_frames, self._high_frames = low_frames, high_frames

        # 无解成因之一：相邻节点间隔超过 U，任何方案都无法覆盖该区间
        gaps = np.diff(frames)
        if len(gaps):
            worst = int(gaps.max())
            self.diagnostics.largest_gap_frames = worst
            self.diagnostics.largest_gap_seconds = Fraction(worst, self._fps)
            if worst > high_frames:
                over = [(int(a), int(b)) for a, b in zip(frames[:-1], frames[1:]) if b - a > high_frames]
                self.diagnostics.blocking_gaps = [
                    (Fraction(a, self._fps), Fraction(b, self._fps)) for a, b in over
                ][:8]

    # ---- 求解 ----------------------------------------------------------

    def solve_exact(self, episode_count: int) -> GraphSolution:
        """严格集数：恰好 N 条边。无解时给出成因，不返回近似方案。"""
        if episode_count < 1:
            raise ValueError("集数必须至少为 1")
        self._solve(max_layers=episode_count)
        sink = len(self._nodes) - 1
        if not np.isfinite(self._reach[episode_count][sink]):
            raise _NoPathError(self._explain_no_path(episode_count), self.diagnostics)
        return self._reconstruct(episode_count)

    def solve_flexible(self, min_count: int, max_count: int) -> GraphSolution:
        """弹性集数：在 [min, max] 内取总代价最小者。"""
        if max_count < min_count or min_count < 1:
            raise ValueError("集数区间不合法")
        self._solve(max_layers=max_count)
        sink = len(self._nodes) - 1
        best_k, best_cost = None, np.inf
        for count in range(min_count, max_count + 1):
            value = self._reach[count][sink]
            if np.isfinite(value) and value < best_cost:
                best_k, best_cost = count, value
        if best_k is None:
            raise _NoPathError(self._explain_no_path(min_count), self.diagnostics)
        return self._reconstruct(best_k)

    # ---- DP 核心 -------------------------------------------------------

    def _solve(self, *, max_layers: int) -> None:
        """逐层 DP。dp[k][j] = 用 k 条边到达节点 j 的最小代价。

        按列分块计算以限制临时数组大小（见 _COLUMN_BLOCK）。
        """
        count = len(self._nodes)
        assert self._cost is not None
        dp = np.full((max_layers + 1, count), np.inf, dtype=np.float64)
        pred = np.full((max_layers + 1, count), -1, dtype=np.int32)
        dp[0][0] = 0.0

        for layer in range(1, max_layers + 1):
            previous = dp[layer - 1]
            if not np.isfinite(previous).any():
                self.diagnostics.unreachable_layers.append(layer)
                continue
            for begin in range(0, count, _COLUMN_BLOCK):
                finish = min(count, begin + _COLUMN_BLOCK)
                block = previous[:, None] + self._cost[:, begin:finish]
                dp[layer][begin:finish] = block.min(axis=0)
                pred[layer][begin:finish] = block.argmin(axis=0)

        self._dp, self._pred = dp, pred
        self._reach = {k: dp[k] for k in range(max_layers + 1)}
        self._layers = max_layers

    def _reconstruct(self, layers_used: int) -> GraphSolution:
        """回溯路径，还原边界集合。"""
        sink = len(self._nodes) - 1
        path = [sink]
        node = sink
        for layer in range(layers_used, 0, -1):
            node = int(self._pred[layer][node])
            path.append(node)
        path.reverse()

        cut_frames = [self._nodes[i].frame for i in path[1:-1]]
        cut_times = [self._nodes[i].time for i in path[1:-1]]

        durations: list[Fraction] = []
        for a, b in zip(path[:-1], path[1:]):
            frames = self._nodes[b].frame - self._nodes[a].frame
            durations.append(Fraction(frames, self._fps))

        solution = GraphSolution(
            cut_frames=cut_frames,
            cut_times=cut_times,
            episode_count=len(durations),
            durations=durations,
            total_cost=float(self._dp[layers_used][sink]),
            layers_explored=self._layers,
            diagnostics=self.diagnostics,
            limitations=list(self.limitations),
        )
        return solution

    # ---- 诊断（§10.4）--------------------------------------------------

    def _explain_no_path(self, requested: int) -> str:
        """无路径时给出「一句话成因 + 该怎么调设置」。

        「DP 第 N 层已无可达节点」是现象不是解释，用户照着它改不了任何东西。
        这里只留两类信息：**哪一段切不动**、**把哪个参数改成多少**（原先同一段话
        还会因为拼了 describe() 而整段重复两遍，报错框里全是重复内容）。

        建议同时以 `SettingAdvice` 留在诊断里：界面上的「应用建议」按钮直接应用
        它，因此**文案与按钮必须来自同一份数据**，不能各算一套。
        """
        low, high = self._low_frames, self._high_frames
        total = self._total_frames
        low_s = float(self.derived.min_duration)
        high_s = float(self.derived.max_duration)
        total_s = total / self._fps

        causes: list[str] = []
        # 建议按成因分组：同一组内的几项是"调哪个都行"（如减集数或放宽集长），
        # 组间用"；"并列；同一参数被多组各要求一个界时只留更严的那个。
        groups: list[list[SettingAdvice]] = []

        # ① 集数与集长本身矛盾：与候选点无关，改设置就能解
        if requested * low > total:
            causes.append(
                f"{requested} 集至少需要 {requested * low_s:.1f}s"
                f"（每集不低于 {low_s:.1f}s），全片只有 {total_s:.1f}s"
            )
            groups.append([
                SettingAdvice(ADVICE_EPISODE_COUNT, "≤", Fraction(int(total // low))),
                SettingAdvice(
                    ADVICE_MIN_DURATION, "≤",
                    _tenth_seconds(Fraction(total, self._fps * requested), "≤"),
                ),
            ])
        elif requested * high < total:
            causes.append(
                f"{requested} 集最多容纳 {requested * high_s:.1f}s"
                f"（每集不超过 {high_s:.1f}s），全片有 {total_s:.1f}s"
            )
            groups.append([
                SettingAdvice(ADVICE_EPISODE_COUNT, "≥", Fraction(-(-total // high))),
                SettingAdvice(
                    ADVICE_MAX_DURATION, "≥",
                    _tenth_seconds(Fraction(total, self._fps * requested), "≥"),
                ),
            ])

        # ② 片头之后 [L,U] 窗口里没有候选点 → 第一集结束不了。
        # 这是最常见的一类：对白句末的节奏与设定的集长对不上。
        points = [node for node in self._nodes if node.kind == "candidate"]
        in_window = [n for n in points if low <= n.frame <= high]
        before = [n for n in points if n.frame < low]
        after = [n for n in points if n.frame > high]
        jumped = not in_window and bool(after)
        if jumped:
            nearest = min(after, key=lambda n: n.frame)
            causes.append(
                f"第一集无法结束：{low_s:.1f}–{high_s:.1f}s 内没有候选切点，"
                f"最近的候选在 {float(nearest.time):.1f}s"
            )
            group = [
                SettingAdvice(ADVICE_MAX_DURATION, "≥", _tenth_seconds(nearest.time, "≥"))
            ]
            if before:
                last = max(before, key=lambda n: n.frame)
                group.append(
                    SettingAdvice(ADVICE_MIN_DURATION, "≤", _tenth_seconds(last.time, "≤"))
                )
            groups.append(group)
        elif not in_window and before:
            last = max(before, key=lambda n: n.frame)
            causes.append(
                f"第一集无法结束：候选点都在 {float(last.time):.1f}s 之前，"
                f"达不到最短集长 {low_s:.1f}s"
            )
            groups.append([
                SettingAdvice(ADVICE_MIN_DURATION, "≤", _tenth_seconds(last.time, "≤"))
            ])

        # ③ 某段候选点间距超过最长集长：这一段任何方案都跨不过去
        # （②已经点出"候选点跳档"时不再重复同一件事）
        if self.diagnostics.blocking_gaps and not jumped:
            start, end = self.diagnostics.blocking_gaps[0]
            causes.append(
                f"{float(start):.1f}s–{float(end):.1f}s 是 {float(end - start):.1f}s 的空白区间，"
                f"超过最长集长 {high_s:.1f}s"
            )
            groups.append([
                SettingAdvice(ADVICE_MAX_DURATION, "≥", _tenth_seconds(end - start, "≥"))
            ])

        if not causes:
            causes.append("当前候选点与集长范围下没有可行切分")

        # 同一参数只留最严的界：更松的那个已被蕴含（"≥50s；≥80s" 只会让人困惑）
        tightest: dict[str, SettingAdvice] = {}
        for group in groups:
            for item in group:
                old = tightest.get(item.parameter)
                if old is None or (
                    item.value > old.value if item.relation == "≥" else item.value < old.value
                ):
                    tightest[item.parameter] = item

        advices = []
        for group in groups:
            kept = [item.describe() for item in group if tightest[item.parameter] == item]
            if kept:
                advices.append("，或".join(kept))
        if not advices:
            advices.append("放宽集长范围（调大浮动比例或改上下限）后重试")

        # 结构化建议：界面上的「应用建议」按钮照着它改设置（§10.4）
        self.diagnostics.advice = list(tightest.values())

        return "；".join(causes[:2]) + "。\n建议：" + "；".join(advices) + "。"


def _tenth_seconds(value: Fraction, relation: str) -> Fraction:
    """把建议的秒数取整到 0.1s，方向与关系一致（"≥" 向上、"≤" 向下）。

    显示只保留一位小数，取整后的值同时用于显示与应用——若按"最近候选 5.32s"
    显示成 5.3s、却把 5.3s 写进设置，照建议改完仍然无解。
    """
    tenths = Fraction(value) * 10
    if relation == "≥":
        units = -((-tenths.numerator) // tenths.denominator)   # ⌈⌉
    else:
        units = tenths.numerator // tenths.denominator          # ⌊⌋
    return Fraction(units, 10)


class _NoPathError(RuntimeError):
    """候选图无路径（§10.4 的 no_path_in_graph）。"""

    def __init__(self, message: str, diagnostics: GraphDiagnostics) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def solve_from_candidates(
    candidates: CandidateSet,
    media: MediaInfo,
    derived: DerivedParams,
    *,
    settings_count_exact: bool,
    target_episode_count: int,
    allowed_min: int,
    allowed_max: int,
    weights: SolverWeights | None = None,
    baseline: "Callable[[], BoundaryPlan] | None" = None,
    strategy: str = "story",
    ends_with_question: "dict[int, bool] | None" = None,
) -> tuple[BoundaryPlan, GraphSolution, list[PlanProblem], list[str]]:
    """从候选点求解最优边界并转成 BoundaryPlan。

    返回 (方案, 图解, 方案层问题, 说明)。
    `baseline` 用于 §20.3 的对照：传入规则基线的构造函数，其总代价会写进说明。
    `strategy` 决定权重取向与策略性奖惩（§ 策略）。
    `ends_with_question` 是「切点帧 → 前一句是否以问号/叹号结尾」的映射，
    由调用方从事件索引算出——图节点本身不承载语义信息。
    """
    # 构图与策略性奖惩走公共入口：搜索阶段（auto_timing）用的是同一个函数，
    # 两边各写一份就会出现「搜的时候按悬念加分、出方案时没加」这种对不上。
    solver, notes = build_strategy_solver(
        candidates,
        media,
        derived,
        strategy=strategy,
        weights=weights,
        ends_with_question=ends_with_question,
    )

    try:
        if settings_count_exact:
            solution = solver.solve_exact(target_episode_count)
        else:
            solution = solver.solve_flexible(allowed_min, allowed_max)
    except _NoPathError as exc:
        # 说明文字自身已是「成因 + 建议」，不再套"候选图中不存在满足约束的路径"
        # 这类前言——报错框里每一行都要能照着改设置。
        problem = PlanProblem("NO_PATH_IN_GRAPH", "block", str(exc))
        empty_solution = GraphSolution(diagnostics=exc.diagnostics)
        return _empty_plan(media), empty_solution, [problem], notes

    # 转成 BoundaryPlan：切点用精确的帧起点秒数，不经过浮点
    interior = [media.frame_start_seconds(frame) for frame in solution.cut_frames]
    plan = BoundaryPlan.from_interior_cuts(
        interior,
        media.video_time_base,
        media.duration_ticks(),
        source="dp",
        strategy="duration",
        semantic_review_status="rule_based_not_reviewed",
    )
    problems = plan.validate(None, derived)

    notes.append(f"DP 总代价 {solution.total_cost:.4f}（{solution.episode_count} 集）")
    notes.append(solution.diagnostics.describe())
    if baseline is not None:
        try:
            reference = baseline()
            reference_cost = _uniform_cost(reference, media, derived, weights or SolverWeights())
            notes.append(
                f"规则基线（等分）总代价 {reference_cost:.4f}"
                f"（{reference.episode_count} 集）——"
                + ("DP 更优" if solution.total_cost <= reference_cost else "基线更优，请核查")
            )
        except Exception as exc:  # noqa: BLE001 - 基线失败不应影响主结果
            notes.append(f"规则基线计算失败：{exc}")

    return plan, solution, problems, notes


def _uniform_cost(
    plan: BoundaryPlan, media: MediaInfo, derived: DerivedParams, weights: SolverWeights
) -> float:
    """计算等分基线在**同一代价函数**下的总代价，用于与 DP 对照（§20.3）。"""
    durations = plan.durations()
    target = float(derived.target_duration)
    total = 0.0
    for duration in durations:
        total += abs(float(duration) - target) / target * weights.duration
    # 等分的切点不是候选点，按"质量为 0"计惩罚——这会高估基线代价，
    # 因此注明口径差异，比较时以趋势为准
    total += (len(durations) - 1) * (1.0 - 0.0) * weights.boundary
    return total


def build_strategy_solver(
    candidates: CandidateSet,
    media: MediaInfo,
    derived: DerivedParams,
    *,
    strategy: str,
    weights: SolverWeights | None = None,
    ends_with_question: "dict[int, bool] | None" = None,
) -> tuple[CandidateGraphSolver, list[str]]:
    """构图并施加策略性奖惩，返回 ``(solver, 说明)``。

    抽出来是为了让**搜索阶段**与**最终求解**走同一份逻辑。悬念优先的奖励施加
    在 ``solver._cost`` 上，若各写一份，迟早会出现"搜的时候按悬念加分、真正出
    方案的时候没加"——两边结果对不上，而且极难察觉（只是切点稍差，不会报错）。
    """
    notes: list[str] = []
    weights = weights or SolverWeights.for_strategy(strategy)
    solver = CandidateGraphSolver(candidates, media, derived, weights=weights)
    solver.build()

    # 策略性奖惩：悬念优先时，问句/叹句后的切点更优（§ 策略）
    if strategy == "suspense" and solver._cost is not None:
        bonus = strategy_boundary_bonus(
            strategy,
            {node.frame: node.score for node in solver._nodes},
            ends_with_question or {},
        )
        if bonus:
            applied = 0
            for index, node in enumerate(solver._nodes):
                value = bonus.get(node.frame)
                if value:
                    # 奖励施加在"以该节点为切点"的所有边上
                    solver._cost[:, index] = np.maximum(
                        solver._cost[:, index] + value, -1.0
                    )
                    applied += 1
            if applied:
                notes.append(f"悬念策略：{applied} 个问句/叹句后的切点获得优先")
    return solver, notes


def solve_with_strategy(
    candidates: CandidateSet,
    media: MediaInfo,
    derived: DerivedParams,
    *,
    strategy: str,
    episode_count: int,
    weights: SolverWeights | None = None,
    ends_with_question: "dict[int, bool] | None" = None,
) -> tuple[GraphSolution | None, GraphDiagnostics]:
    """恰好 ``episode_count`` 集。无路径时返回 ``(None, 诊断)`` 而不是抛异常。

    搜索阶段要把同一个 N 试很多次（换 D 就要重建代价矩阵），任何一次失败都
    只是"这个 N 不行"，不该打断整轮搜索——所以这里吞掉 ``_NoPathError``，
    把成因留在诊断里。
    """
    solver, _notes = build_strategy_solver(
        candidates,
        media,
        derived,
        strategy=strategy,
        weights=weights,
        ends_with_question=ends_with_question,
    )
    try:
        return solver.solve_exact(episode_count), solver.diagnostics
    except _NoPathError as exc:
        return None, exc.diagnostics


def solve_flexible_with_strategy(
    candidates: CandidateSet,
    media: MediaInfo,
    derived: DerivedParams,
    *,
    strategy: str,
    allowed_min: int,
    allowed_max: int,
    weights: SolverWeights | None = None,
    ends_with_question: "dict[int, bool] | None" = None,
) -> tuple[GraphSolution | None, GraphDiagnostics]:
    """在 ``[allowed_min, allowed_max]`` 内取总代价最小者；无解返回 ``(None, 诊断)``。"""
    solver, _notes = build_strategy_solver(
        candidates,
        media,
        derived,
        strategy=strategy,
        weights=weights,
        ends_with_question=ends_with_question,
    )
    try:
        return solver.solve_flexible(allowed_min, allowed_max), solver.diagnostics
    except _NoPathError as exc:
        return None, exc.diagnostics


def _empty_plan(media: MediaInfo) -> BoundaryPlan:
    return BoundaryPlan(
        boundary_ticks=[0, media.duration_ticks()],
        time_base=media.video_time_base,
        total_ticks=media.duration_ticks(),
    )
