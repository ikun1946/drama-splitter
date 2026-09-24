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
from .settings import DerivedParams, UnresolvedState

__all__ = [
    "SolverWeights",
    "GraphDiagnostics",
    "GraphSolution",
    "CandidateGraphSolver",
    "solve_from_candidates",
]

# 分块宽度：DP 每层按列分块计算，避免 (V,V) 临时数组一次性吃掉内存
_COLUMN_BLOCK = 512


@dataclass(frozen=True)
class SolverWeights:
    """求解器权重。默认值是先验设定，尚未用真实短剧标定（§11.2）。"""

    duration: float = 0.7   # 集长偏离目标的权重
    boundary: float = 0.3   # 切点质量的权重

    def __post_init__(self) -> None:
        if self.duration < 0 or self.boundary < 0:
            raise ValueError("权重不能为负")
        total = self.duration + self.boundary
        if total <= 0:
            raise ValueError("权重之和必须大于 0")


@dataclass
class GraphDiagnostics:
    """图的结构信息与无解成因（§10.4 要求分开报告）。"""

    node_count: int = 0
    edge_count: int = 0
    largest_gap_frames: int = 0
    largest_gap_seconds: Fraction = Fraction(0)
    blocking_gaps: list[tuple[Fraction, Fraction]] = field(default_factory=list)
    unreachable_layers: list[int] = field(default_factory=list)
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
        target_frames = float(self.derived.target_duration * fps)

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
        """无路径时给出可操作的成因，而不是一句"无解"。

        「DP 第 N 层无可达节点」是对的现象但不是有用的解释。用户需要知道
        **该去补什么**：是第一集没有可用切点、中间有空白区间、还是集数本身不成立。
        """
        reasons: list[str] = []
        low, high = self._low_frames, self._high_frames
        total = self._total_frames

        if requested * low > total:
            reasons.append(
                f"{requested} 集至少需要 {requested * low} 帧"
                f"（每集不低于 {float(self.derived.min_duration):.1f}s），"
                f"全片只有 {total} 帧"
            )
        if requested * high < total:
            reasons.append(
                f"{requested} 集最多容纳 {requested * high} 帧"
                f"（每集不超过 {float(self.derived.max_duration):.1f}s），"
                f"全片有 {total} 帧"
            )

        # 成因一：第一集无法结束——片头之后的 [L,U] 窗口内没有任何候选点。
        # 这是最常见的情形：对白句末的节奏与用户指定的集长不匹配。
        low_seconds = Fraction(low, self._fps)
        high_seconds = Fraction(high, self._fps)
        candidates_after_head = [
            node for node in self._nodes
            if node.kind == "candidate" and low_seconds <= node.time <= high_seconds
        ]
        if not candidates_after_head:
            later = [node for node in self._nodes if node.kind == "candidate" and node.time > high_seconds]
            earlier = [node for node in self._nodes if node.kind == "candidate" and node.time < low_seconds]
            if later:
                nearest = min(later, key=lambda n: n.time)
                reasons.append(
                    f"第一集无法结束：片头之后 {float(low_seconds):.1f}–{float(high_seconds):.1f}s "
                    f"内没有任何候选点，最近的候选在 {float(nearest.time):.2f}s。"
                    "可放宽集长范围，或接受第一集更长（由整体规划在候选中就近取舍）"
                )
            elif earlier:
                reasons.append(
                    f"第一集无法结束：所有候选都早于 {float(low_seconds):.1f}s，"
                    f"剩余区间没有可用切点"
                )

        if self.diagnostics.blocking_gaps:
            first = self.diagnostics.blocking_gaps[0]
            reasons.append(
                f"候选点之间存在 {float(first[1] - first[0]):.1f}s 的空白区间"
                f"（{float(first[0]):.1f}s–{float(first[1]):.1f}s），"
                f"超过最长集长 {float(self.derived.max_duration):.1f}s，"
                f"任何方案都无法覆盖——需要补充该区间的候选点"
            )
        if self.diagnostics.unreachable_layers:
            reasons.append(
                f"DP 第 {self.diagnostics.unreachable_layers} 层已无可达节点"
            )
        if not reasons:
            reasons.append(
                "在当前候选与集长约束下没有可行路径；"
                "可放宽集长范围或增加候选来源后重试"
            )
        return "；".join(reasons)


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
) -> tuple[BoundaryPlan, GraphSolution, list[PlanProblem], list[str]]:
    """从候选点求解最优边界并转成 BoundaryPlan。

    返回 (方案, 图解, 方案层问题, 说明)。
    `baseline` 用于 §20.3 的对照：传入规则基线的构造函数，其总代价会写进说明。
    """
    notes: list[str] = []
    solver = CandidateGraphSolver(candidates, media, derived, weights=weights)
    solver.build()

    try:
        if settings_count_exact:
            solution = solver.solve_exact(target_episode_count)
        else:
            solution = solver.solve_flexible(allowed_min, allowed_max)
    except _NoPathError as exc:
        problem = PlanProblem(
            "NO_PATH_IN_GRAPH",
            "block",
            f"候选图中不存在满足约束的路径。{exc}",
        )
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


def _empty_plan(media: MediaInfo) -> BoundaryPlan:
    return BoundaryPlan(
        boundary_ticks=[0, media.duration_ticks()],
        time_base=media.video_time_base,
        total_ticks=media.duration_ticks(),
    )
