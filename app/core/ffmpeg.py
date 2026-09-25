"""FFmpeg / FFprobe 定位与进程调用封装。

设计依据（方案 §6、§14.1、§15.3）：
- 后端只做"调用进程 + 解析输出"，不自行构造复杂滤镜图。
- 定位失败、编码器缺失必须在导入时暴露为兼容性提示，而不是等到导出才炸。
- 长任务必须可取消；取消时终止子进程，不留半成品（由导出器负责清理）。
"""

from __future__ import annotations

import json
import os
import shutil
import re as _RE
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

__all__ = [
    "FfmpegNotFound",
    "FfmpegError",
    "FfmpegBinaries",
    "locate_ffmpeg",
    "run_ffprobe_json",
    "run_ffmpeg",
    "check_encoders",
]

# Windows 上必须隐藏控制台窗口，否则每次调用都会闪黑框
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 已知的常见安装位置（winget / scoop / chocolatey）
_FALLBACK_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages",
    Path("C:/ffmpeg/bin"),
    Path("C:/Program Files/ffmpeg/bin"),
)


class FfmpegNotFound(RuntimeError):
    """找不到 ffmpeg / ffprobe 可执行文件。"""


class FfmpegError(RuntimeError):
    """FFmpeg 进程返回非零退出码。"""

    def __init__(self, message: str, returncode: int, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


@dataclass
class FfmpegBinaries:
    """已定位的 FFmpeg 工具链。"""

    ffmpeg: Path
    ffprobe: Path
    version: str = ""
    encoders: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_libx264(self) -> bool:
        return "libx264" in self.encoders

    @property
    def has_libx265(self) -> bool:
        return "libx265" in self.encoders

    @property
    def has_aac(self) -> bool:
        return "aac" in self.encoders


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    return None


def _search_fallback(name: str) -> Path | None:
    exe = f"{name}.exe" if sys.platform == "win32" else name
    for base in _FALLBACK_DIRS:
        if not base.exists():
            continue
        # WinGet 包目录结构为 <base>/<pkg>/<ver>/bin/<exe>
        try:
            for candidate in base.glob(f"**/bin/{exe}"):
                return candidate
        except OSError:
            continue
    return None


def locate_ffmpeg(explicit_dir: str | Path | None = None) -> FfmpegBinaries:
    """定位 FFmpeg 工具链。

    查找顺序：显式目录 → 环境变量 DRAMA_FFMPEG_DIR → PATH → 已知安装位置。
    全部失败时抛 FfmpegNotFound，由界面提示用户指定目录。
    """
    candidate_dirs: list[Path] = []
    if explicit_dir:
        candidate_dirs.append(Path(explicit_dir))
    env_dir = os.environ.get("DRAMA_FFMPEG_DIR")
    if env_dir:
        candidate_dirs.append(Path(env_dir))

    for directory in candidate_dirs:
        ffmpeg = directory / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        ffprobe = directory / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if ffmpeg.exists() and ffprobe.exists():
            return _finalize(ffmpeg, ffprobe)

    ffmpeg = _which("ffmpeg") or _search_fallback("ffmpeg")
    ffprobe = _which("ffprobe") or _search_fallback("ffprobe")
    if ffmpeg and ffprobe:
        return _finalize(ffmpeg, ffprobe)

    raise FfmpegNotFound(
        "未找到 ffmpeg / ffprobe。请安装 FFmpeg 并加入 PATH，"
        "或设置环境变量 DRAMA_FFMPEG_DIR 指向其 bin 目录。"
    )


def _finalize(ffmpeg: Path, ffprobe: Path) -> FfmpegBinaries:
    version = ""
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            timeout=30,
        )
        first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        if first_line.startswith("ffmpeg version "):
            version = first_line[len("ffmpeg version ") :].split()[0]
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    return FfmpegBinaries(ffmpeg=ffmpeg, ffprobe=ffprobe, version=version)


def check_encoders(binaries: FfmpegBinaries, names: Sequence[str] = ("libx264", "aac")) -> frozenset[str]:
    """核验编码器可用性，结果写回 binaries.encoders。

    方案 §6：编码能力必须在导入时探测；缺失时提示用其它预设，而不是导出时报错。
    """
    available: set[str] = set()
    try:
        proc = subprocess.run(
            [str(binaries.ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()

    wanted = set(names)
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        # 形如 " V..... libx264              libx264 H.264 / AVC ..."
        if len(parts) >= 2 and parts[0][:1] in {"V", "A", "S"}:
            name = parts[1]
            if name in wanted:
                available.add(name)

    binaries.encoders = frozenset(available)
    return binaries.encoders


def run_ffprobe_json(
    binaries: FfmpegBinaries,
    args: Sequence[str],
    timeout: float = 120.0,
) -> dict:
    """执行 ffprobe 并返回解析后的 JSON。"""
    cmd = [
        str(binaries.ffprobe),
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        *args,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise FfmpegError(
            f"ffprobe 失败（退出码 {proc.returncode}）: {' '.join(cmd)}",
            proc.returncode,
            (proc.stderr or "").strip()[-2000:],
        )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - 极少发生
        raise FfmpegError(f"ffprobe 输出不是合法 JSON: {exc}", proc.returncode, proc.stdout[:500]) from exc


def run_ffmpeg(
    binaries: FfmpegBinaries,
    args: Sequence[str],
    *,
    on_progress: Callable[[float], None] | None = None,
    duration_seconds: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float | None = 3600.0,
) -> None:
    """执行一次 FFmpeg 编码任务。

    参数：
        on_progress: 回调，接收 0.0–1.0 的进度（需提供 duration_seconds）。
        cancel_check: 返回 True 时终止进程。方案 §15.2 要求可安全取消。
    异常：
        FfmpegError：非零退出码。
        RuntimeError：被取消。
    """
    cmd = [
        str(binaries.ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-stats_period",
        "0.5",
        *args,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
        bufsize=1,
    )

    cancelled = False
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if cancel_check and cancel_check():
                cancelled = True
                proc.terminate()
                break
            if on_progress and duration_seconds and line.startswith("out_time_us="):
                value = line.split("=", 1)[1].strip()
                if value.isdigit():
                    seconds = int(value) / 1_000_000
                    on_progress(max(0.0, min(1.0, seconds / duration_seconds)))
        if not cancelled:
            proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise FfmpegError("FFmpeg 执行超时", -1, "timeout")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr_tail = ""
    if proc.stderr is not None:
        try:
            stderr_tail = (proc.stderr.read() or "").strip()[-2000:]
        except (OSError, ValueError):
            stderr_tail = ""

    if cancelled:
        raise RuntimeError("任务已取消")
    if proc.returncode != 0:
        raise FfmpegError(
            f"FFmpeg 失败（退出码 {proc.returncode}）",
            proc.returncode,
            stderr_tail,
        )


def probe_hw_encoders(binaries: "Binaries") -> dict:
    """探测硬件 H.264 编码器的**存在性**（§ 硬件兼容）。

    注意：`-encoders` 列表里有，不代表当前驱动可用——NVENC 需要显卡驱动、
    QSV 需要核显驱动。因此返回值只用于"是否可以尝试"，真正可用性应由
    一次 1 帧试编码确认（调用方决定是否做）。
    """
    import subprocess as _sp

    command = [str(binaries.ffmpeg), "-hide_banner", "-encoders"]
    proc = _sp.run(command, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", creationflags=_CREATE_NO_WINDOW)
    listing = proc.stdout or ""
    targets = {
        "h264_nvenc": "NVIDIA NVENC",
        "h264_qsv": "Intel QSV",
        "h264_amf": "AMD AMF",
        "h264_videotoolbox": "VideoToolbox",
    }
    found = {}
    for codec, label in targets.items():
        # 按词边界匹配，避免 h264_qsv 误匹配到别的编码器名
        if _re_search(rf"\b{codec}\b", listing):
            found[codec] = label
    return found


def _re_search(pattern: str, text: str) -> bool:
    return _RE.compile(pattern).search(text) is not None
