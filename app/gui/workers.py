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

import atexit
import time
import weakref
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.app_config import AppConfig, app_dir
from ..core.export import ExportBatchResult, ExportPreset, Exporter
from ..core.ffmpeg import check_encoders
from ..core.model_manager import default_whisper_tier, installed_llm_file, spec_by_key
from ..core.plan import BoundaryPlan
from ..core.planner import PlanningError, plan_rule_based
from ..core.probe import MediaInfo, check_compatibility, probe_media
from ..core.providers import ProviderError
from ..core.settings import SplitSettings
from ..core.solver import GraphDiagnostics

__all__ = [
    "BaseWorker",
    "ProbeWorker",
    "PlanWorker",
    "AnalysisWorker",
    "ExportWorker",
    "ModelDownloadWorker",
    "JudgeProbeWorker",
    "ModelListWorker",
    "EnvCheckWorker",
    "stop_all_workers",
]


#: 所有**还在跑**的后台任务。为什么要有这份全局登记：
#:
#: ``MainWindow.register_worker`` 只登记到主窗口，而"进程要退出"这件事与宿主无关。
#: Qt 在析构一个还在运行的 QThread 时会直接 fail-fast——实测整个进程以
#: ``0xC0000409`` 死掉（故障模块 ``Qt6Core.dll``），且**只在收尾那一刻**发生，
#: 表现为"测试跑完了、进程却崩了"这种最难查的偶发失败。模型页的推理栈探测线程
#: 已经单独收过一次（见 model_form）；这里是覆盖全部 QThread 子类的通用兜底。
_LIVE_WORKERS: "weakref.WeakSet[BaseWorker]" = weakref.WeakSet()


def _is_running(worker: object) -> bool:
    try:
        return bool(worker.isRunning())
    except RuntimeError:      # C++ 侧已释放
        return False


def stop_all_workers(wait_ms: int = 5000) -> int:
    """请求取消并等待所有后台任务收尾；返回等待后**仍在跑**的数量。

    退出路径专用：返回非 0 说明还有线程没停（比如工作函数不检查取消标志），
    调用方可以再等一轮——但绝不能带着"还在跑的 QThread"进解释器收尾。
    """
    pending = [worker for worker in list(_LIVE_WORKERS) if _is_running(worker)]
    for worker in pending:
        try:
            worker.cancel()
        except Exception:  # noqa: BLE001 - 收尾尽力而为
            pass
    for worker in pending:
        try:
            worker.wait(wait_ms)
        except Exception:  # noqa: BLE001
            pass
    return sum(1 for worker in list(_LIVE_WORKERS) if _is_running(worker))


def _drain_workers_at_exit() -> None:
    """解释器退出前把所有后台任务收干净。

    ``atexit`` 在 C 扩展（Qt）被拆除**之前**执行，所以这里 ``wait`` 来得及；
    晚了就只能在 QThread 析构处崩掉。
    """
    try:
        stop_all_workers(wait_ms=10_000)
    except Exception:  # noqa: BLE001 - 退出路径不许再抛
        pass


atexit.register(_drain_workers_at_exit)


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
        _LIVE_WORKERS.add(self)
        try:
            self.work()
        except Exception as exc:  # noqa: BLE001 - 边界层必须把异常变成可读信息
            self.fail(self.describe_error(exc))
        else:
            if self._state == "running":
                self._state = "done"
        finally:
            _LIVE_WORKERS.discard(self)
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


def default_cache_dir() -> Path:
    """分析缓存的默认落点：exe（或项目根）旁边的 `cache/`。

    刻意不用 `Path(__file__)` 推导——打包后源码位于 `_internal/`，
    装进 Program Files 时那里是只读的，写缓存会直接 PermissionError。
    """
    return app_dir() / "cache"


def active_whisper_tier(config: AppConfig | None = None) -> str:
    """本地转写要用的档位：配置里"使用中"的档位，未配置则用默认档位。

    只对**本地**路径有意义（云端转写由厂商承担）；`config` 可注入，便于测试
    与复用同一份配置（`AnalysisWorker` 先读一次，再传进来）。
    """
    config = config if config is not None else AppConfig.load()
    return config.asr_model_tier or default_whisper_tier()


# 转写没产出对白的原因（``AnalysisWorker.dialogue_diagnosis`` 的取值）。
# 「没有对白」有三种完全不同的处境，界面必须分开说——否则用户看着"无对白信息"
# 一头雾水：自己明明下了模型、片子明明有对白。
DIALOGUE_OK = ""                       # 转写正常（可能确实无对白，另有说明）
DIALOGUE_NO_ENGINE = "no_engine"       # 装了模型但版本不含转写引擎（打包版常见）
DIALOGUE_NO_MODEL = "no_model"         # 有引擎但没下模型
DIALOGUE_FAILED = "failed"             # 引擎与模型都在，转写本身出错
DIALOGUE_EMPTY_AUDIO = "empty_audio"   # 无音轨


def classify_dialogue_failure(message: str) -> str:
    """把转写失败信息归类成 ``DIALOGUE_*``，供界面给出对症的下一步。

    判据取自 ``app.core.asr`` 实际抛出的文案（那里是唯一的错误来源）：
    - "未安装 faster-whisper" → 引擎缺失（打包版刻意不含，需源码版）
    - "未找到模型" → 模型没下
    两者都在具体实现里写成固定措辞，这里按关键词匹配；匹配不到归为一般失败。
    """
    text = message or ""
    if "faster-whisper" in text or "faster_whisper" in text:
        return DIALOGUE_NO_ENGINE
    if "未找到模型" in text:
        return DIALOGUE_NO_MODEL
    return DIALOGUE_FAILED


def dialogue_failure_advice(kind: str, model_tier: str = "") -> tuple[str, str]:
    """``DIALOGUE_*`` → (给用户看的一句话标题, 可行动的详细说明)。

    这是"界面不撒谎"的落点：缺引擎就说缺引擎、别引导用户再下一次模型
    （那解决不了问题）。返回空串表示这一类问题不需要额外提示。
    """
    if kind == DIALOGUE_NO_ENGINE:
        return (
            "还没装语音转写引擎",
            "转写需要 faster-whisper，而它不在安装包里（体积原因）。"
            "到「帮助 → 环境自检」点「安装转写引擎」即可应用内一键装好"
            "（约 80 MB，不需要 pip 或编译器）。暂时也可以把它当作"
            "只做静音 / 字幕分集的工具使用。",
        )
    if kind == DIALOGUE_NO_MODEL:
        tier = model_tier or "small"
        return (
            f"未下载转写模型（whisper-{tier}）",
            f"转写引擎已就绪，但缺少 whisper-{tier} 模型文件。"
            "打开「工具 → 模型管理」下载后可重跑分析。",
        )
    if kind == DIALOGUE_FAILED:
        return (
            "语音转写未能完成",
            "转写引擎与模型都在，但转写过程出错了（详见右栏日志）。"
            "分集已按静音与外挂字幕照常完成；修正后可用「重新解析」重跑。",
        )
    if kind == DIALOGUE_EMPTY_AUDIO:
        return (
            "源视频没有音轨",
            "该文件不含音频轨道，无法做语音转写。"
            "分集按画面黑场与外挂字幕进行；如预期应有对白，请检查源文件。",
        )
    return ("", "")


def describe_dialogue_failure(kind: str, model_tier: str, raw: str) -> str:
    """给日志用的**详细**转写失败说明：原因分类 + 该怎么做 + 原始报错。

    原先只打一句 ``跳过语音转写：<异常文本>``——用户看到的是引擎的实现细节
    （如 "No module named 'faster_whisper'"），既不知道这意味着什么，也不知道
    下一步。这里分三层：分类结论（人话）→ 可行的下一步 → 原始报错（供排障）。
    """
    title, advice = dialogue_failure_advice(kind, model_tier)
    if not title:
        title, advice = "转写失败", "原因见后附原始报错。"
    parts = [f"{title}。{advice}"]
    raw = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    if raw:
        parts.append(f"原始报错：{raw[:200]}")
    return " ".join(parts)


def configured_ollama_model(config: AppConfig | None = None) -> str | None:
    """配置里"使用中的本地语义模型"对应的 Ollama 名字；未指定返回 None。

    `None` 不是错误：它表示沿用"按体积取最大 GGUF"的自动规则。
    这里只管**用哪个 GGUF**；本次到底走本地还是云端，由
    `semantic.select_judge()` 按后端偏好决定。
    """
    from app.core.semantic import ollama_model_name_for

    config = config if config is not None else AppConfig.load()
    key = config.llm_model_key
    if not key:
        return None
    spec = spec_by_key(key)
    path = installed_llm_file(spec, config.resolved_models_dir()) if spec else None
    return ollama_model_name_for(path) if path is not None else None


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
        #: 转写为什么没产出对白——给界面用的可行动诊断，取值见 ``DIALOGUE_*``。
        #: 空字符串表示"没出问题"。这是"无对白信息"这类含糊警告的唯一解释来源：
        #: 缺引擎 / 缺模型 / 素材真没对白 是三种完全不同的处境，必须分开说。
        self.dialogue_diagnosis = ""
        #: 诊断里的档位名（"未下载模型"这类提示要指名道姓，否则用户不知道下哪个）
        self.dialogue_model_tier = ""
        # 阶段计时：记下上一阶段的名称与起点，由 _begin_stage 补一条耗时日志
        self._stage_name = ""
        self._stage_t0: float | None = None
        self._work_t0: float | None = None
        # 求解失败时的图诊断：界面据此给出「应用建议」（§10.4 的可操作化）
        self.failure_diagnostics: GraphDiagnostics | None = None
        #: 模式C 的自动判定结果（TimingDecision）；其他模式为 None，供界面展示
        self.timing_decision = None

    # ---- 各阶段 --------------------------------------------------------

    def _begin_stage(self, name: str) -> None:
        """进入一个阶段：先给上一阶段补一条**耗时**日志，再宣告本阶段开始。

        为什么记耗时：用户报"卡住了"时，最有用的第一手信息是"卡在哪个阶段、
        花了多久"。原先日志只有阶段名，没有时间，事后完全看不出是哪一步慢。
        """
        if self._stage_name and self._stage_t0 is not None:
            elapsed = time.perf_counter() - self._stage_t0
            self.stage_log.emit(f"（上一阶段「{self._stage_name}」耗时 {elapsed:.1f} 秒）")
        self._stage_name = name
        self._stage_t0 = time.perf_counter()
        self.stage_started.emit(name)

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

        # 缓存默认写在 exe 旁边而不是 `_internal/`（那里装进 Program Files 后只读）
        cache_dir = self._cache_dir or default_cache_dir()
        store = CacheStore(cache_dir)
        fingerprint = SourceFingerprint.of(self._media.path)
        ffmpeg = str(self._binaries.ffmpeg)
        # 厂商档案与后端偏好：转写与语义判断共用这一份（模型对话框写的是同一个文件）
        config = self._config if self._config is not None else AppConfig.load()

        # 全程计时起点（收尾时打一行总耗时，便于判断"整趟慢在哪"）
        self._work_t0 = time.perf_counter()
        media = self._media
        dur = float(media.timeline_duration) if media.timeline_duration else 0.0
        audio_count = len(media.audio_tracks)
        fps = float(media.video.nominal_fps) if media.video else 0.0
        self.stage_log.emit(
            f"开始分析：时长 {dur:.2f} 秒 · {fps:.3f}fps · "
            f"{audio_count} 条音轨 · 文件 {Path(media.path).name}"
        )

        transcript = None
        subtitle_tracks = []

        # ---- 字幕 --------------------------------------------------
        self._begin_stage("字幕解析")
        sidecars = subs.find_sidecar_subtitles(self._media.path)
        for sidecar in sidecars:
            track = subs.load_subtitle_file(sidecar)
            subtitle_tracks.append(track)
            self.stage_log.emit(f"{sidecar.name}: {track.describe()}")
        if not subtitle_tracks:
            self.stage_log.emit("未找到同名外挂字幕（SRT/VTT/ASS）。")

        # ---- 语音转写 ----------------------------------------------
        self._begin_stage("语音转写")
        settings = AsrSettings(model_tier=active_whisper_tier(config))
        self.dialogue_model_tier = settings.model_tier
        # 无音轨的源片注定转写不出东西：先认出来，别让用户以为"没对白"是引擎问题
        if not self._media.audio_tracks:
            self.dialogue_diagnosis = DIALOGUE_EMPTY_AUDIO
        self.stage_log.emit(
            f"本地转写档位：whisper-{settings.model_tier}（可在 工具 → 模型管理 中切换）"
        )
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
        except (RuntimeError, ImportError) as exc:
            # ImportError 也要接：转写引擎缺依赖（打包版不含 faster-whisper）
            # 抛的是 ModuleNotFoundError，它是 ImportError 子类而**不是**
            # RuntimeError。漏在这里会让"跳过转写"的降级变成致命的"分析失败"。
            # asr.AsrEngine.ensure_model 已把 import 错误转成 RuntimeError，
            # 这里是第二道保险——别的引擎实现未必都做了那层包装。
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
                except (RuntimeError, ImportError) as exc2:
                    self.dialogue_diagnosis = classify_dialogue_failure(str(exc2))
                    self.stage_log.emit(
                        "跳过语音转写（本地回退也失败）："
                        f"{describe_dialogue_failure(self.dialogue_diagnosis, settings.model_tier, str(exc2))}"
                    )
            else:
                self.dialogue_diagnosis = classify_dialogue_failure(str(exc))
                self.stage_log.emit(
                    "跳过语音转写："
                    f"{describe_dialogue_failure(self.dialogue_diagnosis, settings.model_tier, str(exc))}"
                )
        else:
            if hit:
                self.stage_log.emit("转写命中缓存。")
            self.stage_log.emit(transcript.describe())
            if getattr(engine, "is_cloud", False) and not hit:
                # 外发流水（设计 §6）：这一轮到底传了多少音频出去
                self.stage_log.emit(engine.egress_line(transcript))
            for issue in transcript.issues:
                self.stage_log.emit(f"  风险 {issue.code}：{issue.message}")
            # 转写"成功"但一句对白都没有：要么素材真没对白，要么引擎跑空。
            # 有音轨却零句对白值得单独说一句，免得又被读成"转写坏了"。
            if not transcript.sentences:
                if not self._media.audio_tracks:
                    self.dialogue_diagnosis = DIALOGUE_EMPTY_AUDIO
                else:
                    self.stage_log.emit(
                        "转写完成但未识别到任何对白——该片段可能确实无人声。"
                    )
        # 转写在被取消后也要可见：main_window 靠它决定导出时是否生成 SRT
        self.transcript = transcript
        self.subtitle_tracks = subtitle_tracks
        if self.cancelled:
            self.finish_cancelled()
            self.failed.emit("已取消")
            return

        # ---- 镜头与黑场 --------------------------------------------
        self._begin_stage("镜头与黑场")
        shots = detect_all(
            self._media.path, ffmpeg, cache=store, fingerprint=fingerprint,
            duration_seconds=float(self._media.timeline_duration),
            on_progress=lambda ratio, text: self.stage_progress.emit(text, ratio),
        )
        self.stage_log.emit(shots.describe())

        # ---- 候选点 ------------------------------------------------
        self._begin_stage("候选点")
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
        self._begin_stage("整体规划")
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
        # 本地路径用"使用中"的档位指定 Ollama 模型（模型对话框里可切换）。
        judge, judge_notes = select_judge(
            config, ollama_model=configured_ollama_model(config)
        )
        for note in judge_notes:
            self.stage_log.emit(note)
        if judge is None:
            self.stage_log.emit(
                "语义判断未启用（本地模型与云端后端都不可用）——"
                "候选保持规则评分，审核状态保持 pending（§ 降级路径）。"
            )
        strategy = self._settings.strategy.value
        # 语义判断是「整体规划」里唯一的长耗时子步骤（云端后端一次请求数秒），
        # 因此把它的比例映射到本阶段 5%–85%，求解前再报到 90%（求解本身很快）。
        outcome = judge_candidates(
            candidates, index, judge, strategy=strategy, budget_requests=24,
            on_progress=lambda done, total: self.stage_progress.emit(
                f"语义判断 {done}/{total}", 0.05 + 0.8 * done / max(1, total)
            ),
        )
        self.stage_log.emit(outcome.describe())
        for note in outcome.notes:
            self.stage_log.emit(f"  {note}")

        def baseline():
            """§20.3 的规则基线（等分），用于与 DP 结果对照。"""
            return plan_rule_based(self._media, self._settings)[0]

        report = check_feasibility(self._settings, self._media.timeline_duration)
        # 模式C（推荐方案）走 ``auto_pending``：目标时长还没有，要等自动判定
        # 定下集数后才有派生参数。把它排除在外会直接掉进规则等分兜底，
        # 等于推荐方案从来没生效过。
        recommend = self._settings.split_mode == SplitMode.RECOMMEND
        usable = not candidates.is_empty and not report.blocking and (
            report.derived is not None or (recommend and report.auto_pending)
        )
        if usable:
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
            self.stage_progress.emit("边界求解", 0.9)
            try:
                if recommend:
                    # 「推荐方案」：先按内容粒度自动判定集数与时长，再在选定
                    # 集数下比较三种策略；落选者全部写进日志（§20.3 的延伸）。
                    plan, solution, problems, notes = self._solve_recommended(
                        candidates, ends_question
                    )
                    if plan is None:
                        return          # 已在 _solve_recommended 里报错
                else:
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
                # 只报 describe()：它的文本里已经含 message，再拼一次整段话会
                # 在报错框里原样重复两遍。
                # 诊断一并留下：界面据此给出「应用建议」按钮
                self.failure_diagnostics = solution.diagnostics
                self.fail(blocking[0].describe())
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
            self.stage_progress.emit("规则等分兜底", 0.9)
            try:
                plan, report, problems = plan_rule_based(self._media, self._settings)
            except PlanningError as exc:
                self.fail(str(exc))
                return
        # ---- 整集复核（阶段4）--------------------------------------
        from app.core.review import review_plan

        self._begin_stage("边界审核")
        reviews = review_plan(
            plan, self._media, index,
            transcript=transcript, blacks=shots.blacks,
            on_progress=lambda done, total: self.stage_progress.emit(
                f"整集复核 {done}/{total}", done / max(1, total)
            ),
        )
        grade_labels = {"pass": "通过", "warn": "留意", "block": "阻断"}
        for review in reviews:
            detail = "；".join(review.risks) if review.risks else "无风险"
            self.stage_log.emit(
                f"  第{review.episode:02d}集复核[{grade_labels[review.grade]}]：{detail}"
            )

        # 收尾：补上最后一段的耗时，并给出全程总耗时——排障时"哪步慢"一眼可读
        if self._stage_t0 is not None:
            self.stage_log.emit(
                f"（上一阶段「{self._stage_name}」耗时 "
                f"{time.perf_counter() - self._stage_t0:.1f} 秒）"
            )
        if self._work_t0 is not None:
            self.stage_log.emit(f"分析全程耗时 {time.perf_counter() - self._work_t0:.1f} 秒。")

        self.completed.emit(plan, report, problems, candidates, reviews)

    def _solve_recommended(self, candidates, ends_question):
        """「推荐方案」（模式C）：自动判定集长与集数，再在该集数下比较三种策略。

        两段式是刻意的：**搜索阶段只出 ``GraphSolution``**，不重建方案
        （``BoundaryPlan`` 的转换是最贵的一步，而它只需要做一次）；
        胜出者才走一次既有的求解入口拿正式方案。

        落选者不隐藏：节拍锚点、比较过的每个 (集数, 策略) 及其分数一并写进日志，
        用户能看出"为什么是这一集数"。全部无解时返回 None 并已 fail（§4.2）。
        """
        from app.core.auto_timing import auto_decide_timing
        from app.core.settings import DerivedParams
        from app.core.solver import solve_from_candidates

        decision = auto_decide_timing(
            candidates,
            self._media,
            self._settings,
            self._media.timeline_duration,
            ends_with_question=ends_question,
        )
        self.timing_decision = decision
        for line in decision.describe().splitlines():
            self.stage_log.emit(line)

        chosen = decision.chosen
        if chosen is None:
            self.failure_diagnostics = decision.diagnostics
            self.fail(
                "推荐方案自动判定失败："
                + (decision.notes[0] if decision.notes else "没有任何集数可行。")
            )
            return None, None, None, None

        # 判定结果回填到设置：随项目快照保存，重开项目仍能看到当时判成了什么。
        # 单独存在 decided_* 里而不是写回 target_duration_seconds——用户当初
        # 并没填过那个值，写回去会让下次打开设置时误以为可以照着改。
        self._settings.decided_episode_count = chosen.episode_count
        self._settings.decided_target_seconds = chosen.target_duration
        self._settings.decided_strategy = chosen.strategy
        self._settings.decided_beat_seconds = decision.beat.anchor_duration

        derived = DerivedParams(
            target_duration=chosen.target_duration,
            min_duration=max(
                Fraction(1), chosen.target_duration * Fraction(1, 2)
            ),
            max_duration=chosen.target_duration * Fraction(3, 2),
            total_seconds=self._media.timeline_duration,
        )
        plan, solution, problems, notes = solve_from_candidates(
            candidates,
            self._media,
            derived,
            settings_count_exact=True,
            target_episode_count=chosen.episode_count,
            allowed_min=chosen.episode_count,
            allowed_max=chosen.episode_count,
            strategy=chosen.strategy,
            ends_with_question=ends_question,
        )
        notes = list(notes) + [
            f"采用推荐：{chosen.episode_count} 集 × {float(chosen.target_duration):.1f}s"
            f"（{chosen.label}）"
        ]
        return plan, solution, problems, notes


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


class UpdateCheckWorker(BaseWorker):
    """查有没有新版本（后台线程）。

    为什么必须后台：``releases/latest`` 走网络，超时上限十几秒；放主线程会让
    窗口在启动后僵住十几秒。

    启动时的静默检查与菜单里的手动检查共用它，区别只在**结果怎么呈现**——
    那是调用方的事，这个 worker 只负责把结果老老实实报出去。
    """

    checked = Signal(object)  # CheckResult

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self._version = current_version

    def work(self) -> None:  # noqa: D102
        from app.core.updater import check_for_update

        try:
            result = check_for_update(self._version)
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须变成可读结果
            from app.core.updater import CheckResult

            result = CheckResult(ok=False, message=f"检查更新时出错：{exc}")
        if not result.ok:
            self._last_error = result.message
            self._state = "failed"
        self.checked.emit(result)


class UpdateDownloadWorker(BaseWorker):
    """下载更新包（后台线程，可取消，过程中回报进度，校验 SHA-256）。

    校验不通过时 ``download_asset`` 会把文件删掉再返回失败——这里不重复这个
    逻辑，只负责把结果转成信号。**绝不把校验失败的路径交出去。**

    进度必须真的接出来：以前这里没给 ``download_asset`` 传 ``on_progress``，
    而它自己又只在结束时回调一次，结果是「进度条一动不动、看着像卡死」
    （用户实测反馈）。现在按 ~5Hz 把已下载字节透出去。
    """

    progress = Signal(int, int)  # 已下载字节, 总字节
    source_changed = Signal(str)  # 当前下载来源主机（直连 / 镜像）
    finished_with = Signal(object)  # DownloadOutcome

    def __init__(self, urls, target, *, digest: str = "", size: int = 0,
                 parent=None) -> None:
        # ``urls`` 可以是单个地址，也可以是候选列表（API 资产地址优先、
        # 官方下载地址兜底）——谁来试由 download_asset 决定。
        super().__init__(parent)
        self._urls = urls
        self._target = target
        self._digest = digest
        self._size = size

    def work(self) -> None:  # noqa: D102
        from app.core.app_config import AppConfig
        from app.core.updater import DownloadOutcome, download_asset

        try:
            mirror = ""
            try:
                mirror = str(AppConfig.load().update_mirror or "")
            except Exception:  # noqa: BLE001 - 配置读不出来就直连，不该挡住更新
                mirror = ""
            outcome = download_asset(
                self._urls,
                self._target,
                digest=self._digest,
                size=self._size,
                mirror=mirror,
                cancel_check=lambda: self.cancelled,
                on_progress=lambda done, total, *_: self.progress.emit(
                    int(done), int(total)
                ),
                on_source=lambda host: self.source_changed.emit(str(host)),
            )
        except Exception as exc:  # noqa: BLE001
            outcome = DownloadOutcome(ok=False, message=f"下载过程异常：{exc}")
        if not outcome.ok:
            self._last_error = outcome.message
            self._state = "cancelled" if outcome.cancelled else "failed"
        self.finished_with.emit(outcome)


class JudgeProbeWorker(BaseWorker):
    """后台探测语义判断后端（云端档案 / 本地推理栈），供模型界面如实显示。

    为什么必须后台：本地探测自身有 3 秒超时，还可能加载进程内 llama.cpp 模型，
    放主线程会让对话框一打开就卡住。
    """

    probed = Signal(object)  # str（判定器描述 [+ 选择原因]）| None（不可用，降级）

    def work(self) -> None:  # noqa: D102
        from app.core.semantic import select_judge

        try:
            config = AppConfig.load()
            judge, notes = select_judge(
                config, ollama_model=configured_ollama_model(config)
            )
        except Exception:  # noqa: BLE001 - 探测失败就是"不可用"，不该让对话框报错
            judge, notes = None, []
        if judge is None:
            self.probed.emit(None)
            return
        # notes 里是"为什么这样选"（例如"选了云端但没开外发开关，本次仍用本地"），
        # 只显示判定器名字会让用户以为配置没生效。
        self.probed.emit(f"{judge.describe()}（{notes[0]}）" if notes else judge.describe())


class ModelListWorker(BaseWorker):
    """后台拉取某厂商的模型清单（供「厂商管理」自动填充模型名）。

    必须后台：`GET /models` 是网络请求，超时 10 秒——放主线程会把对话框卡住。
    失败**不算异常**：不少厂商（火山方舟只认推理接入点 ID、部分自建网关）根本没有
    该端点，`failed` 带的是"请手动填写"的可读原因，界面照原样提示即可。
    """

    listed = Signal(object)  # list[str]

    def __init__(self, profile, parent=None) -> None:
        super().__init__(parent)
        self._profile = profile

    def work(self) -> None:  # noqa: D102
        from app.core.cloud import list_models

        try:
            models = list_models(self._profile)
        except ProviderError as exc:
            self.fail(exc.describe())
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常都变成可读原因
            self.fail(str(exc))
            return
        self.listed.emit(models)


class EnvCheckWorker(BaseWorker):
    """跑一遍本地环境自检（后台线程）。

    为什么必须后台：检测里含 ``ffmpeg -version`` / ``-encoders``（可达数十秒），
    以及若干包的 ``find_spec``；放主线程会让对话框一打开就僵住。

    ``check_environment`` 本就不抛异常（逐项失败各自降级成一条结论），这里的
    ``except`` 只是兜底——真出意外时给一份"检测失败"的结果，而不是让对话框
    永远停在"检测中…"。
    """

    checked = Signal(object)  # EnvReport

    def __init__(self, *, models_root=None, tier: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._models_root = models_root
        self._tier = tier

    def work(self) -> None:  # noqa: D102
        from app.core.env_check import EnvReport, check_environment

        try:
            report = check_environment(models_root=self._models_root, tier=self._tier)
        except Exception as exc:  # noqa: BLE001 - 自检失败也要给界面一个可读结果
            from app.core.env_check import EnvCheckItem

            report = EnvReport(
                items=(
                    EnvCheckItem(
                        key="selfcheck",
                        label="环境自检",
                        level="block",
                        summary=f"自检本身出错：{exc}",
                    ),
                )
            )
        self.checked.emit(report)


class DepsInstallWorker(BaseWorker):
    """在应用内安装语音转写引擎（下载 wheel 到 exe 同级的 deps/）。

    为什么后台：全套约 80 MB，同步会冻住界面半分钟以上。可取消——``cancel_check``
    会让 ``install_deps`` 在下一个包之前停下；已下好的部分留在缓存里，重试时跳过。
    """

    progress = Signal(int, int, str, int, int)  # 已下载字节, 总字节, 当前文件, 第几个, 共几个
    log = Signal(str)
    finished_with = Signal(object)  # DepsInstallResult

    def __init__(self, *, deps_root=None, endpoint: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._deps_root = deps_root
        self._endpoint = endpoint

    def work(self) -> None:  # noqa: D102
        from app.core.deps_manager import DepsInstallResult, install_deps

        try:
            kwargs = {"deps_root": self._deps_root} if self._deps_root else {}
            if self._endpoint:
                kwargs["endpoint"] = self._endpoint
            result = install_deps(
                **kwargs,
                on_progress=lambda done, total, name, i, n: self.progress.emit(
                    done, total, name, i, n
                ),
                cancel_check=lambda: self.cancelled,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须变成可读结果
            result = DepsInstallResult(ok=False, message=f"安装过程异常：{exc}")
        if result.cancelled:
            self.finish_cancelled()
        elif not result.ok:
            self._last_error = result.message
            self._state = "failed"
        self.log.emit(result.describe())
        self.finished_with.emit(result)
