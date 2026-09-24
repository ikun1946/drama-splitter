"""后台工作线程。

设计依据（§6）：转写、视频解码与编码不得阻塞界面线程。
所有耗时操作（探测、规划、导出）都在这里跑，界面只接信号。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.export import ExportBatchResult, ExportPreset, Exporter
from ..core.ffmpeg import check_encoders
from ..core.plan import BoundaryPlan
from ..core.planner import PlanningError, plan_rule_based
from ..core.probe import MediaInfo, check_compatibility, probe_media
from ..core.settings import SplitSettings

__all__ = ["ProbeWorker", "PlanWorker", "ExportWorker"]


class ProbeWorker(QThread):
    """媒体探测 + 兼容性判定（§17.1，含解码可用性检查）。"""

    completed = Signal(object, list)   # MediaInfo, list[Issue]
    failed = Signal(str)

    def __init__(self, binaries, path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._binaries = binaries
        self._path = Path(path)

    def run(self) -> None:  # noqa: D102
        try:
            check_encoders(self._binaries)
            info: MediaInfo = probe_media(self._binaries, self._path)
            issues = check_compatibility(info, self._binaries)
            self.completed.emit(info, issues)
        except Exception as exc:  # noqa: BLE001 - 边界层必须把任何异常变成用户可读信息
            self.failed.emit(str(exc))


class PlanWorker(QThread):
    """生成规则草案方案。

    阶段1 使用规则规划器（等分 + 帧吸附）。替换路径按方案 §16 分三步：
    阶段3 提供候选数据库 → 阶段2 在候选上建图做动态规划 → 阶段4 加入剧情边代价。
    规则结果届时保留为对照基线（§20.3）。
    """

    completed = Signal(object, object, list)  # BoundaryPlan, FeasibilityReport, list[PlanProblem]
    failed = Signal(str)

    def __init__(self, media: MediaInfo, settings: SplitSettings, parent=None) -> None:
        super().__init__(parent)
        self._media = media
        self._settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            plan, report, problems = plan_rule_based(self._media, self._settings)
            self.completed.emit(plan, report, problems)
        except PlanningError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"规划失败：{exc}")


class AnalysisWorker(QThread):
    """阶段3 分析流水线：字幕 → 语音转写 → 镜头与黑场 → 候选数据库。

    各步骤都走缓存（§12.2），因此重复分析或换参数后重跑都很快。
    每一步的产出都完整保留在结果里，供后续页与阶段2 使用。
    """

    stage_started = Signal(str)
    stage_progress = Signal(str, float)          # 说明, 0-1
    stage_log = Signal(str)                      # 一行人可读的结论
    completed = Signal(object, object, object, object)
    failed = Signal(str)

    def __init__(self, binaries, media: MediaInfo, audio_stream_index: int | None,
                 settings: SplitSettings, parent=None) -> None:
        super().__init__(parent)
        self._binaries = binaries
        self._media = media
        self._audio_stream_index = audio_stream_index
        self._settings = settings
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    # ---- 各阶段 --------------------------------------------------------

    def run(self) -> None:  # noqa: D102
        from fractions import Fraction
        from pathlib import Path

        from app.core import subtitles as subs
        from app.core.asr import AsrEngine, AsrSettings
        from app.core.cache import CacheStore, SourceFingerprint
        from app.core.shots import detect_all
        from app.core.candidates import build_candidate_set

        try:
            project_root = Path(__file__).resolve().parent.parent.parent
            cache_dir = project_root / "cache"
            store = CacheStore(cache_dir)
            fingerprint = SourceFingerprint.of(self._media.path)
            ffmpeg = str(self._binaries.ffmpeg)

            transcript = None
            subtitle_tracks = []

            # ---- 字幕 --------------------------------------------------
            self.stage_started.emit("字幕解析")
            sidecars = subs.find_sidecar_subtitles(self._media.path)
            for sidecar in sidecars:
                track = subs.load_subtitle_file(sidecar)
                subtitle_tracks.append(track)
                self.stage_log.emit(f"{sidecar.name}: {track.describe()}")
            if not subtitle_tracks:
                self.stage_log.emit("未找到同名外挂字幕（SRT/VTT/ASS）。")

            # ---- 语音转写 ----------------------------------------------
            self.stage_started.emit("语音转写")
            settings = AsrSettings()
            if not settings.vad_filter:
                self.stage_log.emit("VAD 已关闭。")
            engine = AsrEngine(settings, cache=store, fingerprint=fingerprint)
            try:
                transcript, hit = engine.transcribe_cached(
                    self._media.path,
                    fingerprint,
                    store,
                    audio_stream_index=self._audio_stream_index,
                    duration_seconds=self._media.timeline_duration,
                    on_progress=lambda ratio, text: self.stage_progress.emit(text, ratio),
                    cancel_check=lambda: self._cancel,
                    ffmpeg=ffmpeg,
                )
            except RuntimeError as exc:
                self.stage_log.emit(f"跳过语音转写：{exc}")
            else:
                if hit:
                    self.stage_log.emit("转写命中缓存。")
                self.stage_log.emit(transcript.describe())
                for issue in transcript.issues:
                    self.stage_log.emit(f"  风险 {issue.code}：{issue.message}")
            if self._cancel:
                self.failed.emit("已取消")
                return

            # ---- 镜头与黑场 --------------------------------------------
            self.stage_started.emit("镜头与黑场")
            shots = detect_all(
                self._media.path, ffmpeg, cache=store, fingerprint=fingerprint,
            )
            self.stage_log.emit(shots.describe())

            # ---- 候选点 ------------------------------------------------
            self.stage_started.emit("候选点")
            duration = self._media.timeline_duration
            snap = self._media.snap_to_frame
            candidates = build_candidate_set(
                transcript=transcript,
                shots=shots.scenes,
                blacks=shots.blacks,
                timeline_duration=duration,
                frame_index_at_or_after=self._media.frame_index_at_or_after,
                snap_to_frame=snap,
                merge_distance=Fraction(1),
                per_window=12,
                window_seconds=Fraction(60),
            )
            self.stage_log.emit(candidates.describe())
            for note in candidates.limitations:
                self.stage_log.emit(f"  限流：{note}")
            for issue in candidates.issues:
                self.stage_log.emit(f"  {issue.code}：{issue.message}")

            # ---- 整体规划 ----------------------------------------------
            self.stage_started.emit("整体规划")
            from app.core.planner import PlanningError, plan_rule_based

            try:
                plan, report, problems = plan_rule_based(self._media, self._settings)
            except PlanningError as exc:
                self.failed.emit(str(exc))
                return
            self.completed.emit(plan, report, problems, candidates)

        except Exception as exc:  # noqa: BLE001 - 边界层必须把异常变成可读信息
            self.failed.emit(f"分析失败：{exc}")


class ExportWorker(QThread):
    """批量导出（§14.4 事务式导出、失败重试、可取消）。"""

    episode_done = Signal(object)   # ExportResult
    completed = Signal(object)      # ExportBatchResult
    failed = Signal(str)

    def __init__(
        self,
        binaries,
        media: MediaInfo,
        plan: BoundaryPlan,
        output_root: Path,
        audio_stream_index: int | None,
        preset: ExportPreset,
        *,
        only_episodes: list[int] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exporter = Exporter(binaries, media, preset)
        self._plan = plan
        self._output_root = Path(output_root)
        self._audio_stream_index = audio_stream_index
        self._only = only_episodes

    @property
    def exporter(self) -> Exporter:
        return self._exporter

    def cancel(self) -> None:
        self._exporter.cancel()

    def run(self) -> None:  # noqa: D102
        try:
            batch: ExportBatchResult = self._exporter.export_plan(
                self._plan,
                self._output_root,
                audio_stream_index=self._audio_stream_index,
                on_episode_done=self.episode_done.emit,
                only_episodes=self._only,
            )
            self.completed.emit(batch)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"导出失败：{exc}")
