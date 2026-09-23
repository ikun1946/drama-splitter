"""媒体探测：FFprobe 封装、时间轴建模、兼容性判定。

设计依据：
- §2.2 实际可用性以媒体后端解码探测为准，不能只看扩展名。
- §9.1 界面显示秒，内部使用源视频时间基准下的整数 PTS；VFR 必须读真实呈现时间。
- §9.3 非零起始 PTS、音视频尾部不一致、VFR、HDR 都要在导入阶段就给出诊断。
- §6.1 转写、解码不得阻塞界面线程 → 本模块全部为同步函数，由后台工作进程调用。

术语约定
--------
本模块用"时间轴秒"表示**相对视频首帧的呈现时间**，即 t=0 对应视频第一帧的 PTS。
所有边界（方案 §9.2 的 b0…bN）都定义在这个域上。
导出时通过 seek_offset_seconds 换算成 FFmpeg `-ss` 所需的输入相对时间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .ffmpeg import FfmpegBinaries, FfmpegError, run_ffprobe_json
from .timebase import (
    TimeBase,
    TimeBaseError,
    format_timecode,
    parse_rational,
    parse_seconds,
    parse_time_base,
    round_half_up,
)

__all__ = [
    "Issue",
    "VideoStreamInfo",
    "AudioStreamInfo",
    "MediaInfo",
    "probe_media",
    "check_compatibility",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "LEVEL_BLOCK",
]

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_BLOCK = "block"

# 判定 VFR 时允许的相对偏差（r_frame_rate 与 avg_frame_rate 的差异超过此比例视为可疑）
_VFR_RATIO_TOLERANCE = Fraction(1, 1000)


@dataclass(frozen=True)
class Issue:
    """一条兼容性或质量问题（对应 §13.1 导入页的兼容性提示）。"""

    level: str
    code: str
    message: str
    detail: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.level == LEVEL_BLOCK


@dataclass
class VideoStreamInfo:
    index: int
    codec_name: str = ""
    codec_long_name: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    time_base: TimeBase | None = None
    r_frame_rate: Fraction | None = None
    avg_frame_rate: Fraction | None = None
    nb_frames: int | None = None
    start_pts: int | None = None
    start_time: Fraction | None = None
    duration: Fraction | None = None
    color_transfer: str = ""
    color_primaries: str = ""
    color_space: str = ""
    field_order: str = ""
    display_aspect_ratio: str = ""

    @property
    def bit_depth(self) -> int:
        """从 pix_fmt 推断位深（yuv420p10le → 10）。"""
        m = re.search(r"p(\d{2})(?:le|be)?$", self.pix_fmt)
        if m:
            return int(m.group(1))
        return 8

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in {"smpte2084", "arib-std-b67", "smpte428"} or self.bit_depth > 10

    @property
    def is_interlaced(self) -> bool:
        return self.field_order.lower() not in {"", "progressive", "unknown"}

    @property
    def nominal_fps(self) -> Fraction:
        """名义帧率，优先 avg_frame_rate（VFR 时它是均值）。"""
        return self.avg_frame_rate or self.r_frame_rate or Fraction(25, 1)


@dataclass
class AudioStreamInfo:
    index: int
    codec_name: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    time_base: TimeBase | None = None
    start_pts: int | None = None
    start_time: Fraction | None = None
    duration: Fraction | None = None
    bit_rate: int | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        """界面上主对白音轨选择项的文字（§17.1 多音轨时必须显式选择）。"""
        layout = self.channel_layout or f"{self.channels}声道"
        label = self.tags.get("title") or self.tags.get("language") or ""
        parts = [f"音轨{self.index}", self.codec_name, f"{self.sample_rate}Hz", layout]
        if label:
            parts.append(f"[{label}]")
        return " ".join(p for p in parts if p)


@dataclass
class MediaInfo:
    path: Path
    container_format: str = ""
    format_long_name: str = ""
    duration: Fraction | None = None
    size_bytes: int = 0
    bit_rate: int = 0
    format_start_time: Fraction | None = None

    video: VideoStreamInfo | None = None
    audio_tracks: list[AudioStreamInfo] = field(default_factory=list)
    subtitle_tracks: list[AudioStreamInfo] = field(default_factory=list)

    is_vfr: bool = False
    vfr_evidence: str = ""
    issues: list[Issue] = field(default_factory=list)

    # ---- 时间轴 --------------------------------------------------------

    @property
    def video_start_time(self) -> Fraction:
        if self.video and self.video.start_time is not None:
            return self.video.start_time
        return Fraction(0)

    @property
    def seek_offset_seconds(self) -> Fraction:
        """视频首帧相对容器起点的偏移。

        FFmpeg 的 `-ss` 作为输入选项时默认相对输入起点（容器 start_time）计时，
        因此导出时需要把"时间轴秒"加上这个偏移才是正确的 `-ss` 值。
        常见 MP4 中该值为 0。
        """
        if self.format_start_time is None:
            return Fraction(0)
        offset = self.video_start_time - self.format_start_time
        return offset if offset > 0 else Fraction(0)

    @property
    def timeline_duration(self) -> Fraction:
        """时间轴总长 T：从视频首帧到视频末尾。"""
        if self.video and self.video.duration is not None:
            return self.video.duration
        if self.duration is not None:
            return self.duration - self.video_start_time
        return Fraction(0)

    @property
    def video_time_base(self) -> TimeBase:
        if self.video and self.video.time_base is not None:
            return self.video.time_base
        return TimeBase(1, 1000)

    def duration_ticks(self) -> int:
        return self.video_time_base.seconds_to_ticks(self.timeline_duration)

    def seconds_to_ticks(self, seconds: Fraction) -> int:
        return self.video_time_base.seconds_to_ticks(seconds)

    def ticks_to_seconds(self, ticks: int) -> Fraction:
        return self.video_time_base.ticks_to_seconds(ticks)

    def frame_index_at(self, seconds: Fraction) -> int:
        """时间轴秒 → 该时刻**正在显示**的帧号（帧起点 <= seconds 的最后一帧）。

        用于预览定位。分集边界请用 frame_index_at_or_after()，语义不同。
        """
        if not self.video:
            return 0
        fps = self.video.nominal_fps
        if self.is_vfr and self._vfr_pts is not None:
            return _search_frame_index(self._vfr_pts, seconds)
        idx = (Fraction(seconds) * fps).__floor__()
        return max(0, idx)

    def frame_index_at_or_after(self, seconds: Fraction) -> int:
        """时间轴秒 → **属于下一集的第一帧**帧号，即满足 k/fps >= seconds 的最小 k。

        这是分集边界的正确语义：
        - b=0 → 0；b=T → nb_frames（表示"最后一帧之后"）。
        - 各集帧数 = 结束帧号 − 起始帧号，求和自动等于源帧数（§14.5 视频层校验）。
        - 边界若已吸附到帧起点（b = k/fps），结果精确为 k。
        """
        if not self.video:
            return 0
        total_frames = self.video.nb_frames
        if self.is_vfr and self._vfr_pts is not None:
            return _search_frame_index_or_after(self._vfr_pts, seconds)

        fps = self.video.nominal_fps
        product = Fraction(seconds) * fps
        index = -((-product.numerator) // product.denominator)  # ceil
        if index < 0:
            index = 0
        if total_frames is not None and index > total_frames:
            index = total_frames
        return index

    def frame_start_seconds(self, frame_index: int) -> Fraction:
        """帧号 → 该帧起点的时间轴秒。"""
        if not self.video:
            return Fraction(0)
        if self.is_vfr and self._vfr_pts is not None:
            if 0 <= frame_index < len(self._vfr_pts):
                return self._vfr_pts[frame_index]
            if self._vfr_pts:
                return self._vfr_pts[-1]
            return Fraction(0)
        return Fraction(frame_index) / self.video.nominal_fps

    def snap_to_frame(self, seconds: Fraction) -> tuple[int, Fraction]:
        """把任意时刻吸附到最近的合法帧边界（§13.3 微调必须吸附帧边界）。

        返回 (帧号, 实际时间轴秒)。同时提供 floor/ceil 两个候选点，
        取距离更近者；距离相同取靠前的帧，避免产生零时长集。
        """
        if not self.video:
            return 0, Fraction(0)
        low = self.frame_index_at(seconds)
        candidates = [low]
        # 边界帧号允许取到 nb_frames：它表示"最后一帧之后"，即末集终点 T。
        max_index = self.video.nb_frames if self.video.nb_frames else low + 1
        if low + 1 <= max_index:
            candidates.append(low + 1)
        best_index = min(
            candidates,
            key=lambda i: (abs(self.frame_start_seconds(i) - seconds), i),
        )
        return best_index, self.frame_start_seconds(best_index)

    def frame_span_seconds(self, start_index: int, frame_count: int) -> Fraction:
        """从 start_index 起 frame_count 帧所占的精确时长（用于导出 -t）。"""
        if frame_count <= 0:
            return Fraction(0)
        end_index = start_index + frame_count
        return self.frame_start_seconds(end_index) - self.frame_start_seconds(start_index)

    # ---- VFR 帧表 ------------------------------------------------------

    _vfr_pts: list[Fraction] | None = None

    def attach_vfr_pts(self, pts_seconds: list[Fraction]) -> None:
        """挂载**完整**的逐帧 PTS 表（仅 VFR 需要）。

        必须是覆盖全片的完整表。抽样得到的窗口片段不得用于此处：
        帧号映射会据此定位，残缺的表会把切点算到错误的位置。
        在完整表可用之前，VFR 素材一律不进入精确导出路径（§9.3）。
        """
        if not pts_seconds:
            return
        self._vfr_pts = pts_seconds
        self.is_vfr = True


def _search_frame_index(pts: list[Fraction], target: Fraction) -> int:
    """在有序帧 PTS 表中二分查找：返回 pts[i] <= target 的最大 i。"""
    if not pts:
        return 0
    lo, hi = 0, len(pts) - 1
    if target < pts[0]:
        return 0
    if target >= pts[hi]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pts[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _search_frame_index_or_after(pts: list[Fraction], target: Fraction) -> int:
    """在有序帧 PTS 表中二分查找：返回 pts[i] >= target 的最小 i。"""
    if not pts:
        return 0
    lo, hi = 0, len(pts) - 1
    if target <= pts[0]:
        return 0
    if target > pts[hi]:
        return len(pts)  # 表末尾之后
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid] >= target:
            hi = mid
        else:
            lo = mid + 1
    return lo


# ---------------------------------------------------------------------------
# 探测入口
# ---------------------------------------------------------------------------


def probe_media(
    binaries: FfmpegBinaries,
    path: str | Path,
    *,
    detect_vfr: bool = True,
    vfr_sample_seconds: float = 8.0,
    vfr_sample_windows: int = 3,
) -> MediaInfo:
    """读取媒体信息并构建时间轴模型。

    VFR 探测默认开启：先比对 r_frame_rate / avg_frame_rate，可疑时再用
    `-read_intervals` 抽样取真实帧 PTS。抽样而非全片扫描，避免长片导入卡死
    （方案 §8.4 控制分析规模的精神）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"源文件不存在: {path}")

    data = run_ffprobe_json(
        binaries,
        ["-show_format", "-show_streams", str(path)],
        timeout=120.0,
    )

    info = MediaInfo(path=path)
    fmt = data.get("format") or {}
    info.container_format = fmt.get("format_name", "")
    info.format_long_name = fmt.get("format_long_name", "")
    info.size_bytes = int(fmt.get("size") or 0)
    info.bit_rate = int(fmt.get("bit_rate") or 0)
    info.format_start_time = _safe_seconds(fmt.get("start_time"))
    info.duration = _safe_seconds(fmt.get("duration"))
    # 注意：format.duration_ts 的单位是 format.time_base 的 ticks，不是秒，
    # 不可直接喂给 parse_seconds。本版不做该兜底，缺失时按 DURATION_UNKNOWN 处理。

    video_streams: list[VideoStreamInfo] = []
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video":
            # 封面图（attached_pic）不是视频轨
            if int((stream.get("disposition") or {}).get("attached_pic") or 0) == 1:
                continue
            video_streams.append(_parse_video(stream))
        elif kind == "audio":
            info.audio_tracks.append(_parse_audio(stream))
        elif kind == "subtitle":
            info.subtitle_tracks.append(_parse_audio(stream))

    if video_streams:
        info.video = video_streams[0]
        if len(video_streams) > 1:
            info.issues.append(
                Issue(
                    LEVEL_WARN,
                    "MULTI_VIDEO_STREAM",
                    f"检测到 {len(video_streams)} 条视频轨，本版仅处理第一条。",
                    "其余视频轨不会出现在导出结果中。",
                )
            )

    if info.video is None:
        info.issues.append(
            Issue(LEVEL_BLOCK, "NO_VIDEO_STREAM", "文件中没有可用视频轨，无法分集。")
        )

    if not info.audio_tracks:
        info.issues.append(
            Issue(
                LEVEL_WARN,
                "NO_AUDIO_STREAM",
                "文件没有音轨。",
                "将跳过语音转写，仅使用画面分析与人工审核（§7.2）。",
            )
        )
    elif len(info.audio_tracks) > 1:
        info.issues.append(
            Issue(
                LEVEL_INFO,
                "MULTI_AUDIO_TRACK",
                f"检测到 {len(info.audio_tracks)} 条音轨，必须显式指定主对白轨。",
                "未选中的音轨不会参与对白分析。",
            )
        )

    if detect_vfr and info.video is not None:
        _detect_vfr(
            binaries,
            info,
            sample_seconds=vfr_sample_seconds,
            windows=vfr_sample_windows,
        )

    return info


def _parse_video(stream: dict) -> VideoStreamInfo:
    vs = VideoStreamInfo(index=int(stream.get("index", 0)))
    vs.codec_name = stream.get("codec_name", "") or ""
    vs.codec_long_name = stream.get("codec_long_name", "") or ""
    vs.width = int(stream.get("width") or 0)
    vs.height = int(stream.get("height") or 0)
    vs.pix_fmt = stream.get("pix_fmt", "") or ""
    vs.time_base = _safe_time_base(stream.get("time_base"))
    vs.r_frame_rate = _safe_rational(stream.get("r_frame_rate"))
    vs.avg_frame_rate = _safe_rational(stream.get("avg_frame_rate"))
    vs.start_pts = _safe_int(stream.get("start_pts"))
    vs.start_time = _safe_seconds(stream.get("start_time"))
    vs.duration = _safe_seconds(stream.get("duration"))
    vs.color_transfer = stream.get("color_transfer", "") or ""
    vs.color_primaries = stream.get("color_primaries", "") or ""
    vs.color_space = stream.get("color_space", "") or ""
    vs.field_order = stream.get("field_order", "") or ""
    vs.display_aspect_ratio = stream.get("display_aspect_ratio", "") or ""
    nb = stream.get("nb_frames")
    if nb not in (None, "", "N/A"):
        try:
            vs.nb_frames = int(nb)
        except (TypeError, ValueError):
            vs.nb_frames = None

    # 视频流没有 duration 时，用 nb_frames / fps 兜底
    if vs.duration is None and vs.nb_frames:
        fps = vs.nominal_fps
        if fps > 0:
            vs.duration = Fraction(vs.nb_frames) / fps
    return vs


def _parse_audio(stream: dict) -> AudioStreamInfo:
    a = AudioStreamInfo(index=int(stream.get("index", 0)))
    a.codec_name = stream.get("codec_name", "") or ""
    a.sample_rate = int(stream.get("sample_rate") or 0)
    a.channels = int(stream.get("channels") or 0)
    a.channel_layout = stream.get("channel_layout", "") or ""
    a.time_base = _safe_time_base(stream.get("time_base"))
    a.start_pts = _safe_int(stream.get("start_pts"))
    a.start_time = _safe_seconds(stream.get("start_time"))
    a.duration = _safe_seconds(stream.get("duration"))
    br = stream.get("bit_rate")
    if br not in (None, "", "N/A"):
        try:
            a.bit_rate = int(br)
        except (TypeError, ValueError):
            a.bit_rate = None
    tags = stream.get("tags") or {}
    a.tags = {str(k): str(v) for k, v in tags.items()}
    return a


def _detect_vfr(
    binaries: FfmpegBinaries,
    info: MediaInfo,
    *,
    sample_seconds: float,
    windows: int,
) -> None:
    """分层判定 VFR：先看名义帧率，可疑时抽样读真实帧 PTS。"""
    vs = info.video
    if vs is None:
        return

    reason = ""
    if vs.r_frame_rate and vs.avg_frame_rate and vs.r_frame_rate != vs.avg_frame_rate:
        diff = abs(vs.r_frame_rate - vs.avg_frame_rate)
        base = max(vs.r_frame_rate, vs.avg_frame_rate)
        if base > 0 and diff / base > _VFR_RATIO_TOLERANCE:
            reason = (
                f"r_frame_rate={vs.r_frame_rate} 与 avg_frame_rate={vs.avg_frame_rate} 不一致"
            )

    if vs.nb_frames is None:
        reason = reason or "容器未提供帧数"

    total = info.timeline_duration
    if total <= 0:
        return

    # 抽样读真实帧 PTS，确认是否真的存在不等间隔
    windows_pts, sample_error = _sample_frame_pts(binaries, info, sample_seconds, windows)

    if sample_error:
        # 抽样失败就不能声称"已确认是固定帧率"。宁可报告"未完成"，
        # 也不能把"当前没查到"说成"不存在"（§10.4）。
        info.issues.append(
            Issue(
                LEVEL_WARN,
                "VFR_CHECK_INCOMPLETE",
                "帧间隔抽样未能完成，已按固定帧率继续处理。",
                f"判定依据：{reason or '容器未提供帧数'}；抽样失败原因：{sample_error}",
            )
        )
        return

    total_frames = sum(len(item) for item in windows_pts)
    if total_frames >= 3:
        # 只在**单个窗口内**计算帧间隔。跨窗口的差值是两个抽样点之间的距离
        # （可能几十秒），拿它当帧间隔会造成整片被误判为 VFR。
        lo: Fraction | None = None
        hi: Fraction | None = None
        for item in windows_pts:
            for i in range(len(item) - 1):
                delta = item[i + 1] - item[i]
                if delta <= 0:
                    continue
                lo = delta if lo is None or delta < lo else lo
                hi = delta if hi is None or delta > hi else hi

        if lo is not None and hi is not None and (hi - lo) / hi > _VFR_RATIO_TOLERANCE:
            info.is_vfr = True
            info.vfr_evidence = (
                f"抽样 {total_frames} 帧（{len(windows_pts)} 个窗口），帧间隔在 "
                f"{float(lo)*1000:.2f}ms 与 {float(hi)*1000:.2f}ms 之间波动"
                f"（{reason or '不等间隔'}）"
            )
            info.issues.append(
                Issue(
                    LEVEL_WARN,
                    "VFR_SOURCE",
                    "检测到可变帧率（VFR）素材。",
                    "本版不支持 VFR 直接精确导出。可先生成固定帧率工作副本，"
                    "此时连续覆盖承诺针对该工作副本（§9.3）。" + info.vfr_evidence,
                )
            )
            return

    if reason:
        info.issues.append(
            Issue(
                LEVEL_INFO,
                "CFR_SUSPECT_CLEARED",
                "名义帧率不一致，但抽样帧间隔均匀，按固定帧率处理。",
                reason,
            )
        )


def _sample_frame_pts(
    binaries: FfmpegBinaries,
    info: MediaInfo,
    sample_seconds: float,
    windows: int,
) -> tuple[list[list[Fraction]], str]:
    """按窗口抽样读取帧 PTS，避免全片解码。

    返回 (每个窗口的帧 PTS 列表, 失败原因)。**逐窗口返回**是必需的：
    合并成一个列表后计算差值会把"两个抽样点之间的距离"误当成帧间隔。

    抽样结果只是一个"间隔是否均匀"的证据，**不是完整的帧 PTS 表**，
    因此不得用它做帧号映射。VFR 的完整帧表需要另行构建（§9.3）。
    """
    total = info.timeline_duration
    if total <= 0:
        return [], "总时长不可用"

    starts: list[float] = []
    seg = float(sample_seconds)
    span = float(total)
    if windows <= 1:
        starts = [0.0]
    else:
        # 均布采样窗口，末窗留出余量避免越过文件尾
        step = max(0.0, (span - seg * windows) / (windows - 1)) if span > seg * windows else seg
        starts = [min(span - seg * 0.5, i * step) for i in range(windows)]

    # ffprobe 的 -read_intervals 语法是 "START%+DURATION"。
    # 注意中间的 % 不能省略：写成 "START+DURATION" 会被判为非法区间并报错，
    # 而错误一旦被上层吞掉，VFR 探测就会永远静默失败。
    #
    # 逐窗口单独发起 ffprobe：这样每个窗口的帧序列天然是"相邻帧"，
    # 窗口内任意两帧之差都是真实的帧间隔，不会被窗口跨度污染。
    stream_index = info.video.index if info.video else 0
    per_window: list[list[Fraction]] = []
    for start in starts:
        interval = f"{max(0.0, start):.6f}%+{seg:.3f}"
        try:
            data = run_ffprobe_json(
                binaries,
                [
                    "-select_streams",
                    f"{stream_index}",
                    "-read_intervals",
                    interval,
                    "-show_entries",
                    "frame=pts_time",
                    "-of",
                    "json",
                    str(info.path),
                ],
                timeout=600.0,
            )
        except FfmpegError as exc:
            return [], f"区间 {interval} 读取失败（退出码 {exc.returncode}）：{exc.stderr_tail[:200]}"

        pts: list[Fraction] = []
        for frame in data.get("frames") or []:
            value = _safe_seconds(frame.get("pts_time"))
            if value is None:
                continue
            # pts_time 是流时间轴上的绝对时间戳，换算到"相对视频首帧"的时间轴秒
            pts.append(value - info.video_start_time)
        pts.sort()
        deduped: list[Fraction] = []
        for value in pts:
            if not deduped or value > deduped[-1]:
                deduped.append(value)
        if deduped:
            per_window.append(deduped)

    if not per_window:
        return [], "抽样区间内未读到任何帧"
    return per_window, ""


# ---------------------------------------------------------------------------
# 兼容性判定
# ---------------------------------------------------------------------------


def check_compatibility(info: MediaInfo, binaries: FfmpegBinaries) -> list[Issue]:
    """汇总 §2.2 / §9.3 / §20.2 要求的兼容性判定，返回完整问题清单。"""
    issues: list[Issue] = list(info.issues)
    vs = info.video

    if vs is None:
        return issues

    if vs.is_hdr:
        issues.append(
            Issue(
                LEVEL_WARN,
                "HDR_SOURCE",
                "检测到 HDR 或高位深素材。",
                f"pix_fmt={vs.pix_fmt} color_transfer={vs.color_transfer or '未标注'}。"
                "转换 SDR 必须作为显式选项并先看样片（§9.3）。",
            )
        )

    if vs.is_interlaced:
        issues.append(
            Issue(
                LEVEL_WARN,
                "INTERLACED_SOURCE",
                "检测到隔行扫描素材。",
                f"field_order={vs.field_order}，需要反交错处理，首版未包含该预设。",
            )
        )

    # 编码器可用性（§6：导入阶段就要发现缺失）
    missing = [name for name in ("libx264", "aac") if name not in binaries.encoders]
    if missing:
        issues.append(
            Issue(
                LEVEL_BLOCK,
                "ENCODER_MISSING",
                f"当前 FFmpeg 缺少编码器: {', '.join(missing)}。",
                "精确导出需要 libx264 与 aac。请更换 FFmpeg 构建版本。",
            )
        )

    # 音视频尾部不一致（§9.3：不能无提示使用 shortest）
    if info.duration and vs.duration:
        audio_max = max((a.duration for a in info.audio_tracks if a.duration), default=None)
        if audio_max is not None:
            gap = abs(vs.duration - audio_max)
            if gap > Fraction(1, 2):
                issues.append(
                    Issue(
                        LEVEL_WARN,
                        "AV_DURATION_MISMATCH",
                        "音视频时长不一致。",
                        f"视频 {float(vs.duration):.3f}s，"
                        f"最长音轨 {float(audio_max):.3f}s，差 {float(gap)*1000:.0f}ms。"
                        "分集范围以视频时长为准，导出不会自动补黑帧或截断音轨。",
                    )
                )

    # 非零起始 PTS
    offset = info.seek_offset_seconds
    if offset > 0:
        issues.append(
            Issue(
                LEVEL_INFO,
                "NONZERO_START_PTS",
                f"源片起始 PTS 非零（视频相对容器偏移 {float(offset)*1000:.1f}ms）。",
                "时间轴已按视频首帧归一化，导出时会自动换算 -ss 偏移。",
            )
        )

    if vs.start_time is not None and vs.start_time < 0:
        issues.append(
            Issue(
                LEVEL_WARN,
                "NEGATIVE_START_PTS",
                f"视频流起始 PTS 为负（{vs.start_time}）。",
                "已按首帧归一化处理，但建议核查源片是否经过非标准封装。",
            )
        )

    if vs.nb_frames is None:
        issues.append(
            Issue(
                LEVEL_WARN,
                "FRAME_COUNT_UNKNOWN",
                "容器未提供帧数，无法直接做帧数守恒校验。",
                "可在导出后使用深度校验（重新计数）确认。",
            )
        )

    if info.duration is None:
        issues.append(
            Issue(
                LEVEL_BLOCK,
                "DURATION_UNKNOWN",
                "无法读取总时长，不能进行分集规划。",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _safe_seconds(value) -> Fraction | None:
    try:
        return parse_seconds(value)
    except TimeBaseError:
        return None


def _safe_rational(value) -> Fraction | None:
    try:
        result = parse_rational(value)
    except TimeBaseError:
        return None
    return result if result > 0 else None


def _safe_time_base(value) -> TimeBase | None:
    try:
        return parse_time_base(value)
    except TimeBaseError:
        return None


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_media(info: MediaInfo) -> str:
    """生成导入页的一行摘要文本。"""
    vs = info.video
    if vs is None:
        return f"{info.path.name}｜无视频轨"
    fps = float(vs.nominal_fps)
    parts = [
        info.path.name,
        f"{vs.width}×{vs.height}",
        f"{fps:.3f}fps" + ("(VFR)" if info.is_vfr else ""),
        vs.codec_name.upper(),
        vs.pix_fmt,
        f"时长 {format_timecode(info.timeline_duration)}",
        f"{len(info.audio_tracks)} 条音轨",
    ]
    return "｜".join(parts)
