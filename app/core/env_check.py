"""本地运行环境自检：逐项判断"能不能干活"，并把可修复项标出来。

为什么单独成模块
----------------
判定逻辑要与界面解耦：这份检测既给 GUI 的「环境自检」对话框用，也能被
命令行（``run.py --self-check``）与测试直接调用。**本模块不 import PySide6**，
纯逻辑，可在无界面环境里单测——分层与 ``ffmpeg.py`` / ``model_manager.py`` 一致。

诚实边界（重要，别让界面撒谎）
------------------------------
自检能查出问题，可修的动作分两类，**kind 必须如实标注**：

- ``download_model``：应用内下载**模型文件**（走 ``model_manager``，用 curl）；
- ``install_engine``：应用内安装**运行期依赖**（走 ``deps_manager``，下载
  wheel 解压到 exe 同级的 ``deps/``）——语音转写引擎走这条，不需要 pip。

其余一律只能"检测 + 给指引"：

- **FFmpeg**：没有任何下载逻辑，只能让用户去装、或设 DRAMA_FFMPEG_DIR；
- **可选 Python 包**（scenedetect / zhconv / pysubs2）：缺失只降级，给说明
  （scenedetect 还会给 ``open_url`` 指到发布页）；
- **llama-cpp-python**：给 ``open_url`` 指到官方发布页（有 prebuilt 轮子），
  让用户自己下——不冒充应用内能装。

历史教训：早期只有 ``download_model`` 能在应用内执行，那时的注释写成"Python
包一律装不了"。**那是错的**——冻结 exe 的 sys.path 含 ``_internal``，把 wheel
解压到独立目录再挂上就能用；实测（本机 PyInstaller onedir 产物）转写引擎
装好后能在冻结版里真正完成中文语音识别。故新增 ``install_engine``。

模型文件与转写引擎必须分成两项
------------------------------
打包版必然缺 faster-whisper，即便模型文件"就绪"，转写也照样不可用。把二者
合成一项会让用户以为"下了模型就能转写"，那是误导。这里分成 ``asr_engine``
与 ``whisper_model`` 两条独立结论。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LEVEL_OK",
    "LEVEL_WARN",
    "LEVEL_BLOCK",
    "FIX_DOWNLOAD",
    "FIX_INSTALL_ENGINE",
    "FIX_OPEN_URL",
    "FIX_NAVIGATE",
    "FIX_NONE",
    "EnvFix",
    "EnvCheckItem",
    "EnvReport",
    "check_environment",
]

#: 三档严重度：ok 正常；warn 缺的是可选能力（功能降级但能干活）；
#: block 缺的是必需品（干不了活）。
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_BLOCK = "block"

_ORDER = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_BLOCK: 2}

#: 修复动作的种类。
#:
#: - ``download_model``：应用内下载**模型文件**（走 model_manager，curl）；
#: - ``install_engine``：应用内安装**运行期依赖**（走 deps_manager，下载 wheel
#:   并解压到 exe 同级的 ``deps/``）——这是给"转写引擎"用的，与下模型是两回事；
#: - ``open_url`` / ``navigate``：只能引导，应用内做不了；
#: - ``none``：纯告知，连按钮都不画。
FIX_DOWNLOAD = "download_model"   # payload = 模型 key（spec_by_key 能解析）
FIX_INSTALL_ENGINE = "install_engine"  # payload = ""（清单在 deps_manager 里）
FIX_OPEN_URL = "open_url"         # payload = 要打开的网址
FIX_NAVIGATE = "navigate"         # payload = 目标对话框标识（如 "models"）
FIX_NONE = "none"


@dataclass(frozen=True)
class EnvFix:
    """一条可执行的补救动作。

    ``kind`` 决定界面怎么处理，也决定"这条到底能不能在应用内修好"：只有
    :data:`FIX_DOWNLOAD` 与 :data:`FIX_INSTALL_ENGINE` 会真的动手装，
    其余都是引导。
    """

    kind: str
    label: str          # 按钮文案，如 "下载 whisper-small"
    payload: str = ""   # 模型 key / URL / 目标页标识


@dataclass(frozen=True)
class EnvCheckItem:
    """一项环境结论。"""

    key: str            # 稳定标识，测试与跳转靠它
    label: str          # 中文名
    level: str          # LEVEL_*
    summary: str        # 一句话结论
    detail: str = ""    # 细节（路径、版本、缺什么）
    fix: EnvFix | None = None
    required: bool = True   # 是否阻断性（决定整体可用性）

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "level": self.level,
            "summary": self.summary,
            "detail": self.detail,
            "fix": (
                {"kind": self.fix.kind, "label": self.fix.label, "payload": self.fix.payload}
                if self.fix is not None
                else None
            ),
            "required": self.required,
        }


@dataclass(frozen=True)
class EnvReport:
    """一次自检的完整结果。"""

    items: tuple[EnvCheckItem, ...] = field(default_factory=tuple)

    @property
    def level(self) -> str:
        """整体严重度：取所有项里最严重的那一档。"""
        worst = LEVEL_OK
        for item in self.items:
            if _ORDER[item.level] > _ORDER[worst]:
                worst = item.level
        return worst

    @property
    def usable(self) -> bool:
        """能否正常干活：没有任何 required 项处于 block。"""
        return not any(i.required and i.level == LEVEL_BLOCK for i in self.items)

    def by_key(self, key: str) -> EnvCheckItem | None:
        return next((i for i in self.items if i.key == key), None)


# ---------------------------------------------------------------------------
# 单项检测
# ---------------------------------------------------------------------------


def _module_available(name: str) -> bool:
    """试探某个包能否导入。用 importlib.util.find_spec，不真正执行模块顶层代码。

    为什么不用 ``import name``：cv2 / llama_cpp 这类模块顶层可能很重，检测阶段
    不该把它们真的加载起来。find_spec 只查能否找到，代价小得多。
    """
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _check_ffmpeg() -> EnvCheckItem:
    from .ffmpeg import FfmpegNotFound, check_encoders, locate_ffmpeg

    try:
        binaries = locate_ffmpeg()
        check_encoders(binaries)
    except FfmpegNotFound as exc:
        return EnvCheckItem(
            key="ffmpeg",
            label="FFmpeg 工具链",
            level=LEVEL_BLOCK,
            summary="未找到 ffmpeg / ffprobe，导入与导出都不可用",
            detail=str(exc),
            fix=EnvFix(
                kind=FIX_OPEN_URL,
                label="打开下载页",
                payload="https://www.gyan.dev/ffmpeg/builds/",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 自检不该因单项失败而中断
        return EnvCheckItem(
            key="ffmpeg",
            label="FFmpeg 工具链",
            level=LEVEL_BLOCK,
            summary="FFmpeg 调用失败",
            detail=str(exc),
            fix=EnvFix(kind=FIX_NONE, label=""),
        )

    detail = f"{binaries.ffmpeg}"
    fix = None
    if not binaries.has_libx264 or not binaries.has_aac:
        missing = [n for n, ok in (("libx264", binaries.has_libx264),
                                   ("aac", binaries.has_aac)) if not ok]
        # 编码器缺失不阻断运行（能分析、能预览），但导出会失败——算 warn。
        return EnvCheckItem(
            key="ffmpeg",
            label="FFmpeg 工具链",
            level=LEVEL_WARN,
            summary=f"缺少编码器：{'、'.join(missing)}，精确导出不可用",
            detail=detail,
            fix=EnvFix(
                kind=FIX_OPEN_URL,
                label="打开下载页",
                payload="https://www.gyan.dev/ffmpeg/builds/",
            ),
        )
    return EnvCheckItem(
        key="ffmpeg", label="FFmpeg 工具链", level=LEVEL_OK,
        summary=f"就绪{'（' + binaries.version + '）' if binaries.version else ''}",
        detail=detail,
    )


def _check_ffplay() -> EnvCheckItem:
    """ffplay 只用于预览出声，缺失不算问题（画面照常播）。"""
    from .ffmpeg import FfmpegNotFound, locate_ffmpeg

    try:
        binaries = locate_ffmpeg()
    except FfmpegNotFound:
        return EnvCheckItem(
            key="ffplay", label="预览声音（ffplay）", level=LEVEL_WARN, required=False,
            summary="FFmpeg 未就位，无法判断 ffplay",
        )
    if binaries.ffplay is None:
        return EnvCheckItem(
            key="ffplay", label="预览声音（ffplay）", level=LEVEL_WARN, required=False,
            summary="未找到 ffplay，预览将没有声音（画面不受影响）",
            detail="精简版 FFmpeg 构建可能不含 ffplay；完整构建自带。",
        )
    return EnvCheckItem(
        key="ffplay", label="预览声音（ffplay）", level=LEVEL_OK, required=False,
        summary="就绪",
    )


def _check_asr_engine() -> EnvCheckItem:
    """语音转写引擎（faster-whisper）。

    **这条与 ``whisper_model`` 必须分开**：打包版有意不含 faster-whisper，
    此时即便模型文件都在，转写也不可用。

    可修性分三种处境，动作各不相同：

    - 引擎已在系统 Python 环境里（源码版）→ 就绪，无动作；
    - 引擎在应用内装的 deps 里 → 就绪，无动作；
    - 都没有 → 给「安装转写引擎」（``install_engine``）：应用内下载官方
      wheel 到 exe 同级的 ``deps/``，不需要 pip、不需要编译器。

    第三种的按钮**不是** ``download_model``——缺的是包不是模型，下模型解决
    不了。给 ``install_engine`` 才是对的；这也让"只有模型能在应用内一键修"
    的旧说法就此更新（现在依赖也能装了，见 deps_manager）。
    """
    # 1) 应用内安装的引擎（优先判断：源码版与打包版都可能用它）
    try:
        from .deps_manager import deps_installed, deps_present, default_deps_root

        if deps_present():
            if deps_installed():
                return EnvCheckItem(
                    key="asr_engine", label="语音转写引擎（faster-whisper）",
                    level=LEVEL_OK, summary="就绪（应用内安装）",
                    detail=str(default_deps_root()),
                )
            # 目录在、包导入不了：多半是缺 DLL 或版本不匹配。给"重新安装"的出口。
            return EnvCheckItem(
                key="asr_engine", label="语音转写引擎（faster-whisper）",
                level=LEVEL_BLOCK,
                summary="已下载但无法加载（可能缺 DLL 或版本不匹配）",
                detail=f"可重新安装以修复。引擎目录：{default_deps_root()}",
                fix=EnvFix(kind=FIX_INSTALL_ENGINE, label="重新安装转写引擎"),
            )
    except Exception:  # noqa: BLE001 - 探测依赖目录失败不应影响自检
        pass

    # 2) 系统 Python 环境里已有（源码版运行的情形）
    if _module_available("faster_whisper"):
        return EnvCheckItem(
            key="asr_engine", label="语音转写引擎（faster-whisper）",
            level=LEVEL_OK, summary="就绪",
        )

    # 3) 都没有：给应用内安装的出口
    return EnvCheckItem(
        key="asr_engine", label="语音转写引擎（faster-whisper）",
        level=LEVEL_BLOCK,
        summary="未安装 faster-whisper，无法做语音转写",
        detail=(
            "语音转写需要这个引擎，而它不在安装包里（体积原因）。"
            "点右侧按钮可在应用内安装：程序会下载官方预编译组件（约 80 MB）"
            "放进一个独立目录，装好后转写就能用，无需 pip 或编译器。"
        ),
        fix=EnvFix(kind=FIX_INSTALL_ENGINE, label="安装转写引擎"),
    )


def _check_whisper_model(
    models_root: Path | None, tier: str | None, *, engine_ok: bool = True
) -> EnvCheckItem:
    """转写模型文件。

    ``engine_ok`` 是转写引擎（faster-whisper）的可用性。引擎装不上时，即便模型
    下载齐了也用不了——所以此时**不给出下载按钮**，而是如实说明"先解决引擎问题"。
    否则用户点完"下载"、等几个 GB 下完，发现还是用不了，那是最糟的体验。
    """
    from .asr import locate_model
    from .model_manager import default_whisper_tier

    tier = tier or default_whisper_tier()
    info = locate_model(tier)
    key = f"whisper:{tier}"
    if info is not None:
        return EnvCheckItem(
            key="whisper_model", label=f"转写模型（whisper-{tier}）",
            level=LEVEL_OK, summary=f"就绪 {info.describe()}",
            detail=str(info.path),
        )
    if not engine_ok:
        # 引擎缺失：模型能下，但下了也没用。如实说清楚，不给下载按钮。
        return EnvCheckItem(
            key="whisper_model", label=f"转写模型（whisper-{tier}）",
            level=LEVEL_BLOCK,
            summary="未下载（且转写引擎未安装，下载后仍不可用）",
            detail="请先解决上面的「语音转写引擎」问题，再下载模型——否则白下几个 GB。",
            fix=EnvFix(kind=FIX_NONE, label=""),
        )
    # 引擎就绪、只是缺模型：这是**唯一**可一键修复的场景。
    return EnvCheckItem(
        key="whisper_model", label=f"转写模型（whisper-{tier}）",
        level=LEVEL_BLOCK,
        summary=f"未下载（whisper-{tier}）",
        detail="可在应用内下载，无需联网配置。",
        fix=EnvFix(kind=FIX_DOWNLOAD, label=f"下载 whisper-{tier}", payload=key),
    )


def _check_llm_model() -> EnvCheckItem:
    from .semantic import locate_llm_model

    llm = locate_llm_model()
    if llm is not None:
        return EnvCheckItem(
            key="llm_model", label="语义模型（本地）", level=LEVEL_OK, required=False,
            summary=f"就绪 {llm.name}", detail=str(llm),
        )
    return EnvCheckItem(
        key="llm_model", label="语义模型（本地）", level=LEVEL_WARN, required=False,
        summary="未下载，语义判断将降级（可用规则与静音分集）",
        fix=EnvFix(
            kind=FIX_DOWNLOAD, label="下载语义模型", payload="llm:2B:Q4_K_M",
        ),
    )


def _check_llm_stack() -> EnvCheckItem:
    """本地语义推理栈（llama.cpp / Ollama）。缺失只降级，不阻断。

    这一项**给不了** ``download_model``——缺的是 Python 包而不是模型，下模型
    解决不了。但也不能只丢一句"装不上"：官方发布页有 ``py3-none-win_amd64``
    的**预编译轮子**（``py3-none`` 不绑定具体 Python 版本，7 MB 的 CPU 版也
    在内），没编译器的用户下回来 ``pip install`` 即可。所以给 ``open_url``
    指向发布页——与 FFmpeg 那条同一处理：应用装不了，但把路指清楚。
    """
    from .semantic import _import_llama

    if _import_llama() is not None:
        return EnvCheckItem(
            key="llm_stack", label="本地语义推理栈", level=LEVEL_OK, required=False,
            summary="就绪（llama.cpp）",
        )
    return EnvCheckItem(
        key="llm_stack", label="本地语义推理栈", level=LEVEL_WARN, required=False,
        summary="未安装 llama-cpp-python，本地语义判断不可用",
        detail=(
            "可在发布页下载预编译轮子（win_amd64 有 py3-none 标签，不挑 Python "
            "版本；CPU 版约 7 MB，CUDA 版更大），装好后本地语义判断即生效。"
            "也可以改用云端语义，或直接装 Ollama。"
        ),
        fix=EnvFix(
            kind=FIX_OPEN_URL,
            label="打开下载页",
            payload="https://github.com/abetlen/llama-cpp-python/releases",
        ),
    )


def _check_optional_packages() -> list[EnvCheckItem]:
    """可选能力依赖：缺失只降级，逐项如实说明。"""
    items: list[EnvCheckItem] = []
    specs = (
        ("scenedetect", "镜头检测（scenedetect）",
         "未安装 scenedetect，镜头切换检测跳过（仍可凭对白与静音分集）"),
        ("zhconv", "繁简转换（zhconv）",
         "未安装 zhconv，无法保证字幕输出为简体中文"),
        ("pysubs2", "ASS/SSA 字幕（pysubs2）",
         "未安装 pysubs2，ASS/SSA 复杂样式字幕无法解析"),
    )
    for module, label, warn in specs:
        ok = _module_available(module)
        items.append(
            EnvCheckItem(
                key=module, label=label,
                level=LEVEL_OK if ok else LEVEL_WARN, required=False,
                summary="就绪" if ok else warn,
                fix=None if ok else EnvFix(kind=FIX_NONE, label=""),
            )
        )
    return items


def _check_models_dir(models_root: Path | None) -> EnvCheckItem:
    """检查模型目录可用性——**不创建目录**。

    刻意不用 ``model_manager.validate_models_root``：那个函数会 ``mkdir`` 出
    目录来试写。检测不该有副作用——用户只是打开"环境自检"看一眼，不该在
    exe 旁边凭空多一个空的 ``models/``。所以这里自己做一个只读判定：

    - 目录已存在：在里面试写一个探针再删（瞬时、且在既有目录内）；
    - 目录不存在：退到最近的既有祖先，看它可不可写（``os.access``）。
      缺失本身不算问题——首次下载模型时程序会自己建。
    """
    from .app_config import default_models_root

    root = models_root or default_models_root()
    root = Path(root)

    if root.exists():
        probe = root / ".write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return EnvCheckItem(
                key="models_dir", label="模型目录", level=LEVEL_BLOCK,
                summary=f"目录不可写：{exc}", detail=str(root),
                fix=EnvFix(kind=FIX_NAVIGATE, label="打开模型管理", payload="models"),
            )
        return EnvCheckItem(
            key="models_dir", label="模型目录", level=LEVEL_OK,
            summary="可写", detail=str(root),
        )

    # 目录还没建：检查最近的既有祖先是否可写
    ancestor = root.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists() and os.access(ancestor, os.W_OK):
        return EnvCheckItem(
            key="models_dir", label="模型目录", level=LEVEL_OK,
            summary="尚未创建，首次下载时自动建立", detail=str(root),
        )
    return EnvCheckItem(
        key="models_dir", label="模型目录", level=LEVEL_BLOCK,
        summary=f"上级目录不可写：{ancestor}", detail=str(root),
        fix=EnvFix(kind=FIX_NAVIGATE, label="打开模型管理", payload="models"),
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def check_environment(
    *, models_root: str | Path | None = None, tier: str | None = None
) -> EnvReport:
    """跑一遍完整自检，返回带结论与修复建议的报告。

    本函数**只读**：不下载、不安装、不改任何文件。修复动作由调用方（GUI）
    在用户确认后另行执行——检测与修复必须分开，否则打开对话框就会偷偷下载。
    """
    root = Path(models_root) if models_root else None
    engine = _check_asr_engine()
    items: list[EnvCheckItem] = [
        _check_ffmpeg(),
        _check_ffplay(),
        engine,
        _check_whisper_model(root, tier, engine_ok=engine.level != LEVEL_BLOCK),
        _check_llm_model(),
        _check_llm_stack(),
        *_check_optional_packages(),
        _check_models_dir(root),
    ]
    return EnvReport(items=tuple(items))
