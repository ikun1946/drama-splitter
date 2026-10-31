"""首次运行的 FFmpeg 自动配置：下载官方构建、解压、校验、落到应用目录。

为什么放在应用内而不是安装包里
------------------------------
安装包不捆绑 FFmpeg：GPL 全量构建三个 exe 约 500 MB，且随包分发 GPL 二进制会让
整个分发物承担 GPL 义务。但用户也不该被要求自己找下载页、解压、改 PATH——首次
启动时由本模块下载到 ``<app_dir>/ffmpeg/bin``，之后 ``locate_ffmpeg`` 直接命中。

下载源
------
1. BtbN 的 win64 GPL 构建（约 200 MB，含 ffmpeg / ffprobe / ffplay、libx264、aac）；
2. 失败或限速时回退 gyan.dev 的 release essentials（约 115 MB，同样包含三个
   exe 与所需编码器）。

两个源都是滚动发布，没有稳定可引用的 SHA-256；完整性靠 HTTPS + 落盘后实际运行
``-version`` / ``-encoders`` 校验。校验不通过就换下一个源，绝不把坏文件留在目标目录。

落点与失败处理
--------------
先解压到临时目录，校验通过后再原子替换正式 ``bin/``；失败时保留旧目录、清理临时
文件与压缩包。取消下载保留 ``.part``，下次启动可续传。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .app_config import app_dir
from .ffmpeg import FfmpegNotFound, check_encoders, locate_ffmpeg
from .model_manager import free_space_bytes, remote_size

__all__ = [
    "DOWNLOAD_URLS",
    "MIN_FREE_BYTES",
    "FfmpegInstallResult",
    "extract_ffmpeg_zip",
    "install_ffmpeg",
    "managed_bin",
    "managed_root",
    "remember_ffmpeg_dir",
    "validate_ffmpeg_dir",
]

#: 下载源，按顺序尝试。都包含 ffmpeg / ffprobe / ffplay 与 libx264、aac。
#: BtbN 放前面是因为它在本机实测约 50 MB/s；gyan.dev 走 Cloudflare，实测可能只有
#: 十几 KB/s。限速检测（_download_one 的 30 秒阈值）会在慢源上自动切到下一个。
DOWNLOADS: tuple[str, ...] = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
)

#: 兼容旧名字（测试与调用方都用 URL 列表）。
DOWNLOAD_URLS = DOWNLOADS

#: 下载 + 解压的最低可用空间：zip 约 115–200 MB，三个静态 exe 合计约 0.5 GB。
MIN_FREE_BYTES = 800 * 1024 * 1024

#: ffprobe 必需，ffplay 只用于预览出声，精简构建可能不带。
_REQUIRED = ("ffmpeg.exe", "ffprobe.exe")
_OPTIONAL = ("ffplay.exe",)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

@dataclass(frozen=True)
class FfmpegInstallResult:
    """一次自动配置的结果。永不抛异常，失败原因放在 ``message``。"""

    ok: bool
    cancelled: bool = False
    message: str = ""
    directory: Path | None = None
    version: str = ""
    downloaded_bytes: int = 0

    def describe(self) -> str:
        if self.ok:
            return self.message or f"FFmpeg 已配置到 {self.directory}"
        if self.cancelled:
            return self.message or "FFmpeg 下载已取消（已下载部分保留，重试可续传）"
        return f"FFmpeg 配置失败：{self.message}"

def managed_root() -> Path:
    """自动下载的 FFmpeg 根目录：与 models/、deps/ 同级。"""
    return app_dir() / "ffmpeg"

def managed_bin() -> Path:
    return managed_root() / "bin"

def _download_dir() -> Path:
    return managed_root() / "download"

def validate_ffmpeg_dir(directory: str | Path) -> tuple[bool, str]:
    """校验一个目录能否作为 FFmpeg 工具链使用。

    返回 ``(是否可用, 可读原因)``。要求 ffmpeg 与 ffprobe 同时存在，并且实际跑得
    起来、带 libx264 与 aac；缺任意一项都拒绝，避免用户以为配好了、导出时才失败。
    """
    path = Path(directory)
    if path.is_file():
        path = path.parent
    missing = [
        exe for exe in ("ffmpeg.exe", "ffprobe.exe")
        if not (path / exe).exists()
    ]
    if missing:
        return False, f"所选目录缺少：{'、'.join(missing)}"
    try:
        binaries = locate_ffmpeg(explicit_dir=path)
        check_encoders(binaries)
    except FfmpegNotFound as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - 校验失败要给原因，不能把异常抛给界面
        return False, f"无法运行所选 FFmpeg：{exc}"
    missing_enc = [
        name for name, ok in (("libx264", binaries.has_libx264), ("aac", binaries.has_aac))
        if not ok
    ]
    if missing_enc:
        return False, f"所选 FFmpeg 缺少编码器：{'、'.join(missing_enc)}"
    return True, f"已就绪（{binaries.version or '版本未知'}）"

def remember_ffmpeg_dir(directory: str | Path) -> None:
    """把手选的 FFmpeg 目录写进配置，后续启动直接命中。"""
    from .app_config import AppConfig

    config = AppConfig.load()
    config.ffmpeg_dir = str(Path(directory))
    config.save()

def _normalized_members(zf: zipfile.ZipFile) -> dict[str, str]:
    """zip 成员名 → 原始名。统一分隔符，兼容 Windows 生成的包。"""
    return {name.replace("\\", "/"): name for name in zf.namelist()}

def _member_for(members: dict[str, str], exe: str) -> str | None:
    """找一个 exe 的成员名：优先 ``*/bin/<exe>``，也接受根目录平铺。"""
    tail = f"/bin/{exe}"
    for normalized, original in members.items():
        if normalized.endswith(tail):
            return original
    for normalized, original in members.items():
        if normalized == exe or normalized.endswith(f"/{exe}"):
            return original
    return None

def extract_ffmpeg_zip(zip_path: str | Path, dest: str | Path) -> list[str]:
    """只解出 ffmpeg / ffprobe / ffplay 三个 exe。

    不整包解压：构建包里还有几百 MB 的文档、头文件与其它工具，用户用不到。
    缺少 ffmpeg 或 ffprobe 时抛 ``ValueError``（调用方据此换下一个源）。
    """
    target = Path(dest)
    target.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        members = _normalized_members(zf)
        for exe in (*_REQUIRED, *_OPTIONAL):
            member = _member_for(members, exe)
            if member is None:
                if exe in _REQUIRED:
                    raise ValueError(f"压缩包内未找到 {exe}")
                continue
            with zf.open(member) as src, open(target / exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(exe)
    return extracted

def _download_one(
    url: str,
    part: Path,
    total: int,
    *,
    on_progress: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> tuple[bool, bool, str, int]:
    """下载到 ``part``，返回 (成功, 取消, 原因, 已下载字节)。"""
    part.parent.mkdir(parents=True, exist_ok=True)
    if total and part.exists() and part.stat().st_size > total:
        part.unlink(missing_ok=True)

    def run(resume: bool) -> tuple[int, str]:
        command = [
            "curl", "-sS", "-L", "--fail",
            "--noproxy", "localhost,127.0.0.1",
            "-o", str(part),
        ]
        if resume:
            command.extend(["-C", "-"])
        command.append(url)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            return -1, str(exc)

        started = time.monotonic()
        initial = part.stat().st_size if part.exists() else 0
        last_report = 0.0
        try:
            while True:
                if cancel_check and cancel_check():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return -2, ""
                code = process.poll()
                current = part.stat().st_size if part.exists() else 0
                now = time.monotonic()
                if on_progress and (now - last_report > 0.2 or code is not None):
                    on_progress(current, total)
                    last_report = now
                # 源被限速时不要干等：30 秒还不到 0.5 MB，就换下一个源。
                # 这里同时兜住「服务器先卡住再开始传」的少数情况——多等 30 秒
                # 换来一次换源机会，比让用户盯着 15 KB/s 看一小时好。
                if (
                    code is None
                    and now - started > 30
                    and current - initial < 512 * 1024
                    and (not total or current < total - 1024 * 1024)
                ):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return -3, ""
                if code is not None:
                    break
                time.sleep(0.1)
            detail = ""
            if code != 0:
                detail = (process.stderr.read() or b"").decode("utf-8", "replace").strip()
            return code, detail
        finally:
            if process.poll() is None:
                process.kill()

    # 续传被服务器拒绝（curl 33）时，删掉残片从头下载一次。
    code, detail = run(resume=part.exists())
    if code == 33 and part.exists():
        part.unlink(missing_ok=True)
        code, detail = run(resume=False)

    size = part.stat().st_size if part.exists() else 0
    if code == -2:
        return False, True, "已取消下载", size
    if code == -3:
        return False, False, "下载速度过慢，切换下载源", size
    if code != 0:
        return False, False, f"下载失败（curl 退出码 {code}）{detail[:200]}".strip(), size
    if total and size < total:
        return False, False, f"下载不完整：{size}/{total} 字节", size
    return True, False, "", size

def _install_from_zip(zip_path: Path) -> tuple[bool, str, Path | None, str]:
    """解压、校验并替换正式 bin 目录。返回 (成功, 原因, 目录, 版本)。"""
    root = managed_root()
    temp = root / ".incoming"
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    try:
        extract_ffmpeg_zip(zip_path, temp)
        binaries = locate_ffmpeg(explicit_dir=temp)
        check_encoders(binaries)
        missing = [
            name for name, ok in (("libx264", binaries.has_libx264), ("aac", binaries.has_aac))
            if not ok
        ]
        if missing:
            return False, f"下载的构建缺少编码器：{'、'.join(missing)}", None, ""
    except (ValueError, zipfile.BadZipFile, FfmpegNotFound, OSError) as exc:
        shutil.rmtree(temp, ignore_errors=True)
        return False, f"压缩包不可用：{exc}", None, ""
    except Exception as exc:  # noqa: BLE001 - 校验失败照样换源，不能让异常冒出去
        shutil.rmtree(temp, ignore_errors=True)
        return False, f"校验失败：{exc}", None, ""

    target = managed_bin()
    old = root / ".bin-old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists():
            target.rename(old)
        temp.rename(target)
    except OSError as exc:
        if not target.exists() and old.exists():
            try:
                old.rename(target)
            except OSError:
                pass
        shutil.rmtree(temp, ignore_errors=True)
        return False, f"无法写入目标目录 {target}：{exc}", None, ""
    shutil.rmtree(old, ignore_errors=True)
    return True, "", target, binaries.version

def install_ffmpeg(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    urls: Sequence[str] = DOWNLOADS,
) -> FfmpegInstallResult:
    """下载并安装 FFmpeg 到 ``<app_dir>/ffmpeg/bin``。

    同步阻塞（约 115 MB），GUI 必须放到后台线程。**永不抛异常**：结果里写明
    成功 / 取消 / 失败原因。
    """
    if sys.platform != "win32":
        return FfmpegInstallResult(ok=False, message="自动配置目前只支持 Windows")

    root = managed_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FfmpegInstallResult(
            ok=False, message=f"无法创建目录 {root}：{exc}（可改用「选择已有 FFmpeg」）"
        )

    free = free_space_bytes(root)
    if free is not None and free < MIN_FREE_BYTES:
        return FfmpegInstallResult(
            ok=False,
            message=(
                f"磁盘空间不足：需要约 {MIN_FREE_BYTES // 1024 // 1024} MB，"
                f"可用 {free // 1024 // 1024} MB"
            ),
        )

    download_dir = _download_dir()
    downloaded = 0
    last_error = "没有可用的下载源"

    for url in urls:
        # 残片按 URL 分开存：换源时不能把上一个源的字节续到另一个文件上。
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        part = download_dir / f"ffmpeg-{digest}.zip.part"
        total = remote_size(url) or 0
        if on_progress:
            on_progress(part.stat().st_size if part.exists() else 0, total)
        ok, cancelled, message, size = _download_one(
            url,
            part,
            total,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )
        downloaded = size
        if cancelled:
            return FfmpegInstallResult(
                ok=False, cancelled=True, message="已取消下载", downloaded_bytes=downloaded
            )
        if not ok:
            last_error = message
            part.unlink(missing_ok=True)
            continue

        ok, message, directory, version = _install_from_zip(part)
        if ok:
            part.unlink(missing_ok=True)
            shutil.rmtree(download_dir, ignore_errors=True)
            # 自动安装的目录优先于旧的手选目录：否则用户修完「编码器缺失」，
            # 下次启动仍会命中那个不完整的旧路径。
            from .app_config import AppConfig

            config = AppConfig.load()
            if config.ffmpeg_dir:
                config.ffmpeg_dir = None
                config.save()
            return FfmpegInstallResult(
                ok=True,
                message=f"FFmpeg 已配置到 {directory}",
                directory=directory,
                version=version,
                downloaded_bytes=downloaded,
            )
        last_error = message
        part.unlink(missing_ok=True)

    return FfmpegInstallResult(
        ok=False, message=last_error or "下载失败", downloaded_bytes=downloaded
    )
