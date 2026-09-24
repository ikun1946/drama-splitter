"""音频包络与静音区间：字幕对齐与分块吸附的公共基础。

为什么单独成层
--------------
"哪里是静音"这件事同时被两处需要：
- `subtitles.py`：估计字幕相对原声的整体偏移（§7.2）；
- `asr.py`：分块转写时把块边界吸附到静音处（§7.3），避免把词切两半。

放在任何一侧都会造成不必要的依赖，因此提取为独立模块。

时间基准：包络第 n 个值对应源时间轴的 [n*hop, (n+1)*hop)。
注意音频流起点与视频首帧可能有几十毫秒差异（见 probe.seek_offset_seconds），
该量级小于默认步长，对这两项用途均无实质影响。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .cache import CacheKey, CacheStore, SourceFingerprint
from .timebase import round_half_up

__all__ = [
    "DEFAULT_HOP_SECONDS",
    "audio_rms_envelope",
    "envelope_hop",
    "low_energy_point",
    "silence_intervals",
    "speech_onsets",
    "summarize_silence",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

DEFAULT_HOP_SECONDS = Fraction(1, 20)  # 50ms
DEFAULT_SAMPLE_RATE = 8000


def envelope_hop(hop_seconds: Fraction = DEFAULT_HOP_SECONDS) -> Fraction:
    return hop_seconds


def audio_rms_envelope(
    ffmpeg: str | Path,
    media: str | Path,
    *,
    hop_seconds: Fraction = DEFAULT_HOP_SECONDS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    audio_stream_index: int | None = None,
    cache: CacheStore | None = None,
    fingerprint: SourceFingerprint | None = None,
) -> list[float]:
    """按固定步长计算音频 RMS 包络。

    用 8kHz 单声道足够——用途是时间对齐而非音频质量分析，解码成本极低。
    默认 50ms 步长，远小于需要检出的偏移量级。
    """
    hop_samples = max(1, round_half_up(hop_seconds * sample_rate))

    key = None
    if cache is not None and fingerprint is not None:
        key = CacheKey.build(
            "audio",
            fingerprint,
            hop_seconds=str(hop_seconds),
            sample_rate=sample_rate,
            audio_stream=audio_stream_index if audio_stream_index is not None else "auto",
            kind="rms_envelope",
        )
        cached = cache.load(key)
        if cached is not None:
            return [float(v) for v in cached.get("envelope", [])]

    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(media), "-vn"]
    if audio_stream_index is not None:
        cmd += ["-map", f"0:a:{audio_stream_index}"]
    cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW
    )
    assert proc.stdout is not None

    envelope: list[float] = []
    leftover = b""
    chunk_bytes = hop_samples * 2 * 512
    try:
        while True:
            raw = proc.stdout.read(chunk_bytes)
            if not raw:
                break
            data = leftover + raw
            usable = (len(data) // (hop_samples * 2)) * hop_samples * 2
            leftover = data[usable:]
            if usable == 0:
                continue
            envelope.extend(_pcm16_rms_per_hop(data[:usable], hop_samples))
            if len(envelope) > 4_000_000:  # 约 55 小时，防异常输入吃光内存
                break
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # 不足一个 hop 的尾部直接丢弃：它对应不到完整时间窗，
    # 硬塞进包络会让最后一格的时间含义与其他格子不一致。

    if cache is not None and key is not None:
        cache.save(key, {"envelope": envelope, "hop_seconds": float(hop_seconds)}, note="RMS 包络")
    return envelope


def _pcm16_rms_per_hop(data: bytes, hop_samples: int) -> list[float]:
    """按 hop 计算 RMS（numpy 向量化，避免逐样本 Python 循环）。"""
    import numpy as np

    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    usable = (samples.size // hop_samples) * hop_samples
    if usable == 0:
        return []
    frames = samples[:usable].reshape(-1, hop_samples)
    rms = np.sqrt((frames * frames).mean(axis=1)) / 32768.0
    return rms.tolist()


# ---------------------------------------------------------------------------
# 静音与低能量点
# ---------------------------------------------------------------------------


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * ratio)))
    return ordered[index]


def silence_intervals(
    envelope: list[float],
    *,
    hop_seconds: Fraction = DEFAULT_HOP_SECONDS,
    threshold_ratio: float = 0.15,
    min_duration_seconds: Fraction = Fraction(3, 10),
) -> list[tuple[Fraction, Fraction]]:
    """从包络中找出静音区间。

    阈值取包络的**高分位数**乘以比例，而不是绝对幅度——否则在整体音量偏小的
    素材上会把所有位置都判成静音。用分位数让阈值随素材自适应。
    """
    if not envelope:
        return []

    reference = _percentile(envelope, 0.9)
    threshold = reference * threshold_ratio
    if threshold <= 0:
        # 全静音素材：整段都是静音，但把它当成"可切点"没有意义
        return []

    min_hops = max(1, round_half_up(min_duration_seconds / hop_seconds))
    intervals: list[tuple[Fraction, Fraction]] = []
    start: int | None = None

    for index, value in enumerate(envelope):
        quiet = value <= threshold
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            if index - start >= min_hops:
                intervals.append((Fraction(start) * hop_seconds, Fraction(index) * hop_seconds))
            start = None
    if start is not None and len(envelope) - start >= min_hops:
        intervals.append((Fraction(start) * hop_seconds, Fraction(len(envelope)) * hop_seconds))
    return intervals


def low_energy_point(
    envelope: list[float],
    target_seconds: Fraction,
    *,
    hop_seconds: Fraction = DEFAULT_HOP_SECONDS,
    search_seconds: Fraction = Fraction(3),
    window: int = 3,
) -> Fraction | None:
    """在 `target_seconds` 附近找能量最低的时间点。

    用于把分块边界吸附到静音处（§7.3）。搜索半径默认 ±3 秒——
    足够跨越一句话的停顿，又不会把边界推离原位置太远导致块长失衡。

    用**滑动窗口均值**而不是单点能量：单点可能恰好落在波形的过零点，
    而我们要找的是"一小段安静的时间"，不是"一个安静的时刻"。
    """
    if not envelope:
        return None

    hop = hop_seconds
    centre = int(target_seconds / hop)
    radius = max(1, int(search_seconds / hop))
    lo = max(0, centre - radius)
    hi = min(len(envelope) - 1, centre + radius)
    if hi <= lo:
        return None

    half = max(0, window // 2)
    best_index = lo
    best_score = None
    for index in range(lo, hi + 1):
        window_slice = envelope[max(0, index - half) : min(len(envelope), index + half + 1)]
        if not window_slice:
            continue
        # 越安静越好；同样安静时靠目标点更近的优先（距离作为极小的次要项）
        score = sum(window_slice) / len(window_slice) + abs(index - centre) * 1e-9
        if best_score is None or score < best_score:
            best_score = score
            best_index = index
    return Fraction(best_index) * hop


def speech_onsets(
    envelope: list[float],
    *,
    hop_seconds: Fraction = DEFAULT_HOP_SECONDS,
    threshold_ratio: float = 0.35,
    min_silence_seconds: Fraction = Fraction(1, 4),
) -> list[Fraction]:
    """检测语音起始点（一段静音之后的第一次有声）。

    为什么需要它
    ------------
    字幕对齐要的是**事件对齐**，不是密度对齐。实测：短剧对白下字幕覆盖了
    约 85% 的时间轴，把"字幕是否有字"和"音频是否有声"两条**稠密**序列做互相关，
    峰值只有 0.24 且在相邻 6 格内几乎完全平坦（0.243 vs 0.241），定位能力极差。
    而语音起始点是锐利事件，字幕起始点也是锐利事件，两者配对投票能得到清晰峰值。

    两个参数都必须够紧，否则会碎片化：早先用 `threshold_ratio=0.25` 且不要求
    前置静音时长，一段 10 句话的素材检测出 **72 个起始点**——词内音节的音量凹陷
    全被当成起始点，噪声淹没信号，估计偏移偏了 0.66 秒。
    这里改为：阈值取包络高分位数的 35%，且必须有一段不少于 250ms 的静音才算起始。
    """
    if not envelope:
        return []

    reference = _percentile(envelope, 0.9)
    threshold = reference * threshold_ratio
    if threshold <= 0:
        return []

    min_silence_hops = max(1, round_half_up(min_silence_seconds / hop_seconds))
    onsets: list[Fraction] = []
    quiet_start: int | None = 0  # 序列开头视为静音，允许第 0 格成为起始点

    for index, value in enumerate(envelope):
        if value <= threshold:
            if quiet_start is None:
                quiet_start = index
            continue
        if quiet_start is not None and index - quiet_start >= min_silence_hops:
            onsets.append(Fraction(index) * hop_seconds)
        quiet_start = None
    return onsets


@dataclass(frozen=True)
class SilenceSummary:
    count: int
    total_seconds: Fraction
    longest_seconds: Fraction

    def describe(self) -> str:
        return (
            f"检出 {self.count} 处静音，合计 {float(self.total_seconds):.1f}s，"
            f"最长 {float(self.longest_seconds):.2f}s"
        )


def summarize_silence(
    intervals: list[tuple[Fraction, Fraction]],
) -> SilenceSummary:
    if not intervals:
        return SilenceSummary(0, Fraction(0), Fraction(0))
    durations = [end - start for start, end in intervals]
    return SilenceSummary(
        count=len(intervals),
        total_seconds=sum(durations, Fraction(0)),
        longest_seconds=max(durations),
    )
