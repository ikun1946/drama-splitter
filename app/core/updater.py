"""应用内更新：查 GitHub Releases、下载安装包、校验完整性、启动安装器。

为什么传输层用 curl 而不是 cloud_http
-------------------------------------
``cloud_http.get_json`` 失败时抛 ``ProviderError``，而那套文案是**厂商导向**的
（带 logid、区分 401/403/429/5xx 的厂商说明）——用在 GitHub 上语义完全不对。
本模块的契约是"取不到就降级，绝不炸调用方"，正好对上 ``model_manager._curl``
的既有行为：返回 (退出码, 输出)，超时返回 28，不抛异常。

契约
----
本模块**所有公开函数都不抛异常**，一律返回带 ``ok`` 与可读 ``message`` 的结果。
更新检查是个可选的便利功能，它失败绝不能影响主程序干活。

安全边界
--------
**SHA-256 不匹配时必须删掉已下载的文件**，绝不把校验失败的包交给安装器。
需要说清楚的边界：``digest`` 字段本身来自同一个 API，能改 API 的人也能改
digest，所以这挡的是"传输损坏 / CDN 篡改"，挡不住"仓库被攻陷"。真正的防护
是代码签名证书，不在本次范围内。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from .model_manager import free_space_bytes

__all__ = [
    "RELEASE_API",
    "RELEASES_PAGE",
    "UpdateInfo",
    "CheckResult",
    "DownloadOutcome",
    "parse_version",
    "is_newer",
    "installed_copy",
    "check_for_update",
    "download_asset",
    "extract_portable_zip",
    "launch_installer",
    "mark_update_pending",
    "take_update_pending",
    "update_action_label",
]

ROOT = Path(__file__).resolve().parent.parent.parent

#: 公开仓库的 releases/latest **不需要鉴权**，且自动排除 draft 与 prerelease
#: （版本比较因此不必处理预发布分支）。未鉴权配额是 60 次/小时/IP。
RELEASE_API = "https://api.github.com/repos/ikun1946/drama-splitter/releases/latest"
RELEASES_PAGE = "https://github.com/ikun1946/drama-splitter/releases"

SETUP_SUFFIX = "-setup.exe"
ZIP_SUFFIX = "-win64.zip"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class UpdateInfo:
    """一次检查拿到的远端信息。"""

    tag: str
    version: str
    url: str
    notes: str
    setup_url: str
    setup_size: int
    setup_digest: str
    zip_url: str
    zip_size: int
    zip_digest: str

    def asset_for(self, installed: bool) -> tuple[str, int, str] | None:
        """按运行形态挑资产：装了就给安装包，没装就给免安装 zip。"""
        if installed:
            if self.setup_url:
                return self.setup_url, self.setup_size, self.setup_digest
            return None
        if self.zip_url:
            return self.zip_url, self.zip_size, self.zip_digest
        return None


@dataclass(frozen=True)
class CheckResult:
    """检查结果。``info`` 非空 = 确有新版。"""

    ok: bool
    info: UpdateInfo | None = None
    message: str = ""

    @property
    def has_update(self) -> bool:
        return self.info is not None


@dataclass(frozen=True)
class DownloadOutcome:
    ok: bool
    path: Path | None = None
    cancelled: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """把 ``v1.2.3`` / ``1.2.3`` 解析成 ``(1, 2, 3)``；解析不了返回 ``None``。

    刻意不抛：远端 tag 是什么形态不由我们控制，解析不了就当"没有新版"，
    宁可漏报也不要误报——后者会让用户反复看到"有新版"却装不上。
    """
    if not text:
        return None
    cleaned = str(text).strip().lstrip("vV")
    # 先剥掉预发布/构建后缀：``v1.0.0-rc1`` 的版本是 1.0.0，不是 1.0.1
    for sep in ("-", "+"):
        cleaned = cleaned.split(sep, 1)[0]
    parts = cleaned.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    numbers = []
    for part in parts:
        # 只取**前导**数字：用 isdigit 过滤会把 "0-rc1" 里的 1 也吸进来，
        # 变成 1.0.1 —— 比不解析还糟，会让用户反复看到"有新版"。
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            return None
        numbers.append(int(digits))
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def is_newer(remote: str | None, local: str | None) -> bool:
    """远端是否比本地新。任一侧解析不了就返回 ``False``。"""
    left = parse_version(remote)
    right = parse_version(local)
    if left is None or right is None:
        return False
    return left > right


def installed_copy() -> bool:
    """当前这份是安装版还是免安装版。

    判据是安装器会在 exe 旁边留下 ``uninstall/unins000.exe``；免安装的 zip
    解压出来没有这个目录。两者能用的更新方式不一样：安装版可以交给安装器做
    升级，免安装版只能让用户自己换文件。
    """
    from .app_config import app_dir

    return (app_dir() / "uninstall" / "unins000.exe").is_file()


# ---------------------------------------------------------------------------
# HTTP（走 curl，不抛）
# ---------------------------------------------------------------------------


def _curl(args: list[str], *, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--noproxy", "localhost,127.0.0.1", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=timeout,
        )
    except FileNotFoundError:
        return -2, ""
    except subprocess.TimeoutExpired:
        return 28, ""
    except OSError as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout or "").strip() or (proc.stderr or "").strip()


def _fetch_release(timeout: int) -> tuple[int, dict | None, str]:
    """取 releases/latest。返回 (退出码, 解析后的 dict, 诊断文本)。"""
    code, body = _curl(
        [
            "-H", "Accept: application/vnd.github+json",
            "-H", "User-Agent: drama-splitter-updater",
            "-w", "\n__HTTP__%{http_code}",
            "-o", "-",
            "--max-time", str(timeout),
            RELEASE_API,
        ],
        timeout=timeout + 10,
    )
    if code == -2:
        return code, None, "本机没有 curl，无法检查更新。请安装 curl 或确认 PATH。"
    if code == 28:
        return code, None, "连接 GitHub 超时。"
    if code != 0:
        return code, None, f"无法连接 GitHub（curl 退出码 {code}）。"

    http = 0
    body_text = body
    marker = "\n__HTTP__"
    if marker in body:
        body_text, _, tail = body.rpartition(marker)
        try:
            http = int(tail.strip())
        except ValueError:
            http = 0
    if http == 404:
        return 404, None, "发布地址已失效（仓库可能已转私有或改名）。"
    if http == 403 or http == 429:
        return 403, None, "GitHub 检查过于频繁，请稍后再试。"
    if http >= 400:
        return http, None, f"GitHub 返回 {http}。"

    try:
        payload = json.loads(body_text or "{}")
    except json.JSONDecodeError:
        return -3, None, "GitHub 返回的内容不是预期的 JSON。"
    if not isinstance(payload, dict):
        return -3, None, "GitHub 返回的内容格式异常。"
    return 0, payload, ""


def _asset_fields(assets: list) -> tuple[str, int, str, str, int, str]:
    """从资产列表里挑出安装包与免安装包各自的 (url, size, digest)。"""
    setup = ("", 0, "")
    portable = ("", 0, "")
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not url:
            continue
        size = int(item.get("size") or 0)
        digest = str(item.get("digest") or "")
        if name.endswith(SETUP_SUFFIX):
            setup = (url, size, digest)
        elif name.endswith(ZIP_SUFFIX):
            portable = (url, size, digest)
    return setup[0], setup[1], setup[2], portable[0], portable[1], portable[2]


def check_for_update(local_version: str, *, timeout: int = 15) -> CheckResult:
    """查有没有新版。**永不抛异常。**"""
    code, payload, diagnostic = _fetch_release(timeout)
    if payload is None:
        return CheckResult(ok=False, message=diagnostic or "检查更新失败。")

    tag = str(payload.get("tag_name") or "")
    if not tag:
        return CheckResult(ok=False, message="GitHub 没有返回版本号。")

    if not is_newer(tag, local_version):
        return CheckResult(ok=True, message=f"当前已是最新版本（{local_version}）。")

    assets = payload.get("assets")
    assets = assets if isinstance(assets, list) else []
    s_url, s_size, s_digest, z_url, z_size, z_digest = _asset_fields(assets)
    if not s_url and not z_url:
        return CheckResult(ok=False, message="该版本没有可下载的安装包。")

    info = UpdateInfo(
        tag=tag,
        version=tag.lstrip("vV") or tag,
        url=str(payload.get("html_url") or RELEASES_PAGE),
        notes=str(payload.get("body") or ""),
        setup_url=s_url,
        setup_size=s_size,
        setup_digest=s_digest,
        zip_url=z_url,
        zip_size=z_size,
        zip_digest=z_digest,
    )
    return CheckResult(ok=True, info=info, message=f"发现新版本 {info.version}。")


# ---------------------------------------------------------------------------
# 下载与校验
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_asset(
    url: str,
    target: Path,
    *,
    digest: str = "",
    size: int = 0,
    on_progress=None,
    cancel_check=None,
    timeout: int = 30,
) -> DownloadOutcome:
    """下载一个资产到 ``target``，校验 SHA-256，失败时**删掉文件**。

    ``target`` 旁边会留一个 ``.part``，支持断点续传（curl 的 ``-C -``），
    取消或失败时删掉——半截文件留着只会让下次误判成"已完成"。
    """
    if not url:
        return DownloadOutcome(ok=False, message="下载地址为空。")

    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)

    if size:
        free = free_space_bytes(target.parent)
        if free is not None and free < size * 105 // 100:
            return DownloadOutcome(
                ok=False,
                message=(
                    f"磁盘空间不足：需要约 {size / 1024 / 1024:.0f} MB，"
                    f"可用 {free / 1024 / 1024:.0f} MB"
                ),
            )

    args = ["-L", "--fail", "-o", str(part)]
    if part.is_file() and part.stat().st_size:
        args += ["-C", "-"]  # 续传
    args += ["--max-time", str(timeout), "--speed-time", "30", "--speed-limit", "1024", url]

    code, output = _curl(args, timeout=timeout + 60)
    if code == -2:
        _unlink(part)
        return DownloadOutcome(ok=False, message="本机没有 curl，无法下载更新。")
    if code == 28 or code == 0 and not part.is_file():
        # 28 = 超时；超时可能发生在半途，.part 保留以便续传
        if part.is_file() and part.stat().st_size:
            return DownloadOutcome(
                ok=False, cancelled=True, message="下载超时，已保留进度。"
            )
        _unlink(part)
        return DownloadOutcome(ok=False, message="下载超时，请重试。")
    if code != 0:
        _unlink(part)
        return DownloadOutcome(ok=False, message=f"下载失败（curl 退出码 {code}）。{output[:160]}".strip())

    if cancel_check and cancel_check():
        _unlink(part)
        return DownloadOutcome(ok=False, cancelled=True, message="已取消。")

    expected = digest.split(":", 1)[1].strip().lower() if ":" in digest else digest.strip().lower()
    if expected:
        actual = _sha256(part)
        if actual != expected:
            # 安全边界：校验不过，绝不把文件交给安装器
            _unlink(part)
            return DownloadOutcome(
                ok=False,
                message="下载的文件校验不通过，已删除，请重试。",
            )

    try:
        if target.is_file():
            target.unlink()
        part.replace(target)
    except OSError as exc:
        _unlink(part)
        return DownloadOutcome(ok=False, message=f"保存文件失败：{exc}")

    if on_progress:
        on_progress(1, 1, target.name, 1, 1)
    return DownloadOutcome(ok=True, path=target, message=f"已下载 {target.name}")


def _unlink(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 更新闭环：待确认标记 / 免安装版解压 / 入口文案
# ---------------------------------------------------------------------------

#: config.json 的 extra 里记「这次更新预计会装到哪个版本」。
#: 为什么需要：应用内更新是静默安装 + 自动重启，用户只看到窗口闪了一下。
#: 不告诉他结果，他没法判断是升级成功还是崩了。
PENDING_KEY = "update_pending"


def update_action_label(available: str | None = None) -> str:
    """「检查更新」入口的文案：知道有新版就把版本号写进去。

    为什么要落到菜单文字上：只弹一次对话框的话，用户点了「稍后」/关掉窗口之后
    就再也看不到「有新版」这件事——菜单是关掉弹窗后唯一还在的入口。
    """
    if available:
        return f"检查更新（有新版 {available}）"
    return "检查更新…"


def mark_update_pending(version: str) -> bool:
    """升级前记下预计装到的版本；写失败返回 False。

    写失败**不阻断更新**：这只是给下一次启动的一句提示，不该挡住升级本身。
    """
    if not version:
        return False
    try:
        from .app_config import AppConfig

        config = AppConfig.load()
        config.extra[PENDING_KEY] = str(version)
        config.save()
    except Exception:  # noqa: BLE001 - 配置读写问题不许影响升级
        return False
    return True


def take_update_pending(current_version: str) -> str | None:
    """消费「刚更新完」标记：只有当前版本与标记一致才返回它。

    两种情形都**清掉标记**：

    - 装成功：当前版本 == 标记 → 返回它，界面报一句「已更新到 X」；
    - 用户中途取消了安装：当前版本还是旧的 → 返回 None（否则每次启动都误报
      「已更新」，比不提示更糟）。

    配置读写失败一律当作「没有标记」。
    """
    try:
        from .app_config import AppConfig

        config = AppConfig.load()
        pending = str(config.extra.get(PENDING_KEY) or "")
        if not pending:
            return None
        config.extra.pop(PENDING_KEY, None)
        config.save()
    except Exception:  # noqa: BLE001
        return None
    if parse_version(pending) != parse_version(current_version):
        return None
    return pending


def extract_portable_zip(zip_path: Path, version: str) -> tuple[bool, Path | None, str]:
    """把免安装版 zip 解压到临时目录，返回 (是否成功, 解压根目录, 说明)。

    为什么不原地覆盖：① 正在运行的 exe 覆盖不了自己；② 目标目录里混着用户的
    ``models/``、``cache/``、``config.json``，自动替换等于拿用户数据冒险。
    这里只把新版解压到临时目录并把路径交出去，替换与否由用户决定。
    """
    import tempfile
    import zipfile

    if not zip_path.is_file():
        return False, None, "压缩包不存在。"

    root = Path(tempfile.mkdtemp(prefix=f"drama-splitter-{version}-"))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/")
                # 压缩包来自网络，不能信：绝对路径、盘符、../ 一律拒绝（zip-slip）
                if name.startswith("/") or ":" in name or ".." in name.split("/"):
                    continue
                archive.extract(member, root)
    except (OSError, zipfile.BadZipFile) as exc:
        return False, None, f"解压失败：{exc}"

    # 包里通常套一层 drama-splitter/，有就返回它，省得用户再点进去一次
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return True, entries[0], f"已解压到 {entries[0]}"
    return True, root, f"已解压到 {root}"


# ---------------------------------------------------------------------------
# 启动安装器
# ---------------------------------------------------------------------------


def launch_installer(path: Path) -> tuple[bool, str]:
    """启动安装程序。返回 (是否成功, 原因)。

    装的是免安装版时**不该**走这里——那种情况没有安装器可启动，界面要改成
    "打开下载目录"让用户自己换文件。

    装完由 installer.iss 的 [Run]（``RelaunchRequested``）把新版重新拉起：
    静默安装结束后程序自己回来，用户不必记得手动打开——否则他看到的只是
    "窗口突然关掉，然后什么都没有"。
    """
    if not path.is_file():
        return False, "安装包不存在。"
    # /RESTARTAPP=1 是给 installer.iss 的信号：装完请把新版拉起来。
    # 交互安装那条 [Run] 带 skipifsilent（静默安装不弹窗），靠这一条兜住
    # 应用内更新的场景。
    args = [
        str(path),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/RESTARTAPP=1",
    ]
    try:
        subprocess.Popen(args, creationflags=_NO_WINDOW)
    except OSError as exc:
        return False, f"无法启动安装程序：{exc}"
    return True, "安装程序已启动，它会关闭本程序并完成升级。"
