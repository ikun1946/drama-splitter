"""自动判定集长与集数（模式C）。

断言集中在三件容易做错的事：

1. **代价项天生偏向少集**——``total_cost / N`` 里的边界惩罚是 ``(N−1)p / N``，
   N 越小它越小。先验一旦归零，最优解会一路退到"能切几集切几集"。
   所以这里必须有一组"关掉内容信号、结论仍然不是 2 集"的回归。
2. **判定确实随内容变化**——同样 30 分钟，对白密集与长独白必须给出不同的集长。
   只在一份素材上验证等于没验证。
3. **L/U 是软参考而非硬约束**——用户填的区间要能把结果拉过去，但被排除的集数
   仍然要能进入候选（那才叫"软"）。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.core.auto_timing import (
    BEAT_WEIGHT,
    BEAT_WEIGHT_MIN_RATIO,
    DEFAULT_BUDGET,
    MAX_EPISODES,
    BeatProfile,
    TimingCandidate,
    auto_decide_timing,
    estimate_beat,
)
from app.core.candidates import (
    CandidatePoint,
    CandidateSet,
    SOURCE_SENTENCE_END,
)
from app.core.settings import (
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
    UnresolvedState,
)

FPS = 25
TOTAL = 600.0          # 10 分钟；够看清趋势，又不至于让 DP 跑太久


class _Media:
    def __init__(self, total_seconds: float = TOTAL, fps: int = FPS) -> None:
        self.fps = fps
        self.total_frames = int(total_seconds * fps)
        self.video = type(
            "V", (), {"nominal_fps": Fraction(fps), "nb_frames": self.total_frames}
        )()

    @property
    def timeline_duration(self) -> Fraction:
        return Fraction(self.total_frames, self.fps)


def candidates_every(seconds: float, count: int) -> CandidateSet:
    """每 ``seconds`` 秒一个候选点（对白均匀、无特殊结构）。"""
    return CandidateSet(
        points=[
            CandidatePoint(
                time=Fraction(str(seconds * (i + 1))),
                frame_index=int(round(seconds * (i + 1) * FPS)),
                sources=[SOURCE_SENTENCE_END],
                score=0.8,
            )
            for i in range(count)
        ]
    )


def decide(candidates: CandidateSet, *, total: float = TOTAL, budget: int | None = None, **fields):
    settings = SplitSettings(split_mode=SplitMode.RECOMMEND, **fields)
    kwargs = {"budget": budget} if budget is not None else {}
    return auto_decide_timing(
        candidates, _Media(total), settings, _Media(total).timeline_duration, **kwargs
    )


# ---------------------------------------------------------------------------
# 节拍估计
# ---------------------------------------------------------------------------


class TestBeatProfile:
    def test_units_come_from_candidate_spacing(self):
        """一集多长由"一句台词多长"推出来，不来自任何写死的秒数。"""
        dense = estimate_beat(candidates_every(3, 200), _Media(), Fraction(600), 2, 200)
        sparse = estimate_beat(candidates_every(12, 50), _Media(), Fraction(600), 2, 50)

        assert float(dense.median_gap) == pytest.approx(3.0, abs=0.2)
        assert float(sparse.median_gap) == pytest.approx(12.0, abs=0.5)
        # 台词短 → 一集可以更短；台词长 → 一集必须更长
        assert float(dense.anchor_duration) < float(sparse.anchor_duration)

    def test_floor_is_below_anchor(self):
        beat = estimate_beat(candidates_every(4, 150), _Media(), Fraction(600), 2, 150)
        assert 0 < float(beat.floor_duration) < float(beat.anchor_duration)

    def test_anchor_count_tracks_total_duration(self):
        """总时长翻倍 → 集数跟着翻倍（每集长度不变）。"""
        short = estimate_beat(candidates_every(3, 100), _Media(300), Fraction(300), 2, 100)
        long = estimate_beat(candidates_every(3, 200), _Media(600), Fraction(600), 2, 200)
        assert long.anchor_episode_count == pytest.approx(
            short.anchor_episode_count * 2, rel=0.35
        )

    def test_no_candidates_means_no_prior(self):
        """一个候选点都没有 → 先验权重为 0，不装作有依据。"""
        beat = estimate_beat(CandidateSet(points=[]), _Media(), Fraction(600), 2, 60)
        assert beat.sample_count == 0
        assert beat.weight == 0.0

    def test_weight_never_drops_to_zero_when_there_is_data(self):
        """间隔再乱也留一部分先验——代价项偏向少集，没人拉就退化成 2 集了。"""
        base = estimate_beat(candidates_every(3, 200), _Media(), Fraction(600), 2, 200)
        chaotic = BeatProfile(
            median_gap=base.median_gap,
            spread=99.0,                       # 极端离散
            low_confidence=True,
            floor_duration=base.floor_duration,
            anchor_duration=base.anchor_duration,
            anchor_episode_count=base.anchor_episode_count,
            sample_count=base.sample_count,
        )
        assert chaotic.weight >= BEAT_WEIGHT * BEAT_WEIGHT_MIN_RATIO > 0

    def test_no_crash_on_degenerate_input(self):
        for points in (CandidateSet(points=[]), candidates_every(3, 1)):
            beat = estimate_beat(points, _Media(), Fraction(600), 2, 60)
            assert beat.anchor_episode_count >= 2


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


class TestAutoDecide:
    def test_produces_a_plan_and_logs_every_candidate(self):
        decision = decide(candidates_every(3, 200))

        assert decision.chosen is not None
        assert decision.chosen.episode_count >= 2
        assert decision.chosen.ok
        assert decision.candidates, "比较记录要留全"
        assert len([c for c in decision.candidates if c.ok]) >= 2
        text = decision.describe()
        assert "内容节拍" in text and "自动判定：每集" in text

    def test_does_not_collapse_to_the_fewest_episodes(self):
        """核心回归：10 分钟的对白素材不该被判成 2 集。

        这是先验必须一直在线的理由——``total_cost / N`` 里的边界项随 N 变小而
        变小，孤立地用它必然选最少的集数。
        """
        decision = decide(candidates_every(3, 200))
        assert decision.chosen.episode_count > 2, (
            f"退化成 {decision.chosen.episode_count} 集：节拍先验没起作用"
        )

    def test_follows_the_content_not_a_fixed_length(self):
        """同样 10 分钟，对白密集与长独白必须给出不同集长。"""
        dense = decide(candidates_every(3, 200))
        sparse = decide(candidates_every(10, 60))
        assert float(dense.chosen.target_duration) < float(sparse.chosen.target_duration)
        assert dense.chosen.episode_count > sparse.chosen.episode_count

    def test_stays_within_the_feasible_range(self):
        decision = decide(candidates_every(3, 200))
        low, high = decision.feasible_range
        assert low <= decision.chosen.episode_count <= high

    def test_respects_the_budget(self):
        decision = decide(candidates_every(3, 200), budget=6)
        assert decision.probed <= 6
        if decision.state == UnresolvedState.BUDGET_EXHAUSTED:
            assert decision.chosen is not None, "预算耗尽也该交出已找到的最优解"
            assert "预算" in "\n".join(decision.notes)

    def test_default_budget_is_respected_by_default(self):
        decision = decide(candidates_every(3, 200))
        assert decision.budget == DEFAULT_BUDGET
        assert decision.probed <= DEFAULT_BUDGET

    def test_never_exceeds_the_hard_episode_cap(self):
        decision = decide(candidates_every(1, 900), total=900.0)
        if decision.chosen is not None:
            assert decision.chosen.episode_count <= MAX_EPISODES


# ---------------------------------------------------------------------------
# 手动上下限：软参考
# ---------------------------------------------------------------------------


class TestManualRangeIsSoft:
    def test_pulls_the_result_toward_the_user_range(self):
        """用户填的区间要把结果拉过去——但拉过去不等于强制落进去。"""
        free = decide(candidates_every(3, 200))
        narrow = decide(
            candidates_every(3, 200),
            range=RangeSpec(
                mode=RangeMode.MANUAL,
                manual_min=Fraction(40), manual_max=Fraction(50),
            ),
        )
        midpoint = 45.0
        free_gap = abs(float(free.chosen.target_duration) - midpoint)
        narrow_gap = abs(float(narrow.chosen.target_duration) - midpoint)
        assert narrow_gap < free_gap, (
            f"用户区间没能把结果拉过去：自由 {free.chosen.target_duration} vs "
            f"限 40–50 {narrow.chosen.target_duration}"
        )

    def test_out_of_range_counts_are_still_reachable(self):
        """L/U 排除掉的集数仍要能进入候选——那才叫软参考。"""
        decision = decide(
            candidates_every(3, 200),
            range=RangeSpec(
                mode=RangeMode.MANUAL,
                manual_min=Fraction(2), manual_max=Fraction(4),
            ),
        )
        counts = {c.episode_count for c in decision.candidates if c.ok}
        assert max(counts) > 4, f"L/U 外的集数根本没被考虑：{sorted(counts)}"
        assert any(c.range_penalty > 0 for c in decision.candidates if c.ok)

    def test_inside_range_scores_no_penalty(self):
        decision = decide(
            candidates_every(3, 200),
            range=RangeSpec(
                mode=RangeMode.MANUAL,
                manual_min=Fraction(1), manual_max=Fraction(1000),
            ),
        )
        assert all(c.range_penalty == 0.0 for c in decision.candidates if c.ok)

    def test_percent_range_carries_no_independent_information(self):
        """百分比浮动是围绕自动 D 的容差，D 还没定，套它只会自我循环。"""
        percent = decide(
            candidates_every(3, 200),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        assert percent.user_range is None
        assert all(c.range_penalty == 0.0 for c in percent.candidates if c.ok)
        assert percent.chosen.episode_count > 2


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


class TestUnresolvable:
    def test_too_few_candidates_reports_a_cause(self):
        decision = decide(candidates_every(3, 1), total=30.0)
        assert decision.chosen is None
        assert decision.state != UnresolvedState.NONE
        assert decision.notes, "无解必须说清成因"

    def test_zero_length_media_is_rejected(self):
        decision = auto_decide_timing(
            candidates_every(3, 10), _Media(30), SplitSettings(split_mode=SplitMode.RECOMMEND),
            Fraction(0),
        )
        assert decision.chosen is None
        assert decision.state == UnresolvedState.MATH_INFEASIBLE

    def test_describe_never_raises_even_when_empty(self):
        empty = TimingCandidate(episode_count=5, target_duration=Fraction(10), strategy="story")
        assert "无解" in empty.describe()
