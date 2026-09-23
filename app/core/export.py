"""批量导出与质量检查。

设计依据：
- §14.1 默认精确裁切：按确认的源帧/时间边界解码并重新编码。保持源片画幅与方向，
  不默认缩放或补帧。FFmpeg 输入端定位配合重新编码可丢弃多出的前段，而流复制会保留，
  因此"只写 FFmpeg 按时间裁切"不足以保证审核切点与输出切点一致。
- §14.2 快速复制第一阶段不提供，避免为速度引入时间错位。
- §14.4 导出事务：冻结方案版本；先写临时文件，校验通过后命名；不静默覆盖；
  失败保留成功集数，只重试失败项；切点改变后相邻集标记过期。
- §14.5 校验分计划层、视频层、边界层、音频层、时长层。
- §15.2 分阶段运行，限制同时运行的编码任务数。
"""

from __future__ import annotations

import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable

from .ffmpeg import FfmpegBinaries, FfmpegError, run_ffmpeg, run_ffprobe_json
from .plan import BoundaryPlan, Episode
from .probe import MediaInfo
from .timebase import format_seconds_brief, format_timecode, parse_seconds

__all__ = [
    "CutMode",
    "ExportPreset",
    "ExportJob",
    "ExportResult",
    "ExportBatchResult",
    "Exporter",
    "verify_episode_output",
]


class CutMode(str, Enum):
    """§14.1 / §14.2 裁切方式。"""

    PRECISE = "precise"      # 精确裁切（默认）
    FAST_COPY = "fast_copy"  # 快速复制（第一阶段不提供）


@dataclass
class ExportPreset:
    """§14.1 常规兼容预设：MP4 + H.264 + AAC。"""

    container: str = "mp4"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    pix_fmt: str | None = None  # None = 沿用源片，不默认转换
    keep_faststart: bool = True
    extra_output_args: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.container.upper()} / {self.video_codec} CRF{self.crf} / {self.audio_codec}"

    def to_json(self) -> dict:
        return {
            "container": self.container,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "crf": self.crf,
            "preset": self.preset,
            "audio_bitrate": self.audio_bitrate,
            "pix_fmt": self.pix_fmt,
        }


@dataclass
class ExportJob:
    """一集的导出任务。"""

    episode: Episode
    output_path: Path
    time_base_seconds: Fraction
    seek_offset: Fraction
    frame_count: int
    audio_stream_index: int | None

    @property
    def duration_seconds(self) -> Fraction:
        return self.episode.duration_seconds


@dataclass
class ExportResult:
    episode_index: int
    output_path: Path | None
    success: bool
    message: str = ""
    verified: bool = False
    warnings: list[str] = field(default_factory=list)
    actual_frame_count: int | None = None
    expected_frame_count: int | None = None
    actual_duration: Fraction | None = None

    def to_json(self) -> dict:
        return {
            "episode": self.episode_index,
            "success": self.success,
            "output": str(self.output_path) if self.output_path else None,
            "verified": self.verified,
            "message": self.message,
            "warnings": list(self.warnings),
            "expected_frame_count": self.expected_frame_count,
            "actual_frame_count": self.actual_frame_count,
            "actual_duration_seconds": (
                f"{self.actual_duration.numerator}/{self.actual_duration.denominator}"
                if self.actual_duration
                else None
            ),
        }


@dataclass
class ExportBatchResult:
    plan_directory: Path
    results: list[ExportResult] = field(default_factory=list)
    plan_layer_problems: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def succeeded(self) -> list[ExportResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[ExportResult]:
        return [r for r in self.results if not r.success]

    @property
    def all_succeeded(self) -> bool:
        return not self.failed and not self.cancelled

    def summary(self) -> str:
        if self.cancelled:
            return f"已取消：完成 {len(self.succeeded)}/{len(self.results)} 集"
        return f"成功 {len(self.succeeded)}/{len(self.results)} 集，失败 {len(self.failed)} 集"


class Exporter:
    """按冻结的方案版本导出成片。"""

    def __init__(
        self,
        binaries: FfmpegBinaries,
        media: MediaInfo,
        preset: ExportPreset | None = None,
        *,
        max_parallel: int = 1,
        cut_mode: CutMode = CutMode.PRECISE,
    ) -> None:
        if cut_mode is CutMode.FAST_COPY:
            # §14.2 第一阶段不提供快速复制，避免为速度引入时间错位。
            # 这里明确拒绝而非静默降级，防止用户在不知情的情况下拿到关键帧错位的成片。
            raise NotImplementedError(
                "快速复制模式在当前版本未启用。关键帧边界与审核切点可能不一致，"
                "本版仅提供精确裁切（重新编码）。"
            )
        self.binaries = binaries
        self.media = media
        self.preset = preset or ExportPreset()
        self.cut_mode = cut_mode
        # §15.2 限制同时运行的编码任务数；默认串行，避免长片导出打满机器
        self.max_parallel = max(1, max_parallel)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------
    # 命令构造
    # ------------------------------------------------------------------

    def build_command(self, job: ExportJob, tmp_path: Path) -> list[str]:
        """构造一次精确裁切命令。

        关键点（§14.1）：
        - `-ss` 作为**输入选项**：先定位到目标前一个可寻址位置，重新编码时
          自动丢弃多出的前段，输出即从目标帧开始。
        - `seek_offset`：源片非零起始 PTS 的换算，保证审核切点与输出切点一致。
        - `-frames:v N`：视频帧数**只**由它决定。
        - 音频用 `atrim` 精确截断。

        为什么不用 `-t`（实测结论，见 tools/diagnose_seek2.py）
        ------------------------------------------------------
        源片起始 PTS 非零时，`-ss` 会让输出时间轴整体平移（首帧落在 0.04s 或
        0.08s 而非 0）。`-t` 是从**首个输出时间戳**起算的，于是每次都少切最后一帧：
        实测起点帧 0 切出 0–498（499 帧），起点帧 750 切出 750–1248（499 帧）。
        改用 `-frames:v` 单点控制后，两种情况都精确得到 500 帧。
        在起始 PTS 为 0 的素材上这个错误会被完全掩盖，因此必须用非零起始
        PTS 的测试素材才能暴露。

        Why not `-avoid_negative_ts make_zero`（实测结论，见 tools/diagnose_dup.py）
        ----------------------------------------------------------------------
        该选项按**包时间戳**把时间轴对齐到 0，而 AAC 编码器延迟使包时间戳与
        解码样本时间戳相差约一帧（1024/48000 ≈ 21.3ms），于是音频被整体前移。
        实测：非零起始 PTS 素材上，不带该选项的音画偏差为 9.9ms，
        带上之后恶化到 31.2ms（一帧 = 40ms）。因此本实现不做时间戳平移，
        改用 tools/verify.measure_av_sync() 直接量化偏差并守住一帧的门槛。

        - 不做缩放、不补帧、不归一到 SDR（§14.1、§9.3）。
        """
        if job.frame_count <= 0:
            raise ValueError(f"第{job.episode.index:02d}集帧数为 {job.frame_count}，无法导出")

        seek = job.episode.start_seconds + job.seek_offset
        span = self.media.frame_span_seconds(
            self.media.frame_index_at_or_after(job.episode.start_seconds),
            job.frame_count,
        )

        args: list[str] = [
            "-ss",
            _decimal(seek, 6),
            "-i",
            str(self.media.path),
            # 本版只处理第一条视频轨（§2.2），多视频轨已在导入阶段给出警告
            "-map",
            "0:v:0",
        ]
        if job.audio_stream_index is not None:
            args += ["-map", f"0:a:{_audio_ordinal(self.media, job.audio_stream_index)}"]

        args += ["-frames:v", str(job.frame_count)]

        if job.audio_stream_index is not None:
            # 音频严格截到与视频同跨度，并把采样起点归零
            args += [
                "-af",
                f"asetpts=PTS-STARTPTS,atrim=end={_decimal(span, 6)},asetpts=PTS-STARTPTS",
                "-c:a",
                self.preset.audio_codec,
                "-b:a",
                self.preset.audio_bitrate,
            ]

        args += [
            "-c:v",
            self.preset.video_codec,
            "-preset",
            self.preset.preset,
            "-crf",
            str(self.preset.crf),
        ]
        if self.preset.pix_fmt:
            args += ["-pix_fmt", self.preset.pix_fmt]
        if self.media.video and self.media.video.color_primaries:
            # 保留源片色彩标记，避免播放器色彩解释变化
            args += [
                "-color_primaries",
                self.media.video.color_primaries,
            ]

        if self.preset.container == "mp4" and self.preset.keep_faststart:
            args += ["-movflags", "+faststart"]
        args += list(self.preset.extra_output_args)
        args += ["-y", str(tmp_path)]
        return args

    # ------------------------------------------------------------------
    # 单集导出
    # ------------------------------------------------------------------

    def export_episode(
        self,
        job: ExportJob,
        *,
        on_progress: Callable[[float], None] | None = None,
        deep_verify: bool = False,
    ) -> ExportResult:
        """导出一集：写临时文件 → 基础校验 → 原子改名（§14.4）。"""
        result = ExportResult(
            episode_index=job.episode.index,
            output_path=None,
            success=False,
            expected_frame_count=job.frame_count,
        )

        final_path = job.output_path
        tmp_path = final_path.with_name(final_path.stem + ".part" + final_path.suffix)

        if final_path.exists():
            # 不静默覆盖：目标已存在时中止，由上层决定是否换版本目录或显式替换
            result.message = (
                f"目标文件已存在，未覆盖：{final_path.name}。"
                "请改用新的方案版本目录或显式选择替换。"
            )
            return result

        final_path.parent.mkdir(parents=True, exist_ok=True)
        if tmp_path.exists():
            tmp_path.unlink()

        try:
            args = self.build_command(job, tmp_path)
            run_ffmpeg(
                self.binaries,
                args,
                on_progress=on_progress,
                duration_seconds=float(job.duration_seconds),
                cancel_check=self._cancel.is_set,
            )
        except RuntimeError:
            _safe_unlink(tmp_path)
            result.message = "已取消"
            return result
        except (FfmpegError, ValueError) as exc:
            _safe_unlink(tmp_path)
            result.message = str(exc)
            return result

        # 基础校验：文件存在且非空
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            _safe_unlink(tmp_path)
            result.message = "导出未产生可用文件"
            return result

        try:
            verification = verify_episode_output(
                self.binaries,
                tmp_path,
                expected_frames=job.frame_count,
                expected_duration=job.duration_seconds,
                deep=deep_verify,
            )
        except FfmpegError as exc:
            _safe_unlink(tmp_path)
            result.message = f"产物校验失败：{exc}"
            return result

        result.warnings = verification.warnings
        result.actual_frame_count = verification.frame_count
        result.actual_duration = verification.duration

        if verification.blocking_problem:
            _safe_unlink(tmp_path)
            result.message = verification.blocking_problem
            return result

        # 校验通过，原子改名为最终文件
        try:
            tmp_path.replace(final_path)
        except OSError as exc:
            _safe_unlink(tmp_path)
            result.message = f"重命名失败：{exc}"
            return result

        result.output_path = final_path
        result.success = True
        result.verified = verification.frame_count is not None
        result.message = "完成"
        return result

    # ------------------------------------------------------------------
    # 批量导出
    # ------------------------------------------------------------------

    def prepare_jobs(
        self,
        plan: BoundaryPlan,
        output_root: Path,
        *,
        audio_stream_index: int | None,
    ) -> list[ExportJob]:
        """按冻结方案生成导出任务清单（§14.4 冻结版本）。"""
        directory = output_root / "episodes" / plan.export_directory_name()
        counts = plan.frame_counts(self.media)
        jobs: list[ExportJob] = []
        for episode, count in zip(plan.episodes(), counts):
            jobs.append(
                ExportJob(
                    episode=episode,
                    output_path=directory / f"第{episode.index:02d}集.{self.preset.container}",
                    time_base_seconds=self.media.video_time_base.seconds_per_tick,
                    seek_offset=self.media.seek_offset_seconds,
                    frame_count=count,
                    audio_stream_index=audio_stream_index,
                )
            )
        return jobs

    def export_plan(
        self,
        plan: BoundaryPlan,
        output_root: Path,
        *,
        audio_stream_index: int | None,
        on_episode_done: Callable[[ExportResult], None] | None = None,
        only_episodes: Iterable[int] | None = None,
        retry_failed: int = 2,
    ) -> ExportBatchResult:
        """导出整个方案。失败只重试失败项，成功集数保留（§14.4）。"""
        batch = ExportBatchResult(plan_directory=output_root / "episodes" / plan.export_directory_name())

        for problem in plan.validate():
            if problem.is_blocking:
                batch.plan_layer_problems.append(problem.describe())

        # 帧对齐是正确性前提：不对齐会让相邻集重叠若干帧，且帧数之和不再守恒
        aligned, offenders = plan.is_frame_aligned(self.media)
        if not aligned:
            for index in offenders:
                seconds = plan.time_base.ticks_to_seconds(plan.boundary_ticks[index])
                batch.plan_layer_problems.append(
                    f"[FRAME_ALIGNMENT] 边界 #{index}（{format_timecode(seconds)}）"
                    "未落在合法帧起点上，会导致该集与相邻集重叠。"
                    "请先调用 snap_to_frames() 吸附到帧边界。"
                )

        if batch.plan_layer_problems:
            return batch

        jobs = self.prepare_jobs(plan, output_root, audio_stream_index=audio_stream_index)
        if only_episodes is not None:
            wanted = set(only_episodes)
            jobs = [j for j in jobs if j.episode.index in wanted]

        pending = list(jobs)
        attempt = 0
        results: dict[int, ExportResult] = {}

        while pending and attempt <= retry_failed:
            if self._cancel.is_set():
                batch.cancelled = True
                break
            if attempt > 0:
                pending = [j for j in pending if j.episode.index in
                           {r.episode_index for r in results.values() if not r.success}]

            with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                futures = {pool.submit(self.export_episode, job): job for job in pending}
                for future in as_completed(futures):
                    result = future.result()
                    results[result.episode_index] = result
                    if on_episode_done:
                        on_episode_done(result)
            attempt += 1

        batch.results = [results[i] for i in sorted(results)]
        return batch

    # ------------------------------------------------------------------
    # 过期标记
    # ------------------------------------------------------------------

    @staticmethod
    def mark_stale(directory: Path, episodes: Iterable[int]) -> list[Path]:
        """§14.4 切点改变后，相邻受影响集的既有产物标记过期。

        采用改名而非删除：用户可能已经发布了成片，不能被工具静默销毁。
        """
        marked: list[Path] = []
        if not directory.exists():
            return marked
        for index in episodes:
            for path in directory.glob(f"第{index:02d}集.*"):
                if path.name.endswith(".stale"):
                    continue
                target = path.with_name(path.name + ".stale")
                try:
                    path.replace(target)
                except OSError:
                    continue
                marked.append(target)
        return marked


# ---------------------------------------------------------------------------
# §14.5 产物校验
# ---------------------------------------------------------------------------


@dataclass
class OutputVerification:
    frame_count: int | None = None
    duration: Fraction | None = None
    warnings: list[str] = field(default_factory=list)
    blocking_problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocking_problem


def verify_episode_output(
    binaries: FfmpegBinaries,
    path: Path,
    *,
    expected_frames: int | None = None,
    expected_duration: Fraction | None = None,
    deep: bool = False,
    frame_tolerance: int = 0,
) -> OutputVerification:
    """校验单集成片（§14.5 视频层与时长层）。

    deep=True 时使用 `-count_frames` 真实解码计数；否则读容器头部 nb_frames。
    帧数校验是硬判据：说明各集是否少帧或多帧。时长校验只做提示，
    因为封装显示时长会受到音频帧填充影响（§14.5 时长层）。
    """
    verification = OutputVerification()

    args = ["-select_streams", "v:0", "-show_streams", "-show_format", str(path)]
    if deep:
        args = ["-select_streams", "v:0", "-count_frames", "-show_streams", "-show_format", str(path)]

    data = run_ffprobe_json(binaries, args, timeout=1800.0 if deep else 120.0)

    stream = None
    for item in data.get("streams") or []:
        if item.get("codec_type") == "video":
            stream = item
            break

    if stream is None:
        verification.blocking_problem = "产物中没有视频轨"
        return verification

    raw_frames = stream.get("nb_read_frames") if deep else stream.get("nb_frames")
    if raw_frames in (None, "", "N/A"):
        verification.warnings.append("无法从产物读取帧数，未做帧数守恒校验。")
    else:
        verification.frame_count = int(raw_frames)

    fmt = data.get("format") or {}
    try:
        verification.duration = parse_seconds(fmt.get("duration"))
    except Exception:  # noqa: BLE001 - 时长为可选项，解析失败不阻断
        verification.duration = None

    if expected_frames is not None and verification.frame_count is not None:
        delta = verification.frame_count - expected_frames
        if abs(delta) > frame_tolerance:
            verification.blocking_problem = (
                f"帧数不符：期望 {expected_frames} 帧，实际 {verification.frame_count} 帧"
                f"（差 {delta:+d} 帧）。"
            )

    if expected_duration is not None and verification.duration is not None:
        # 音频帧填充会让封装时长略长，容忍一个小量级并仅作提示
        gap = abs(verification.duration - expected_duration)
        if gap > Fraction(1, 4):
            verification.warnings.append(
                f"封装时长与计划相差 {float(gap)*1000:.0f}ms"
                f"（计划 {format_seconds_brief(expected_duration)}，"
                f"实际 {format_seconds_brief(verification.duration)}）。"
            )

    return verification


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _decimal(value: Fraction, digits: int) -> str:
    """把精确 Fraction 转成固定小数位字符串，用于 FFmpeg 参数。

    FFmpeg 只接受十进制数值，因此这里必须经过十进制化；用足够多的小数位
    （默认 6 位，微秒级）把误差压到远小于一帧（§9.1 禁止累加四舍五入秒数，
    但单个参数仍需十进制表示）。
    """
    scale = 10**digits
    scaled = value.numerator * scale // value.denominator
    sign = "-" if scaled < 0 else ""
    scaled = abs(scaled)
    text = str(scaled).rjust(digits + 1, "0")
    return f"{sign}{text[:-digits]}.{text[-digits:]}"


def _audio_ordinal(media: MediaInfo, stream_index: int) -> int:
    """容器全局流号 → a:N 中的 N（类型内序号）。"""
    ordinals = [a.index for a in media.audio_tracks]
    try:
        return ordinals.index(stream_index)
    except ValueError as exc:
        raise ValueError(
            f"音轨 {stream_index} 不在已识别的音轨列表中：{ordinals}"
        ) from exc


def _safe_unlink(path: Path) -> None:
    """删除临时产物。

    只用于本工具自己刚创建的 .part 文件，绝不触碰用户既有文件。
    """
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def write_episode_plan_json(plan: BoundaryPlan, path: Path) -> None:
    """§18.2 data/episode_plan.json —— 机器可读的完整边界方案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.to_json()
    payload["time_base"] = plan.time_base.to_string()
    payload["total_ticks"] = plan.total_ticks
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_episode_plan_csv(plan: BoundaryPlan, path: Path) -> None:
    """§18.2 data/episode_plan.csv —— 人工可核对的集数与时间表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["集号", "起始时间码", "结束时间码", "时长(秒)", "起始ticks", "结束ticks", "例外"])
        for ep in plan.episodes():
            writer.writerow(
                [
                    ep.index,
                    format_timecode(ep.start_seconds),
                    format_timecode(ep.end_seconds),
                    f"{float(ep.duration_seconds):.3f}",
                    ep.start_ticks,
                    ep.end_ticks,
                    plan.exceptions[ep.index].reason if ep.index in plan.exceptions else "",
                ]
            )
    tmp.replace(path)


def copy_source_for_reference(media: MediaInfo, target: Path) -> None:
    """把源片路径写入引用文件，不复制实体（素材不出机器的前提下留痕）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(str(media.path), encoding="utf-8")
    tmp.replace(target)
