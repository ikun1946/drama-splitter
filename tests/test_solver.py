"""候选图与动态规划求解器测试。

这里的断言集中在三件容易被做错的事：
1. **§5.3 末集碎片**：贪心逐集取最优会把剩余时长挤到末集只剩碎片；
   DP 必须全局最优，构造一个"贪心必败"的用例直接验证。
2. **§10.4 无解诊断**：无路径时必须给出可操作的成因，而不是一句"无解"。
3. **§4.2 严格集数**：恰好 N 集，无解就报错，绝不用 N±1 冒充完成。
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from app.core.candidates import (
    CandidatePoint,
    CandidateSet,
    SOURCE_SENTENCE_END,
    SOURCE_SHOT_CUT,
)
from app.core.settings import DerivedParams, RangeMode, RangeSpec, SplitMode, SplitSettings
from app.core.solver import (
    CandidateGraphSolver,
    SolverWeights,
    solve_from_candidates,
)
from app.core.timebase import TimeBase

FPS = 25


class FakeMedia:
    """提供求解器与 BoundaryPlan 所需的最小接口，时间全部用精确 Fraction。

    video_time_base 必须是真实的 TimeBase——BoundaryPlan 的 tick 换算依赖它，
    用裸 Fraction 会在 seconds_to_ticks 处炸掉。
    """

    def __init__(self, total_seconds: float = 100.0, fps: int = FPS) -> None:
        self.fps = fps
        self.total_frames = int(total_seconds * fps)
        self.video = type(
            "V", (), {"nominal_fps": Fraction(fps), "nb_frames": self.total_frames}
        )()
        self.video_time_base = TimeBase(num=1, den=12800)

    def duration_ticks(self) -> int:
        return self.total_frames * (12800 // self.fps)

    def frame_start_seconds(self, index: int) -> Fraction:
        return Fraction(index, self.fps)


def derived(target: float, low: float, high: float, total: float = 100.0) -> DerivedParams:
    return DerivedParams(
        target_duration=Fraction(str(target)),
        min_duration=Fraction(str(low)),
        max_duration=Fraction(str(high)),
        total_seconds=Fraction(str(total)),
    )


def candidates_at(times: list[float], scores: list[float] | None = None) -> CandidateSet:
    points = []
    for index, time in enumerate(times):
        score = scores[index] if scores else 0.8
        frame = int(round(time * FPS))
        points.append(
            CandidatePoint(
                time=Fraction(frame, FPS),
                frame_index=frame,
                sources=[SOURCE_SENTENCE_END],
                score=score,
            )
        )
    return CandidateSet(points=points)


@pytest.fixture
def media() -> FakeMedia:
    return FakeMedia(100.0)


# ---------------------------------------------------------------------------
# 基本求解
# ---------------------------------------------------------------------------


class TestExactSolve:
    def test_exact_count_and_coverage(self, media):
        """恰好 N 集，且各集时长都在 [L, U] 内、拼接覆盖全片。"""
        solver = CandidateGraphSolver(
            candidates_at([25, 50, 75]), media, derived(25, 20, 30)
        )
        solver.build()
        solution = solver.solve_exact(4)
        assert solution.episode_count == 4
        assert solution.cut_times == [Fraction(25), Fraction(50), Fraction(75)]
        assert all(Fraction(20) <= d <= Fraction(30) for d in solution.durations)
        assert sum(solution.durations) == Fraction(100)

    def test_costs_prefer_higher_score_candidates(self, media):
        """时长相近时，应选评分更高的候选做切点。

        数据设计（100s、L=20、U=30、目标 25、严格 4 集）：
        可行的三切点组合只有 {24,51,74} 与 {26,51,74}（其余组合必有集长越界）。
        26 分高（0.9）、24 分低（0.2），DP 应选 26。
        """
        candidates = CandidateSet(
            points=[
                CandidatePoint(time=Fraction(24), frame_index=600,
                               sources=[SOURCE_SENTENCE_END], score=0.2),
                CandidatePoint(time=Fraction(26), frame_index=650,
                               sources=[SOURCE_SENTENCE_END], score=0.9),
                CandidatePoint(time=Fraction(51), frame_index=1275,
                               sources=[SOURCE_SENTENCE_END], score=0.5),
                CandidatePoint(time=Fraction(74), frame_index=1850,
                               sources=[SOURCE_SENTENCE_END], score=0.5),
            ]
        )
        solver = CandidateGraphSolver(
            candidates, media, derived(25, 20, 30),
            weights=SolverWeights(duration=0.5, boundary=0.5),
        )
        solver.build()
        solution = solver.solve_exact(4)
        assert solution.cut_times == [Fraction(26), Fraction(51), Fraction(74)], (
            f"应选评分高的 26s：{[str(t) for t in solution.cut_times]}"
        )

    def test_solution_is_frame_aligned(self, media):
        solver = CandidateGraphSolver(
            candidates_at([25.32, 50.8, 75.04]), media, derived(25, 20, 30)
        )
        solver.build()
        solution = solver.solve_exact(4)
        for time in solution.cut_times:
            assert (time * FPS).denominator == 1, f"{time} 不是帧起点"

    def test_same_input_gives_same_output(self, media):
        """求解必须确定：同输入同输出，否则审核结果不可复现。"""
        outputs = []
        for _ in range(2):
            solver = CandidateGraphSolver(
                candidates_at([40, 70]), media, derived(30, 15, 40)
            )
            solver.build()
            solution = solver.solve_exact(3)
            outputs.append((solution.cut_frames, round(solution.total_cost, 12)))
        assert outputs[0] == outputs[1]

    def test_dedupes_candidates_on_same_frame(self, media):
        """同帧的多个候选只保留评分最高的一个。"""
        candidates = CandidateSet(
            points=[
                CandidatePoint(
                    time=Fraction(25), frame_index=625,
                    sources=[SOURCE_SENTENCE_END], score=0.9,
                ),
                CandidatePoint(
                    time=Fraction(25), frame_index=625,
                    sources=[SOURCE_SHOT_CUT], score=0.4,
                ),
            ]
        )
        candidates.points.append(
            CandidatePoint(time=Fraction(50), frame_index=1250,
                           sources=[SOURCE_SENTENCE_END], score=0.8)
        )
        candidates.points.append(
            CandidatePoint(time=Fraction(75), frame_index=1875,
                           sources=[SOURCE_SENTENCE_END], score=0.8)
        )
        solver = CandidateGraphSolver(candidates, media, derived(25, 20, 30))
        solver.build()
        assert solver.diagnostics.node_count == 5  # 源点 + 3 个候选（同帧去重后）+ 汇点
        solution = solver.solve_exact(4)
        # 25s 处保留分高者（0.9），其余两个切点 0.8：代价 = (0.1+0.2+0.2)*0.3
        assert solution.total_cost == pytest.approx(0.15, abs=1e-9)


# ---------------------------------------------------------------------------
# §5.3 末集碎片：DP 的全局性必须能救贪心救不了的局
# ---------------------------------------------------------------------------


class TestTailFragmentAvoidance:
    def test_dp_avoids_greedy_tail_fragment(self, media):
        """贪心逐集取最优会把末集挤成 10s 碎片；DP 必须给出全部合法的解。

        候选在 45/70s，L=20、U=45、严格 3 集：
        - 贪心前载：先切 45（最长），再切 45 → 剩 10s < L，无解；
        - 全局最优：45 + 25 + 30（或 40+30+30 等），每集都合法。
        """
        solver = CandidateGraphSolver(
            candidates_at([45, 70]), media, derived(30, 20, 45)
        )
        solver.build()
        solution = solver.solve_exact(3)
        assert solution.episode_count == 3
        assert all(Fraction(20) <= d <= Fraction(45) for d in solution.durations), (
            f"出现非法集长：{[str(d) for d in solution.durations]}"
        )
        assert sum(solution.durations) == Fraction(100)

    def test_all_episode_durations_within_range(self, media):
        """候选每 10 秒一个（10–90s），逐档验证 2–5 集的解全部合法。

        注意 100s、U=30 时 3 集是不可能的（3×30=90 < 100），所以
        这里选的是数学上可行的集数档位。
        """
        # 可行档位推导：count×U ≥ T 且 count×L ≤ T
        # → count ≥ ⌈100/30⌉ = 4 且 count ≤ ⌊100/10⌋ = 10
        for count in (4, 5, 6, 7):
            solver = CandidateGraphSolver(
                candidates_at([10, 20, 30, 40, 50, 60, 70, 80, 90]),
                media,
                derived(20, 10, 30),
            )
            solver.build()
            solution = solver.solve_exact(count)
            assert solution.episode_count == count
            assert all(Fraction(10) <= d <= Fraction(30) for d in solution.durations), (
                f"{count} 集时出现越界时长：{[str(d) for d in solution.durations]}"
            )
            assert sum(solution.durations) == Fraction(100)


# ---------------------------------------------------------------------------
# 无解诊断（§10.4）
# ---------------------------------------------------------------------------


class TestNoPathDiagnostics:
    def test_exact_count_impossible_reports_frame_deficit(self, media):
        """§5.1：N 集所需帧数超过全片时，报出具体缺口而不是"无解"。"""
        solver = CandidateGraphSolver(
            candidates_at([50]), media, derived(50, 40, 60)
        )
        solver.build()
        with pytest.raises(_no_path_error()) as excinfo:
            solver.solve_exact(3)  # 3 × 40s = 120s > 100s
        message = str(excinfo.value)
        assert "至少需要" in message
        assert "120" in message or "3000" in message

    def test_blocking_gap_is_reported(self, media):
        """相邻候选间隔超过 U 时，任何方案都无法覆盖该区间——必须指出来。"""
        solver = CandidateGraphSolver(
            candidates_at([10, 90]), media, derived(30, 10, 30)
        )
        solver.build()
        with pytest.raises(_no_path_error()) as excinfo:
            solver.solve_exact(2)
        assert "空白区间" in str(excinfo.value)

    def test_flexible_range_with_no_feasible_count(self, media):
        solver = CandidateGraphSolver(
            candidates_at([10, 90]), media, derived(30, 10, 30)
        )
        solver.build()
        with pytest.raises(_no_path_error()):
            solver.solve_flexible(2, 3)

    def test_diagnostics_carry_node_and_edge_counts(self, media):
        solver = CandidateGraphSolver(
            candidates_at([25, 50, 75]), media, derived(25, 20, 30)
        )
        solver.build()
        solver.solve_exact(4)
        assert solver.diagnostics.node_count == 5
        assert solver.diagnostics.edge_count > 0


def _no_path_error():
    from app.core.solver import _NoPathError

    return _NoPathError


# ---------------------------------------------------------------------------
# 对外入口与 BoundaryPlan 转换
# ---------------------------------------------------------------------------


class TestSolveFromCandidates:
    def test_produces_valid_plan(self, media):
        plan, solution, problems, notes = solve_from_candidates(
            candidates_at([25, 50, 75]),
            media,
            derived(25, 20, 30),
            settings_count_exact=True,
            target_episode_count=4,
            allowed_min=2,
            allowed_max=6,
        )
        assert plan.episode_count == 4
        assert plan.coverage_valid()
        assert not [p for p in problems if p.is_blocking], (
            f"方案层校验不应有阻断项：{[p.describe() for p in problems]}"
        )
        assert solution.durations == [Fraction(25)] * 4
        assert any("DP 总代价" in note for note in notes)

    def test_flexible_mode_uses_range(self, media):
        plan, solution, problems, _ = solve_from_candidates(
            candidates_at([25, 50, 75]),
            media,
            derived(25, 20, 30),
            settings_count_exact=False,
            target_episode_count=99,
            allowed_min=3,
            allowed_max=6,
        )
        assert 3 <= plan.episode_count <= 6
        assert plan.coverage_valid()

    def test_no_path_yields_blocking_problem(self, media):
        plan, solution, problems, _ = solve_from_candidates(
            candidates_at([10, 90]),
            media,
            derived(30, 10, 30),
            settings_count_exact=True,
            target_episode_count=2,
            allowed_min=2,
            allowed_max=2,
        )
        blocking = [p for p in problems if p.is_blocking]
        assert blocking and blocking[0].code == "NO_PATH_IN_GRAPH"
        assert not solution.cut_frames

    def test_baseline_comparison_note(self, media):
        """§20.3 要求规则基线与优化结果可对照。"""
        from app.core.plan import BoundaryPlan

        def baseline() -> "BoundaryPlan":
            return BoundaryPlan.from_interior_cuts(
                [Fraction(40), Fraction(60)],
                media.video_time_base,
                media.duration_ticks(),
            )

        _plan, _solution, _problems, notes = solve_from_candidates(
            candidates_at([25, 50, 75]),
            media,
            derived(25, 20, 30),
            settings_count_exact=True,
            target_episode_count=4,
            allowed_min=2,
            allowed_max=6,
            baseline=baseline,
        )
        assert any("规则基线" in note for note in notes), notes

    def test_empty_candidates_raises_clearly(self, media):
        with pytest.raises(ValueError):
            CandidateGraphSolver(CandidateSet(), media, derived(25, 20, 30))

    def test_candidates_at_edges_are_ignored(self, media):
        """与片头/片尾重合的候选没有意义，应被忽略并记录。"""
        candidates = candidates_at([0.0, 50, 100.0])
        solver = CandidateGraphSolver(candidates, media, derived(25, 20, 30))
        solver.build()
        assert solver.diagnostics.node_count == 3  # 源点 + 50s + 汇点
        assert any("贴近片头或片尾" in note for note in solver.limitations)


# ---------------------------------------------------------------------------
# 性能护栏
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """语音视频 → 转写 → 候选 → 动态规划 → 方案 的完整链路。"""

    def _run_worker(self, binaries, media, settings) -> dict:
        from app.gui.workers import AnalysisWorker

        worker = AnalysisWorker(binaries, media, 0, settings)
        outcome: dict = {}
        worker.stage_log.connect(lambda text: outcome.setdefault("log", []).append(text))
        worker.failed.connect(lambda message: outcome.setdefault("failed", message))
        worker.completed.connect(
            lambda plan, report, problems, candidates: outcome.update(
                plan=plan, candidates=candidates
            )
        )
        worker.run()
        return outcome

    def test_no_path_when_target_window_has_no_candidates(self, binaries, speech_video):
        """§10.4 真实场景：对白句末不落在目标时长窗口内 → 无解，必须报错。

        40.9s 素材、目标 10s ±10%（L=9/U=11）：10 个对白句末候选里
        没有一个落在 [9,11]s（最近的句末是 3.80s 与 12.40s），
        因此 4 集方案在候选图上不存在路径。
        正确行为是明确报错并解释成因，而不是交出一个 1 集的空方案。
        """
        from app.core.probe import probe_media
        from app.core.settings import SplitMode, SplitSettings

        media = probe_media(binaries, speech_video)
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(10),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 10)),
        )
        outcome = self._run_worker(binaries, media, settings)

        assert "failed" in outcome, "无解必须报错，而不是交出空方案"
        message = outcome["failed"]
        assert ("第一集无法结束" in message) or ("空白区间" in message) or ("至少需要" in message), (
            f"报错应包含可操作的成因：{message}"
        )
        assert "最近的候选在" in message, f"应指出最近的可用候选位置：{message}"
        assert "plan" not in outcome, "无解时不得产出方案"

    def test_dp_path_with_wider_range(self, binaries, speech_video):
        """放宽到 ±30%（L=7/U=13）后 DP 应给出 4 集方案，切点全部来自候选。"""
        from app.core.probe import probe_media
        from app.core.settings import SplitMode, SplitSettings

        media = probe_media(binaries, speech_video)
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(10),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(3, 10)),
        )
        outcome = self._run_worker(binaries, media, settings)

        assert "failed" not in outcome, f"分析失败：{outcome['failed']}"
        assert any("动态规划" in line for line in outcome.get("log", [])), outcome["log"]

        plan = outcome["plan"]
        assert plan.episode_count == 4
        assert plan.coverage_valid()
        for episode in plan.episodes():
            duration = episode.duration_seconds
            assert Fraction(7) <= duration <= Fraction(13), (
                f"集长 {float(duration):.3f}s 越界"
            )

    def test_dp_boundaries_prefer_sentence_ends(self, binaries, speech_video):
        """DP 选出的切点应落在候选点上，而不是等分位置。

        等分位置（10.22/20.44/30.66s）没有任何证据支撑；DP 的意义正在于
        把切点挪到有证据的地方。
        """
        from app.core.candidates import build_candidate_set
        from app.core.probe import probe_media
        from app.core.settings import DerivedParams, RangeSpec, SplitMode, SplitSettings, check_feasibility
        from app.core.solver import solve_from_candidates

        media = probe_media(binaries, speech_video)
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(10),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(3, 10)),
        )
        report = check_feasibility(settings, media.timeline_duration)
        assert not report.blocking and report.derived is not None

        from app.core.asr import AsrEngine, AsrSettings
        from app.core.cache import CacheStore, SourceFingerprint
        from pathlib import Path

        store = CacheStore(Path(media.path).parent / "cache")
        fingerprint = SourceFingerprint.of(media.path)
        engine = AsrEngine(AsrSettings(), cache=store, fingerprint=fingerprint)
        transcript, _ = engine.transcribe_cached(
            media.path, fingerprint, store,
            audio_stream_index=0,
            duration_seconds=media.timeline_duration,
        )
        candidates = build_candidate_set(
            transcript=transcript, shots=None, blacks=None,
            timeline_duration=media.timeline_duration,
            frame_index_at_or_after=media.frame_index_at_or_after,
            snap_to_frame=media.snap_to_frame,
            merge_distance=Fraction(1),
        )
        assert not candidates.is_empty, "10 句对白应产出候选点"

        plan, solution, _problems, notes = solve_from_candidates(
            candidates, media, report.derived,
            settings_count_exact=False,
            target_episode_count=4,
            allowed_min=report.allowed_min,
            allowed_max=report.allowed_max,
        )
        # 注意：±10% 时无解（对白句末不落在 9-11s 窗口），必须用 ±30%
        # 才有可行路径——这正是上一个测试验证的行为。
        candidate_times = {float(p.time) for p in candidates.points}
        for cut in solution.cut_times:
            assert float(cut) in candidate_times, (
                f"切点 {float(cut):.3f}s 不是任何候选点——切点必须来自候选"
            )
        assert any("DP 总代价" in note for note in notes)
class TestScale:
    def test_large_graph_completes_quickly(self):
        """599 个候选、60 集（1500s 片长，每集 25s）：真实短剧量级。

        之前这测试用的是 100s 片长配 60 集——每集至少 20s 意味着 60 集
        需要 1200s，数学上就不可能，纯属测试数据错误。
        """
        import time

        long_media = FakeMedia(1500.0)
        times = [round(i * 2.5, 3) for i in range(1, 600)]  # 2.5s … 1497.5s
        solver = CandidateGraphSolver(
            candidates_at(times), long_media, derived(25, 20, 30)
        )
        solver.build()
        started = time.time()
        solution = solver.solve_exact(60)
        elapsed = time.time() - started
        assert solution.episode_count == 60
        assert elapsed < 20, f"60 层 DP 耗时 {elapsed:.1f}s，超出可接受范围"
