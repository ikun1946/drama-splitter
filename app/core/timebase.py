"""时间基准与精确时间运算。

设计依据（方案 §9.1、§9.2）：
- 内部一律使用有理数时间（fractions.Fraction）与源视频时间基准下的整数 ticks。
- 禁止用"帧号 ÷ 平均帧率"表示时间；禁止累加四舍五入后的秒数生成后续集起点。
- 界面显示时才做格式化，格式化不参与任何后续计算。

本模块不依赖 FFmpeg，可被独立测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "TimeBase",
    "parse_time_base",
    "parse_rational",
    "parse_seconds",
    "format_timecode",
    "round_half_up",
]

# ffprobe 可能把 time_base / r_frame_rate 报成 "1/25"、"30000/1001"、"0/0"
_RATIONAL_RE = re.compile(r"^\s*(-?\d+)\s*/\s*(-?\d+)\s*$")
# 允许 "123.456" / "123" / "1e3"（科学计数法由 Fraction 直接支持）
_DECIMAL_RE = re.compile(r"^\s*-?\d+(\.\d+)?([eE][-+]?\d+)?\s*$")


class TimeBaseError(ValueError):
    """时间基准解析失败。"""


def parse_rational(text: str | int | float | None) -> Fraction:
    """解析 ffprobe 的 "num/den" 字符串为 Fraction。

    非法输入（None、空串、"0/0"）抛出 TimeBaseError，由调用方决定降级策略，
    不做静默兜底——静默兜底会让错误的时间基准一路传播到导出。
    """
    if text is None:
        raise TimeBaseError("有理数为空")
    if isinstance(text, (int, float)):
        return Fraction(text).limit_denominator(10**9)
    s = str(text).strip()
    if not s:
        raise TimeBaseError("有理数为空字符串")
    m = _RATIONAL_RE.match(s)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den == 0:
            raise TimeBaseError(f"有理数分母为 0: {s!r}")
        return Fraction(num, den)
    if _DECIMAL_RE.match(s):
        return Fraction(s)
    raise TimeBaseError(f"无法解析有理数: {s!r}")


def parse_time_base(text: str) -> "TimeBase":
    """把 "1/25" 这类字符串解析成 TimeBase。"""
    frac = parse_rational(text)
    if frac <= 0:
        raise TimeBaseError(f"时间基准必须为正数: {text!r}")
    return TimeBase(frac.numerator, frac.denominator)


def parse_seconds(text: str | int | float | Fraction | None) -> Fraction:
    """把 ffprobe 的秒数字符串解析为精确 Fraction。

    ffprobe 输出形如 "123.456000"，Fraction 可直接精确解析十进制字符串，
    因此这里绝不经过 float。
    """
    if text is None:
        raise TimeBaseError("秒数为空")
    if isinstance(text, Fraction):
        return text
    if isinstance(text, int):
        return Fraction(text)
    if isinstance(text, float):
        # 仅在外部传入 float 时走此路径；可能引入浮点误差，调用方应尽量避免。
        return Fraction(text).limit_denominator(10**9)
    s = str(text).strip()
    if not s or s.upper() in {"N/A", "NAN"}:
        raise TimeBaseError(f"秒数不可用: {text!r}")
    if not _DECIMAL_RE.match(s):
        raise TimeBaseError(f"无法解析秒数: {s!r}")
    return Fraction(s)


def round_half_up(value: Fraction) -> int:
    """四舍五入（.5 向远离零方向）。仅用于 ticks / 采样索引取整。"""
    if value >= 0:
        return int((value + Fraction(1, 2)).__floor__())
    return -int((-value + Fraction(1, 2)).__floor__())


@dataclass(frozen=True)
class TimeBase:
    """时间基准：一个 tick 等于 num/den 秒。

    注意：本项目把"源视频时间基准"与"帧率"严格区分。MP4 的视频流 time_base
    通常是 1/15360 或 1/90000，而 r_frame_rate 是 30000/1001。
    方案 §18.1 的示例把二者混写为 "1/25"，本实现不沿用该写法。
    """

    num: int
    den: int

    def __post_init__(self) -> None:
        if self.den == 0:
            raise TimeBaseError("时间基准分母不能为 0")
        if self.num <= 0 or self.den <= 0:
            raise TimeBaseError(f"时间基准必须为正: {self.num}/{self.den}")

    # ---- 基本换算 -----------------------------------------------------

    @property
    def seconds_per_tick(self) -> Fraction:
        return Fraction(self.num, self.den)

    def ticks_to_seconds(self, ticks: int) -> Fraction:
        """整数 ticks → 精确秒数。"""
        return Fraction(ticks) * self.seconds_per_tick

    def seconds_to_ticks(self, seconds: Fraction | int) -> int:
        """精确秒数 → 整数 ticks（四舍五入到最近 tick）。

        若 seconds 本身就是由本 TimeBase 的 ticks 换算而来，则本换算无损。
        """
        return round_half_up(Fraction(seconds) / self.seconds_per_tick)

    def to_string(self) -> str:
        return f"{self.num}/{self.den}"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.to_string()


def format_timecode(seconds: Fraction | float | int, decimals: int = 3) -> str:
    """格式化为 "HH:MM:SS.mmm"。

    仅用于界面显示与报告输出（方案 §4.1：最后用于界面显示时才四舍五入）。
    """
    if not isinstance(seconds, Fraction):
        seconds = Fraction(seconds).limit_denominator(10**9)
    negative = seconds < 0
    total = abs(seconds)
    whole = total.numerator // total.denominator
    frac = total - whole

    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)

    scale = 10**decimals
    frac_units = round_half_up(frac * scale)
    if frac_units >= scale:  # 进位（例如 0.9999 显示为 1.000）
        frac_units -= scale
        secs += 1
        if secs == 60:
            secs = 0
            minutes += 1
            if minutes == 60:
                minutes = 0
                hours += 1

    sign = "-" if negative else ""
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}.{frac_units:0{decimals}d}"


def format_seconds_brief(seconds: Fraction | float | int, decimals: int = 3) -> str:
    """格式化为 "123.200秒"，用于审核卡片（方案 §13.2）。"""
    if not isinstance(seconds, Fraction):
        seconds = Fraction(seconds).limit_denominator(10**9)
    scale = 10**decimals
    units = round_half_up(seconds * scale)
    return f"{units / scale:.{decimals}f}秒"
