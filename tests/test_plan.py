"""边界方案模型的单元测试（§9.2、§13.3、§20.1）。

用假时间轴隔离测试，不依赖 FFmpeg；真实的帧映射由
test_media_and_markers.py 用合成素材验证。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.core.plan import BoundaryPlan
from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
)
from app.core.timebase import TimeBase

TB = TimeBase(1, 25)  # 便于阅读：1 tick = 一帧


class FakeTimeline:
    """按固定帧率实现 ceiling 语义的帧号映射。"""

    def __init__(self, fps: int = 25, total_frames: int = 3000) -> None:
        self.fps = fps
        self.total_frames = total_frames

    def frame_index_at_or_after(self, seconds: Fraction) -> int:
        product = Fraction(seconds) * self.fps
        index = -((-product.numerator) // product.denominator)
        return max(0, min(self.total_frames, index))


def make_plan(boundaries: list[int], total: int = 3000) -> BoundaryPlan:
    return BoundaryPlan(boundary_ticks=list(boundaries), time_base=TB, total_ticks=total)


class TestConstruction:
    def test_uniform_splits_exactly(self):
        plan = BoundaryPlan.uniform(4, TB, 3000)
        assert plan.boundary_ticks == [0, 750, 1500, 2250, 3000]
        assert plan.episode_count == 4
        assert all(ep.duration_seconds == Fraction(30) for ep in plan.episodes())

    def test_uniform_handles_indivisible_total(self):
        """不能整除时必须仍然覆盖整片，且不产生零时长集。"""
        plan = BoundaryPlan.uniform(7, TB, 3000)
        assert plan.boundary_ticks[0] == 0
        assert plan.boundary_ticks[-1] == 3000
        assert all(t > 0 for t in plan.durations())
        assert sum(plan.durations()) == Fraction(120)
        assert plan.coverage_valid()

    def test_from_seconds_requires_full_coverage(self):
        """末边界不等于片尾时必须报错，不得静默改写。

        静默改写会把"调用方漏了一个边界"变成"方案悄悄少一集"，
        而且能顺利通过全部校验——这正是要禁止的失败模式。
        """
        with pytest.raises(ValueError, match="末个边界必须等于片尾"):
            BoundaryPlan.from_seconds([0, 30, 60], TB, 3000)

    def test_from_seconds_requires_zero_start(self):
        with pytest.raises(ValueError, match="首个边界必须为 0"):
            BoundaryPlan.from_seconds([1, 30, 120], TB, 3000)

    def test_from_interior_cuts_fills_ends(self):
        plan = BoundaryPlan.from_interior_cuts([30, 60], TB, 3000)
        assert plan.boundary_ticks == [0, 750, 1500, 3000]
        assert plan.coverage_valid()

    def test_from_interior_cuts_rejects_out_of_order(self):
        with pytest.raises(ValueError):
            BoundaryPlan.from_interior_cuts([60, 30], TB, 3000)
        with pytest.raises(ValueError):
            BoundaryPlan.from_interior_cuts([130], TB, 3000)


class TestValidation:
    def test_valid_plan_has_no_blocking_problem(self):
        plan = make_plan([0, 750, 1500, 2250, 3000])
        assert not [p for p in plan.validate() if p.is_blocking]
        assert plan.coverage_valid()

    def test_gap_before_tail_is_blocking(self):
        """末段没覆盖到片尾必须报出来，这是"全片连续覆盖"承诺的核心。"""
        plan = make_plan([0, 750, 1500, 2000])
        problems = plan.validate()
        assert any(p.code == "NOT_END_AT_TOTAL" and p.is_blocking for p in problems)
        assert not plan.coverage_valid()

    def test_non_increasing_boundary_is_blocking(self):
        plan = make_plan([0, 750, 750, 3000])
        problems = plan.validate()
        assert any(p.code == "NOT_STRICTLY_INCREASING" for p in problems)

    def test_exact_count_mismatch_is_blocking(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=5,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        derived = settings.derive(Fraction(120))
        plan = make_plan([0, 1000, 2000, 3000])  # 只有 3 集
        problems = plan.validate(settings, derived)
        assert any(p.code == "WRONG_EPISODE_COUNT" and p.is_blocking for p in problems)

    def test_out_of_range_episode_is_blocking_without_exception(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.MANUAL, manual_min=Fraction(28), manual_max=Fraction(32)),
        )
        derived = settings.derive(Fraction(120))
        plan = make_plan([0, 750, 1500, 2250, 3000])
        plan.boundary_ticks[2] = 1300  # 第二集 550 帧=22 秒，低于下限
        problems = plan.validate(settings, derived)
        offending = [p for p in problems if p.code == "DURATION_OUT_OF_RANGE"]
        assert offending and offending[0].episode == 2

    def test_exception_records_reason_and_clears_block(self):
        """§4.4 只有用户主动启用例外后，**指定集**才可以超出默认范围。

        边界移到 1300 后第 2 集变 22 秒（低于下限）、第 3 集变 38 秒（高于上限），
        两集都越界。只给第 2 集记例外时，第 3 集必须仍然报错——例外不得外溢。
        """
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.MANUAL, manual_min=Fraction(28), manual_max=Fraction(32)),
        )
        derived = settings.derive(Fraction(120))
        plan = make_plan([0, 750, 1500, 2250, 3000])
        plan.boundary_ticks[2] = 1300

        before = [p for p in plan.validate(settings, derived) if p.is_blocking]
        assert {p.episode for p in before} == {2, 3}

        plan.set_exception(2, "该集为剧情完整保留了长镜头")
        after = [p for p in plan.validate(settings, derived) if p.is_blocking]
        assert {p.episode for p in after} == {3}, "例外不应波及其它集"

        plan.set_exception(3, "收尾集需保留完整冲突")
        assert not [p for p in plan.validate(settings, derived) if p.is_blocking]

        record = plan.exceptions[2]
        assert record.reason.startswith("该集")
        assert record.actual_seconds == Fraction(550, 25)


class TestEditing:
    def test_move_boundary_is_blocked_when_it_would_cross(self):
        """§13.3 越过相邻边界或生成零时长集时禁止提交。"""
        plan = make_plan([0, 750, 1500, 2250, 3000])
        problems = plan.move_boundary(1, 1500)
        assert any(p.is_blocking for p in problems)
        # 非法移动不得改变方案
        assert plan.boundary_ticks[1] == 750

    def test_move_boundary_succeeds_within_bounds(self):
        plan = make_plan([0, 750, 1500, 2250, 3000])
        problems = plan.move_boundary(1, 800)
        assert not [p for p in problems if p.is_blocking]
        assert plan.boundary_ticks[1] == 800
        # 共享边界使相邻两集同步变化
        episodes = plan.episodes()
        assert episodes[0].duration_seconds == Fraction(32)
        assert episodes[1].duration_seconds == Fraction(28)

    def test_endpoints_cannot_move(self):
        plan = make_plan([0, 750, 1500, 2250, 3000])
        assert plan.move_boundary(0, 10)
        assert plan.move_boundary(4, 2990)
        assert plan.boundary_ticks[0] == 0
        assert plan.boundary_ticks[-1] == 3000

    def test_lock_travels_with_boundary(self):
        plan = make_plan([0, 750, 1500, 2250, 3000])
        plan.lock_boundary(2)
        assert 1500 in plan.locked_ticks
        plan.move_boundary(2, 1600)
        assert 1600 in plan.locked_ticks
        assert 1500 not in plan.locked_ticks

    def test_missing_lock_is_reported(self):
        """§12.3 重新规划默认保留锁定；冲突时明确指出是哪个锁定导致无解。"""
        plan = make_plan([0, 750, 1500, 2250, 3000])
        plan.locked_ticks = {1499}
        problems = plan.validate()
        assert any(p.code == "LOCK_NOT_PRESERVED" and p.is_blocking for p in problems)


class TestFrameAccounting:
    def test_frame_counts_sum_equals_source(self):
        """§14.5 视频层：固定帧率素材中各集帧数之和应等于源帧数。"""
        timeline = FakeTimeline(25, 3000)
        plan = BoundaryPlan.uniform(7, TB, 3000)
        counts = plan.frame_counts(timeline)
        assert sum(counts) == 3000
        assert all(c > 0 for c in counts)

    def test_frame_counts_exact_for_aligned_boundaries(self):
        timeline = FakeTimeline(25, 3000)
        plan = make_plan([0, 750, 1500, 2250, 3000])
        assert plan.frame_counts(timeline) == [750, 750, 750, 750]

    def test_audio_cut_points_are_shared_once(self):
        """§9.2 同一个共享边界只计算一次，避免两侧各自取整造成采样遗漏或重复。"""
        plan = make_plan([0, 750, 1500, 2250, 3000])
        samples = plan.audio_cut_samples(48000)
        assert samples == [0, 1_440_000, 2_880_000, 4_320_000, 5_760_000]
        assert len(samples) == len(plan.boundary_ticks)


class TestVersions:
    def test_diff_reports_moved_boundaries_and_affected_episodes(self):
        old = make_plan([0, 750, 1500, 2250, 3000]); old.version = 1
        new = make_plan([0, 750, 1600, 2250, 3000]); new.version = 2
        diff = new.diff(old)
        assert not diff.is_identical
        assert len(diff.moved_boundaries) == 1
        assert diff.affected_episodes == [2, 3]
        assert "1 个切点移动" in diff.summary()

    def test_json_roundtrip_preserves_boundaries_and_locks(self):
        plan = make_plan([0, 750, 1500, 2250, 3000])
        plan.version = 3
        plan.lock_boundary(1)
        plan.set_exception(2, "长镜头")
        payload = plan.to_json()
        restored = BoundaryPlan.from_json(payload, TB, 3000)
        assert restored.boundary_ticks == plan.boundary_ticks
        assert restored.locked_ticks == plan.locked_ticks
        assert restored.exceptions[2].reason == "长镜头"
        assert restored.version == 3

    def test_export_directory_is_versioned(self):
        plan = make_plan([0, 3000])
        plan.version = 7
        assert plan.export_directory_name() == "plan_0007"
