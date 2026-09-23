"""校验库：从产物中还原帧号与声音标记位置。

供 tests/ 使用，也是"参数即事实"的执行者：不靠目测截图判断切点对不对，
而是把帧号从画面里读回来、把提示音从波形里找出来，用数字说话。

对应方案 §14.5：
- 视频层：各集帧数之和应等于源帧数；
- 边界层：检查每集首尾帧及切点两侧，避免多帧、少帧或重复内容；
- 音频层：切点附近 A/V 误差不超过一个视频帧时长。
"""

from __future__ import annotations

import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markers import (  # noqa: E402
    BEEP_AMPLITUDE,
    BEEP_INTERVAL_FRAMES,
    BLOCK_GAP,
    BLOCK_MARGIN_X,
    BLOCK_MARGIN_Y,
    BLOCK_SIZE,
    FRAME_BITS,
    SAMPLE_RATE,
    decode_marker_bits,
    marker_bbox,
)

__all__ = [
    "ffmpeg_binary",
    "read_frame_indices",
    "read_frame_indices_and_bytes",
    "read_wav_mono",
    "detect_beep_onsets",
    "SyncMeasurement",
    "measure_av_sync",
    "extract_audio_to_wav",
    "probe_frame_count",
    "probe_start_time",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def ffmpeg_binary() -> str:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from app.core.ffmpeg import locate_ffmpeg

    return str(locate_ffmpeg().ffmpeg)


def ffprobe_binary() -> str:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from app.core.ffmpeg import locate_ffmpeg

    return str(locate_ffmpeg().ffprobe)


# ---------------------------------------------------------------------------
# 帧号还原
# ---------------------------------------------------------------------------


def read_frame_indices(video: str | Path, *, max_frames: int | None = None) -> list[int]:
    """逐帧解码整段视频，从条码里读出每帧的原始帧号。

    实现要点：先用 crop 把画面裁到条码包围盒，每帧只有几千字节，
    因此"逐帧解码全片"的成本可以接受。
    """
    indices, _ = read_frame_indices_and_bytes(video, max_frames=max_frames)
    return indices


def read_frame_indices_and_bytes(
    video: str | Path,
    *,
    max_frames: int | None = None,
) -> tuple[list[int], int]:
    """同上，但额外返回处理的帧数（用于判断是否读全）。

    必须显式指定 `-fps_mode passthrough`：默认的恒定帧率输出策略会在
    首帧 PTS 不为 0 时**补帧填充起始空隙**，导致读取器凭空多出一帧，
    让"帧数不符"的假警报掩盖真正的切点错误。
    """
    x, y, width, height = marker_bbox()
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-fps_mode",
        "passthrough",
        "-vf",
        f"crop={width}:{height}:{x}:{y}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None

    frame_bytes = width * height * 3
    centers = _block_centers_in_crop(x, y)
    indices: list[int] = []
    count = 0

    while True:
        if max_frames is not None and count >= max_frames:
            break
        chunk = proc.stdout.read(frame_bytes)
        if not chunk or len(chunk) < frame_bytes:
            break
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)
        indices.append(_decode_from_crop(frame, centers))
        count += 1

    proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
    proc.wait()
    return indices, count


def _block_centers_in_crop(offset_x: int, offset_y: int) -> list[tuple[int, int]]:
    """把条码块中心从原图坐标换算到裁剪后坐标。"""
    centers: list[tuple[int, int]] = []
    for index in range(FRAME_BITS):
        original_x = BLOCK_MARGIN_X + index * (BLOCK_SIZE + BLOCK_GAP) + BLOCK_SIZE // 2
        original_y = BLOCK_MARGIN_Y + BLOCK_SIZE // 2
        centers.append((original_x - offset_x, original_y - offset_y))
    return centers


def _decode_from_crop(frame: np.ndarray, centers: list[tuple[int, int]]) -> int:
    """从裁剪画面里采样块心，阈值判定得到帧号。

    取 3×3 邻域均值而非单点，抵抗有损编码在块边缘产生的振铃。
    """
    bits: list[int] = []
    for cx, cy in centers:
        patch = frame[max(0, cy - 1) : cy + 2, max(0, cx - 1) : cx + 2]
        bits.append(1 if float(patch.mean()) > 127.0 else 0)
    return decode_marker_bits(bits)


# ---------------------------------------------------------------------------
# 音频标记
# ---------------------------------------------------------------------------


def extract_audio_to_wav(
    video: str | Path,
    target: str | Path,
    *,
    start_seconds: float | None = None,
    duration_seconds: float | None = None,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """把视频音轨导出为单声道 PCM WAV，便于逐样本分析。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error"]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.6f}"]
    cmd += ["-i", str(video)]
    if duration_seconds is not None:
        cmd += ["-t", f"{duration_seconds:.6f}"]
    cmd += [
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        "-y",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"提取音频失败：{(proc.stderr or '').strip()[-600:]}")
    return target


def read_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """读取单声道 WAV，返回归一化到 [-1, 1] 的 float32 数组与采样率。"""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"仅支持 16 位 PCM，实际 {width * 8} 位")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def detect_beep_onsets(samples: np.ndarray, sample_rate: int, *, threshold_ratio: float = 0.25) -> list[float]:
    """检测提示音起始时刻（秒）。

    阈值取标记振幅的固定比例；相邻检测点在 50ms 内视为同一次发声，
    避免正弦波的过零点被重复计入。
    """
    threshold = BEEP_AMPLITUDE * threshold_ratio
    above = np.abs(samples) > threshold
    index = np.flatnonzero(above)
    if index.size == 0:
        return []
    min_gap = max(1, int(0.050 * sample_rate))
    splits = np.flatnonzero(np.diff(index) > min_gap) + 1
    groups = np.split(index, splits)
    return [float(group[0]) / sample_rate for group in groups if group.size]


# ---------------------------------------------------------------------------
# 音画同步测量
# ---------------------------------------------------------------------------


@dataclass
class SyncMeasurement:
    """一集产物的音画同步测量结果。"""

    beep_count: int = 0
    max_abs_offset_seconds: float = 0.0
    offsets: list[float] = field(default_factory=list)
    first_source_frame: int | None = None

    def within(self, tolerance_seconds: float) -> bool:
        return bool(self.offsets) and self.max_abs_offset_seconds <= tolerance_seconds

    def describe(self, fps: float = 25.0) -> str:
        frame_ms = 1000.0 / fps
        return (
            f"提示音 {self.beep_count} 个，最大音画偏差 "
            f"{self.max_abs_offset_seconds * 1000:.1f}ms（一帧={frame_ms:.0f}ms）"
        )


def measure_av_sync(
    video: str | Path,
    tmp_dir: str | Path,
    *,
    fps: float = 25.0,
    beep_interval_frames: int = BEEP_INTERVAL_FRAMES,
) -> SyncMeasurement:
    """用量化标记测量音画同步。

    原理：条码给出"每个输出帧是源片第几帧"，提示音给出音频时刻。
    已知提示音被放在源片中相隔 beep_interval_frames 的帧上，因此
    第 k 个提示音在输出里应当恰好落在"源帧号 = 首帧 + k×间隔"的那一帧上。
    两者之差就是真实的音画偏差，不依赖容器报的 start_time。

    这个方法能抓住 `-avoid_negative_ts make_zero` 之类选项引入的整段偏移，
    而只看容器元数据是发现不了的。
    """
    indices = read_frame_indices(video)
    measurement = SyncMeasurement()
    if not indices:
        return measurement
    measurement.first_source_frame = indices[0]

    target = Path(tmp_dir)
    target.mkdir(parents=True, exist_ok=True)
    wav = extract_audio_to_wav(video, target / f"{Path(video).stem}_sync.wav")
    samples, rate = read_wav_mono(wav)
    onsets = detect_beep_onsets(samples, rate)
    measurement.beep_count = len(onsets)
    if not onsets:
        return measurement

    # 源帧号 → 输出帧号
    position_of = {}
    for output_index, source_index in enumerate(indices):
        position_of.setdefault(source_index, output_index)

    first = indices[0]
    for order, onset in enumerate(onsets):
        expected_source = first + order * beep_interval_frames
        output_index = position_of.get(expected_source)
        if output_index is None:
            continue
        expected_time = output_index / fps
        measurement.offsets.append(onset - expected_time)

    if measurement.offsets:
        measurement.max_abs_offset_seconds = max(abs(value) for value in measurement.offsets)
    return measurement


# ---------------------------------------------------------------------------
# 容器信息
# ---------------------------------------------------------------------------


def probe_frame_count(video: str | Path, *, deep: bool = False) -> int | None:
    """读取帧数。deep=True 时真实解码计数（慢但只依赖实际内容）。"""
    import json

    args = ["-select_streams", "v:0", "-show_streams", "-show_format"]
    if deep:
        args = ["-select_streams", "v:0", "-count_frames", "-show_streams", "-show_format"]
    cmd = [ffprobe_binary(), "-hide_banner", "-loglevel", "error", "-print_format", "json", *args, str(video)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            raw = stream.get("nb_read_frames") if deep else stream.get("nb_frames")
            if raw in (None, "", "N/A"):
                return None
            return int(raw)
    return None


def probe_start_time(video: str | Path) -> float | None:
    """读取容器起始时间（秒），用于验证非零起始 PTS 的处理。"""
    import json

    cmd = [
        ffprobe_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_format",
        str(video),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    value = (data.get("format") or {}).get("start_time")
    if value in (None, "", "N/A"):
        return None
    return float(value)
