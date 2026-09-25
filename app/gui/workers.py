"""后台工作线程。

设计依据（§6）：转写、视频解码与编码不得阻塞界面线程。
所有耗时操作（探测、规划、导出）都在这里跑，界面只接信号。

统一基类
--------
:class:`BaseWorker` 收敛五件事：取消标志、``failed`` 信号、``finished_ok`` 信号、
结束后的自我清理、以及把任意异常转成用户可读文本。子类只需实现 :meth:`work`
并返回它自己的成败结果，不需要各自写 try/except 与 cancel 标志。

对外契约（五个 worker 一致）：

- ``failed(str)``        失败时发一次，文本可直接展示
- ``finished_ok()``      无论成败，线程主体结束时发一次（含被取消）
- ``cancel()``           请求取消，线程内通过 ``self.cancelled`` 查询
- ``state``              ``"idle" | "running" | "done" | "failed" | "cancelled"``
- ``last_error``         最近一次失败原因（``str | None``）

``completed`` / ``episode_done`` / ``progress`` / ``log`` 等属于各子类的
业务信号，保留在各子类，不进基类。

取消语义（重要）
----------------
取消**不回滚已完成的阶段**。``AnalysisWorker`` 在转写之后被取消时，
``finished_ok`` 会照常发出，此时 ``transcript`` 已有值、``candidates`` 仍为 None。
调用方靠 ``state`` 与结果是否为 None 判断，不要假设「取消 = 什么都没做」。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.app_config import AppConfig
from ..core.export import ExportBatchResult, ExportPreset, Exporter
from ..core.ffmpeg import check_encoders
from ..core.plan import BoundaryPlan
from ..core.planner import PlanningError, plan_rule_based
from ..core.probe import MediaInfo, check_compatibility, probe_media
from ..core.settings import SplitSettings

__all__ = [
    "BaseWorker",
    "ProbeWorker",
    "PlanWorker",
    "AnalysisWorker",
    "ExportWorker",
    "ModelDownloadWorker",
]


class BaseWorker(QThread):
    """所有后台任务的公共基类。

    子类实现 :meth:`work`；抛出的异常会被 :meth:`run` 捕获并转成
    ``failed`` 信号，同时把 ``state`` 置为 ``"failed"``。
    """

    failed = Signal(str)
    finished_ok = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancel_requested = False
        self._state = "idle"
        self._last_error: str | None = None

    # ---- 对外状态 ------------------------------------------------------

    @property
    def state(self) -> str:
        """``idle`` / ``running`` / ``done`` / ``failed`` / ``cancelled``。"""
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def cancelled(self) -> bool:
        """线程内查询是否已被请求取消。

        子类在长循环里轮询它；线程外可用 ``state == "cancelled"`` 判断最终结果。
        """
        return self._cancel_requested

    def cancel(self) -> None:
        """请求取消。非阻塞：不会等待线程结束。"""
        self._cancel_requested = True

    def fail(self, message: str) -> None:
        """子类在无法用返回值表达失败时调用（如中途发现无解）。"""
        self._last_error = message
        self._state = "failed"
        self.failed.emit(message)

    def finish_cancelled(self) -> None:
        """子类确认取消后调用，把状态标成已取消（不覆盖已有失败）。"""
        if self._state != "failed":
            self._state = "cancelled"

    # ---- 子类入口 ------------------------------------------------------

    def work(self) -> None:
        """子类实现实际工作。返回即视为成功。"""
        raise NotImplementedError

    def run(self) -> None:  # noqa: D102
        self._state = "running"
        try:
            self.work()
        except Exception as exc:  # noqa: BLE001 - 边界层必须把异常变成可读信息
            self.fail(self.describe_error(exc))
        else:
            if self._state == "running":
                self._state = "done"
        finally:
            self.finished_ok.emit()

    def describe_error(self, exc: BaseException) -> str:
        """把异常转成用户可读文本。子类可覆写以加领域前缀。"""
        return str(exc)


class ProbeWorker(BaseWorker):
    """媒体探测 + 兼容性判定（§17.1，含解码可用性检查）。"""

    completed = Signal(object, list)   # MediaInfo, list[Issue]

    def __init__(self, binaries, path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._binaries = binaries
        self._path = Path(path)

    def work(self) -> None:  # noqa: D102
        check_encoders(self._binaries)
        info: MediaInfo = probe_media(self._binaries, self._path)
        issues = check_compatibility(info, self._binaries)
        self.completed.emit(info, issues)


class PlanWorker(BaseWorker):
    """生成规则草案方案。

    阶段1 使用规则规划器（等分 + 帧吸附）。替换路径按方案 §16 分三步：
    阶段3 提供候选数据库 → 阶段2 在候选上建图做动态规划 → 阶段4 加入剧情边代价。
    规则结果届时保留为对照基线（§20.3）。
    """

    completed = Signal(object, object, list)  # BoundaryPlan, FeasibilityReport, list[PlanProblem]

    def __init__(self, media: MediaInfo, settings: SplitSettings, parent=None) -> None:
        super().__init__(parent)
        self._media = media
        self._settings = settings

    def describe_error(self, exc: BaseException) -> str:
        if isinstance(exc, PlanningError):
            return str(exc)
        return f"规划失败：{exc}"

    def work(self) -> None:  # noqa: D102
        plan, report, problems = plan_rule_based(self._media, self._settings)
        self.completed.emit(plan, report, problems)


class AnalysisWorker(BaseWorker):
    """阶段3 分析流水线：字幕 → 语音转写 → 镜头与黑场 → 候选数据库。

    各步骤都走缓存（§12.2），因此重复分析或换参数后重跑都很快。
    每一步的产出都完整保留在结果里，供后续页与阶段2 使用。
    """

    stage_started = Signal(str)
    stage_progress = Signal(str, float)          # 说明, 0-1
    stage_log = Signal(str)                      # 一行人可读的结论
    completed = Signal(object, object, object, object, object)  # 方案,报告,问题,候选,整集复核

    def __init__(self, binaries, media: MediaInfo, audio_stream_index: int | None,
                 settings: SplitSettings, parent=None, *, config: AppConfig | None = None,
                 cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._binaries = binaries
        self._media = media
        self._audio_stream_index = audio_stream_index
        self._settings = settings
        # 厂商档案与后端偏好：给定时用它（测试注入），否则运行时读配置文件——
        # 模型对话框把改动写在同一份 config.json 上，因此两边看到的是同一状态。
        self._config = config
        # 缓存目录：默认就是项目根的 cache/；测试与端到端脚本可注入临时目录，
        # 免得把假结果写进开发机缓存（否则后续跑测试会静默命中假数据）。
        self._cache_dir = cache_dir
        # 中间产出：取消或失败时也保留已算出的部分，供调用方按需取用
        self.transcript = None
        self.subtitle_tracks: list = []

    # ---- 各阶段 --------------------------------------------------------

    def describe_error(self, exc: BaseException) -> str:
        return f"分析失败：{exc}"

    def work(self) -> None:  # noqa: D102
        from fractions import Fraction
        from pathlib import Path

        from app.core import subtitles as subs
        from app.core.asr import AsrSettings
        from app.core.cache import CacheStore, SourceFingerprint
        from app.core.cloud_asr import select_asr_engine
        from app.core.shots import detect_all
        from app.core.candidates import build_candidate_set

        project_root = Path(__file__).resolve().parent.parent.parent
        cache_dir = self._cache_dir or (project_root / "cache")
        store = CacheStore(cache_dir)
        fingerprint = SourceFingerprint.of(self._media.path)
        ffmpeg = str(self._binaries.ffmpeg)
        # 厂商档案与后端偏好：转写与语义判断共用这一份（模型对话框写的是同一个文件）
        config = self._config if self._config is not None else AppConfig.load()

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
        engine, engine_notes = select_asr_engine(
            config, settings, cache=store, fingerprint=fingerprint
        )
        for note in engine_notes:
            self.stage_log.emit(note)
        try:
            transcript, hit = engine.transcribe_cached(
                self._media.path,
                fingerprint,
                store,
                audio_stream_index=self._audio_stream_index,
                duration_seconds=self._media.timeline_duration,
                on_progress=lambda ratio, text: self.stage_progress.emit(text, ratio),
                cancel_check=lambda: self.cancelled,
                ffmpeg=ffmpeg,
            )
        except RuntimeError as exc:
            if getattr(engine, "is_cloud", False) and config.fallback_to_local:
                # §7：块级失败一律整体回退本地重跑，不做"半云端半本地"的拼接——
                # 混合来源的时间码难以核实，宁可贵一点重跑。失败原因要带上"第几段"。
                detail = getattr(engine, "last_chunk_failure", "") or str(exc)
                self.stage_log.emit(f"云端转写失败（{detail}），已回退本地模型重跑。")
                local, _ = select_asr_engine(None, settings, cache=store, fingerprint=fingerprint)
                try:
                    transcript, hit = local.transcribe_cached(
                        self._media.path,
                        fingerprint,
                        store,
                        audio_stream_index=self._audio_stream_index,
                        duration_seconds=self._media.timeline_duration,
                        on_progress=lambda ratio, text: self.stage_progress.emit(text, ratio),
                        cancel_check=lambda: self.cancelled,
                        ffmpeg=ffmpeg,
                    )
                except RuntimeError as exc2:
                    self.stage_log.emit(f"跳过语音转写：{exc2}")
            else:
                self.stage_log.emit(f"跳过语音转写：{exc}")
        else:
            if hit:
                self.stage_log.emit("转写命中缓存。")
            self.stage_log.emit(transcript.describe())
            if getattr(engine, "is_cloud", False) and not hit:
                # 外发流水（设计 §6）：这一轮到底传了多少音频出去
                self.stage_log.emit(engine.egress_line(transcript))
            for issue in transcript.issues:
                self.stage_log.emit(f"  风险 {issue.code}：{issue.message}")
        # 转写在被取消后也要可见：main_window 靠它决定导出时是否生成 SRT
        self.transcript = transcript
        self.subtitle_tracks = subtitle_tracks
        if self.cancelled:
            self.finish_cancelled()
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
        from app.core.semantic import EventIndex, judge_candidates, select_judge
        from app.core.settings import CountPolicy, SplitMode, check_feasibility
        from app.core.solver import solve_from_candidates

        index = EventIndex.build(transcript, shots.scenes)
        self.stage_log.emit(index.describe())

        # ---- 语义判断（§11，可选，带预算与降级）--------------------
        # 后端由用户配置决定（本地 / 云端档案）；select_judge 返回的 notes 是
        # "为什么这样选"的唯一说明来源，必须原样上日志——否则用户只会看到
        # 判定没生效，却不知道该去开哪个开关。
        judge, judge_notes = select_judge(config)
        for note in judge_notes:
            self.stage_log.emit(note)
        if judge is None:
            self.stage_log.emit(
                "语义判断未启用（本地模型与云端后端都不可用）——"
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
                self.fail(str(exc))
                return

            blocking = [p for p in problems if p.is_blocking]
            if blocking:
                # §4.2：无解必须明确报错，不能把空方案当结果交给用户。
                # 典型成因：对白句末不落在目标时长允许的窗口内（§10.4）。
                self.fail(blocking[0].message + " " + blocking[0].describe())
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
                self.fail(str(exc))
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


class ExportWorker(BaseWorker):
    """批量导出（§14.4 事务式导出、失败重试、可取消）。"""

    episode_done = Signal(object)   # ExportResult
    completed = Signal(object)      # ExportBatchResult

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
        """导出取消要落到 Exporter 内部的子进程上，否则 ffmpeg 会跑完。"""
        super().cancel()
        self._exporter.cancel()

    def describe_error(self, exc: BaseException) -> str:
        return f"导出失败：{exc}"

    def work(self) -> None:  # noqa: D102
        batch: ExportBatchResult = self._exporter.export_plan(
            self._plan,
            self._output_root,
            audio_stream_index=self._audio_stream_index,
            on_episode_done=self.episode_done.emit,
            only_episodes=self._only,
        )
        self.completed.emit(batch)


class ModelDownloadWorker(BaseWorker):
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

    def work(self) -> None:  # noqa: D102
        from app.core.model_manager import download_model

        try:
            result = download_model(
                self._spec,
                self._models_root,
                endpoint=self._endpoint,
                on_progress=lambda done, total, name, index, count: self.progress.emit(
                    done, total, name, index, count
                ),
                cancel_check=lambda: self.cancelled,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须变成可读结果
            from app.core.model_manager import DownloadResult

            result = DownloadResult(
                spec=self._spec, ok=False, message=f"下载过程异常：{exc}"
            )
        if not result.ok:
            self._last_error = result.message
            self._state = "failed"
        elif self.cancelled:
            self.finish_cancelled()
        self.log.emit(result.describe())
        self.finished_with.emit(result)
