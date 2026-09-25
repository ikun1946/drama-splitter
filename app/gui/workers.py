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
    completed = Signal(object, object, object, object, object)  # 方案,报告,问题,候选,整集复核
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
            from app.core.semantic import EventIndex, create_judge, judge_candidates
            from app.core.settings import CountPolicy, SplitMode, check_feasibility
            from app.core.solver import solve_from_candidates

            index = EventIndex.build(transcript, shots.scenes)
            self.stage_log.emit(index.describe())

            # ---- 语义判断（§11，可选，带预算与降级）--------------------
            judge = create_judge()
            if judge is None:
                self.stage_log.emit(
                    "语义判断未启用（本地模型或推理栈不可用）——"
                    "候选保持规则评分，审核状态保持 pending（§ 降级路径）。"
                )
            strategy = self._settings.strategy.value
            outcome = judge_candidates(
                candidates, index, judge, strategy=strategy, budget_requests=24
            )
            self.stage_log.emit(outcome.describe())
            for note in outcome.notes:
                self.stage_log.emit(f"  {note}")

            def baseline():
                """§20.3 的规则基线（等分），用于与 DP 结果对照。"""
                return plan_rule_based(self._media, self._settings)[0]

            report = check_feasibility(self._settings, self._media.timeline_duration)
            if not candidates.is_empty and not report.blocking and report.derived is not None:
                exact = (
                    self._settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
                    and self._settings.count_policy == CountPolicy.EXACT
                )
                # 问句/叹句后的切点帧（悬念策略用，§ 策略）
                ends_question = {
                    point.frame_index: bool(
                        (point.speech_before or "").strip()
                        and (point.speech_before or "").strip()[-1] in "？！?"
                    )
                    for point in candidates.points
                }
                try:
                    plan, solution, problems, notes = solve_from_candidates(
                        candidates,
                        self._media,
                        report.derived,
                        settings_count_exact=exact,
                        target_episode_count=self._settings.target_episode_count
                        or report.allowed_min,
                        allowed_min=report.allowed_min,
                        allowed_max=report.allowed_max,
                        baseline=baseline,
                        strategy=strategy,
                        ends_with_question=ends_question,
                    )
                except Exception as exc:  # noqa: BLE001 - 含 PlanningError
                    self.failed.emit(str(exc))
                    return

                blocking = [p for p in problems if p.is_blocking]
                if blocking:
                    # §4.2：无解必须明确报错，不能把空方案当结果交给用户。
                    # 典型成因：对白句末不落在目标时长允许的窗口内（§10.4）。
                    self.failed.emit(
                        blocking[0].message + " " + blocking[0].describe()
                    )
                    return

                self.stage_log.emit("边界由候选图动态规划求得（阶段2 §10.2）。")
                for note in notes:
                    self.stage_log.emit(f"  {note}")
            else:
                if candidates.is_empty:
                    reason = "候选点为空，回退到规则等分方案（§6.1 兜底路径）。"
                else:
                    first = report.issues[0] if report.issues else None
                    reason = f"参数无解（{first.message if first else '未知'}），跳过动态规划。"
                self.stage_log.emit(reason)
                try:
                    plan, report, problems = plan_rule_based(self._media, self._settings)
                except PlanningError as exc:
                    self.failed.emit(str(exc))
                    return
            # ---- 整集复核（阶段4）--------------------------------------
            from app.core.review import review_plan

            reviews = review_plan(
                plan, self._media, index,
                transcript=transcript, blacks=shots.blacks,
            )
            grade_labels = {"pass": "通过", "warn": "留意", "block": "阻断"}
            for review in reviews:
                detail = "；".join(review.risks) if review.risks else "无风险"
                self.stage_log.emit(
                    f"  第{review.episode:02d}集复核[{grade_labels[review.grade]}]：{detail}"
                )

            self.completed.emit(plan, report, problems, candidates, reviews)

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
        transcript=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exporter = Exporter(binaries, media, preset)
        self._plan = plan
        self._output_root = Path(output_root)
        self._audio_stream_index = audio_stream_index
        self._only = only_episodes
        # 转写可选：有则导出单集 SRT（§7.2），没有就如实说明而不是静默跳过
        self._transcript = transcript
        self.subtitle_results: list = []

    def _export_subtitles(self) -> None:
        """导出各集 SRT 字幕（§7.2 单集字幕以 SRT 为主）。

        只在导出成功后调用；无转写时记一条说明，不产出空文件。
        """
        from app.core.episode_files import export_episode_subtitles

        directory = (
            self._output_root / "episodes" / self._plan.export_directory_name()
        )
        try:
            self.subtitle_results = export_episode_subtitles(
                self._plan, self._transcript, directory
            )
        except OSError as exc:
            self.subtitle_results = []
            self.failed.emit(f"字幕导出失败：{exc}")

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


class ModelDownloadWorker(QThread):
    """模型下载（后台线程，可取消，进度按已下载字节回报）。

    为什么不做成同步调用：whisper large-v3 是 3GB，同步会冻结界面几分钟。
    """

    progress = Signal(int, int, str, int, int)  # 已下载字节, 总字节, 当前文件, 第几个, 共几个
    log = Signal(str)
    finished_with = Signal(object)  # DownloadResult

    def __init__(self, spec, models_root, *, endpoint: str = "https://hf-mirror.com",
                 parent=None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._models_root = models_root
        self._endpoint = endpoint
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102
        from app.core.model_manager import download_model

        try:
            result = download_model(
                self._spec,
                self._models_root,
                endpoint=self._endpoint,
                on_progress=lambda done, total, name, index, count: self.progress.emit(
                    done, total, name, index, count
                ),
                cancel_check=lambda: self._cancel,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须变成可读结果
            from app.core.model_manager import DownloadResult

            result = DownloadResult(
                spec=self._spec, ok=False, message=f"下载过程异常：{exc}"
            )
        self.log.emit(result.describe())
        self.finished_with.emit(result)
