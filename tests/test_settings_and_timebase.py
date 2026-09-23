"""时间基准与参数规则的单元测试。

§19 的四个"修正后的结果示例"在这里被固化为断言：
它们是文档里唯一可验证的数值承诺，一旦回归就该立刻报警。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
    UnresolvedState,
    ceiling_div,
    check_feasibility,
    normalized_duration_distance,
    remaining_feasible,
)
from app.core.timebase import (
    TimeBase,
    TimeBaseError,
    format_timecode,
    parse_rational,
    parse_seconds,
    parse_time_base,
)


class TestTimeBaseParsing:
    def test_parse_rational_common_forms(self):
        assert parse_rational("1/25") == Fraction(1, 25)
        assert parse_rational("30000/1001") == Fraction(30000, 1001)
        assert parse_rational("25") == Fraction(25)

    def test_parse_rational_rejects_zero_denominator(self):
        with pytest.raises(TimeBaseError):
            parse_rational("0/0")

    def test_parse_rational_rejects_garbage(self):
        with pytest.raises(TimeBaseError):
            parse_rational("N/A")
        with pytest.raises(TimeBaseError):
            parse_rational("")

    def test_parse_seconds_is_exact(self):
        """ffprobe 的十进制秒数必须精确解析，绝不经过 float。"""
        assert parse_seconds("121.600000") == Fraction(1216, 10)
        assert parse_seconds("0.040") == Fraction(4, 100)
        assert parse_seconds(Fraction(3, 7)) == Fraction(3, 7)

    def test_parse_time_base_roundtrip(self):
        tb = parse_time_base("1/15360")
        assert tb.num == 1 and tb.den == 15360
        assert tb.to_string() == "1/15360"

    def test_ticks_roundtrip_is_lossless(self):
        """由 ticks 换算出的秒数再换算回 ticks，必须无损。"""
        tb = TimeBase(1, 15360)
        for ticks in (0, 1, 460800, 1234567):
            seconds = tb.ticks_to_seconds(ticks)
            assert tb.seconds_to_ticks(seconds) == ticks


class TestTimecodeFormatting:
    def test_basic(self):
        assert format_timecode(Fraction(0)) == "00:00:00.000"
        assert format_timecode(Fraction(1232, 10)) == "00:02:03.200"
        assert format_timecode(Fraction(7296)) == "02:01:36.000"

    def test_rounding_carries_correctly(self):
        # 0.9999 秒显示为 1.000 秒时，必须进位到秒而不是显示 00:00:00.1000
        assert format_timecode(Fraction(9999, 10000)) == "00:00:01.000"
        # 59.9999 → 01:00.000
        assert format_timecode(Fraction(599999, 10000)) == "00:01:00.000"


class TestCeilingDiv:
    def test_exact_and_inexact(self):
        assert ceiling_div(Fraction(600), Fraction(100)) == 6
        assert ceiling_div(Fraction(601), Fraction(100)) == 7
        assert ceiling_div(Fraction(5700), Fraction(180)) == 32  # 31.67 → 32


class TestRangeSpec:
    def test_percent_resolves_fractionally(self):
        spec = RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5))
        low, high = spec.resolve(Fraction(1216, 10))
        assert low == Fraction(9728, 100)
        assert high == Fraction(14592, 100)

    def test_manual_and_percent_are_exclusive(self):
        """§4.1 两种方式不可同时生效；切换时保留旧输入但不参与计算。"""
        spec = RangeSpec(
            mode=RangeMode.PERCENT,
            manual_min=Fraction(100),
            manual_max=Fraction(140),
            tolerance=Fraction(1, 5),
        )
        # 百分比模式生效时，手动值不参与计算
        low, high = spec.resolve(Fraction(120))
        assert (low, high) == (Fraction(96), Fraction(144))

        spec.switch_to(RangeMode.MANUAL)
        low, high = spec.resolve(Fraction(120))
        assert (low, high) == (Fraction(100), Fraction(140))

    def test_switch_back_restores_previous_tolerance(self):
        spec = RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(3, 10))
        spec.switch_to(RangeMode.MANUAL)
        spec.manual_min, spec.manual_max = Fraction(50), Fraction(70)
        spec.switch_to(RangeMode.PERCENT)
        assert spec.tolerance == Fraction(3, 10)


class TestSection19Examples:
    """§19 修正后的结果示例。四个演算必须逐位对上。"""

    def test_case1_120min_exact60_pm20(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=60,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        report = check_feasibility(settings, Fraction(7200))
        assert report.derived.target_duration == Fraction(120)
        assert report.derived.min_duration == Fraction(96)
        assert report.derived.max_duration == Fraction(144)
        assert report.exact_count_ok is True
        assert report.state is UnresolvedState.NONE

    def test_case2_95min_target150_pm20(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(150),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        report = check_feasibility(settings, Fraction(5700))
        assert report.derived.min_duration == Fraction(120)
        assert report.derived.max_duration == Fraction(180)
        assert report.min_episodes == 32
        assert report.max_episodes == 47

    def test_case3_47min_exact30_pm20(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=30,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        report = check_feasibility(settings, Fraction(2820))
        assert report.derived.target_duration == Fraction(94)
        assert report.derived.min_duration == Fraction(752, 10)
        assert report.derived.max_duration == Fraction(1128, 10)
        assert report.exact_count_ok is True

    def test_case4_121min36s_exact60_pm20(self):
        """§4.2 例：必须先算 97.28–145.92，不能先取整成 122 再算。"""
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=60,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        report = check_feasibility(settings, Fraction(7296))
        assert report.derived.target_duration == Fraction(1216, 10)
        assert report.derived.min_duration == Fraction(9728, 100)
        assert report.derived.max_duration == Fraction(14592, 100)


class TestFeasibilityBlocking:
    def test_section_5_1_example_is_rejected_before_analysis(self):
        """§5.1 例：600 秒要分成 10 集、每集 80–100 秒，参数本身无解。"""
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=10,
            range=RangeSpec(mode=RangeMode.MANUAL, manual_min=Fraction(80), manual_max=Fraction(100)),
        )
        report = check_feasibility(settings, Fraction(600))
        assert report.blocking
        assert report.state is UnresolvedState.MATH_INFEASIBLE
        assert report.issues[0].code == "EXACT_COUNT_TOO_SHORT"
        assert "800" in report.issues[0].hint

    def test_target_outside_range_is_reported(self):
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(100),
            range=RangeSpec(mode=RangeMode.MANUAL, manual_min=Fraction(50), manual_max=Fraction(60)),
        )
        report = check_feasibility(settings, Fraction(100))
        assert report.blocking
        assert report.issues[0].code == "TARGET_OUT_OF_RANGE"

    def test_zero_tolerance_warns_but_does_not_block(self):
        """§4.1 0% 允许输入，但必须提示可能无法精确满足。"""
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(120),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        report = check_feasibility(settings, Fraction(7200))
        assert not report.blocking
        assert any(i.code == "ZERO_TOLERANCE" for i in report.issues)

    def test_flexible_intersects_user_range(self):
        """§5.2 弹性模式还要与用户允许集数区间取交集。"""
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.FLEXIBLE,
            target_episode_count=60,
            allowed_count_min=58,
            allowed_count_max=62,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        report = check_feasibility(settings, Fraction(7200))
        assert not report.blocking
        # 数学可行区间是 50–75，与 58–62 的交集就是 58–62
        assert (report.allowed_min, report.allowed_max) == (58, 62)


class TestRemainingConstraint:
    def test_section_5_3_example_rejects_tail_fragment(self):
        """§5.3 例：600 秒、5 集、100–140 秒，前 4 集都切 140 会让末集只剩 40 秒。"""
        low, high = Fraction(100), Fraction(140)
        assert not remaining_feasible(Fraction(600), Fraction(560), 1, low, high)
        # 同样条件下把边界前移到 200 秒，剩余 4 集 400 秒落在 [400, 560] 内
        assert remaining_feasible(Fraction(600), Fraction(200), 4, low, high)

    def test_negative_remaining_is_rejected(self):
        assert not remaining_feasible(Fraction(600), Fraction(700), 1, Fraction(100), Fraction(140))


class TestNormalizedDistance:
    def test_zero_tolerance_does_not_divide_by_zero(self):
        """§11.2 浮动为 0 时特殊处理，避免除零。"""
        value = normalized_duration_distance(Fraction(120), Fraction(120), Fraction(120), Fraction(120))
        assert value == Fraction(0)
        non_zero = normalized_duration_distance(
            Fraction(121), Fraction(120), Fraction(120), Fraction(120)
        )
        assert 0 < non_zero <= 1

    def test_inside_range_is_normalized_within_unit(self):
        value = normalized_duration_distance(
            Fraction(130), Fraction(120), Fraction(96), Fraction(144)
        )
        # 偏差 10 秒 / 上行空间 24 秒
        assert value == Fraction(10, 24)
