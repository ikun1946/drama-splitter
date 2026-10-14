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
import time
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
    "asset_candidates",
    "download_curl_args",
    "download_status_text",
    "extract_portable_zip",
    "format_duration",
    "format_size",
    "launch_installer",
    "mirror_url",
    "release_headline",
    "mark_update_pending",
    "take_update_pending",
    "update_action_label",
]

ROOT = Path(__file__).resolve().parent.parent.parent

#: 公开仓库的 releases/latest **不需要鉴权**，且自动排除 draft 与 prerelease
#: （版本比较因此不必处理预发布分支）。未鉴权配额是 60 次/小时/IP。
RELEASE_API = "https://api.github.com/repos/ikun1946/drama-splitter/releases/latest"
RELEASES_PAGE = "https://github.com/ikun1946/drama-splitter/releases"

#: 直连都失败时依次尝试的公共加速源（**只影响传输**：装之前仍按发布页的
#: SHA-256 逐字节校验，所以镜像塞不进被篡改的包）。
#:
#: 实测（同一条 35.8 MB 安装包）：
#:   gh-proxy.com  5.3 MB/s（8 秒下完）
#:   ghproxy.net   62 KB/s
#:   ghfast.top    连不上
#: 只有直连候选全部失败才会用到它们；配置里把 ``update_mirror`` 设成
#: "off" 可以彻底关掉。
BUILTIN_MIRRORS: tuple[str, ...] = (
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
)

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
    #: Release API 给的资产地址（``api.github.com/.../releases/assets/<id>``）。
    #: 实测 ``github.com`` 在部分网络下连不上（用户机 21 秒超时、本机仅
    #: ~48KB/s），而 API 主机与它 302 过去的 CDN 通常还通——所以下载优先走它。
    setup_api_url: str = ""
    zip_api_url: str = ""

    def asset_for(self, installed: bool) -> tuple[list[str], int, str] | None:
        """按运行形态挑资产，返回**候选地址列表**（API 地址优先、官方地址兜底）。

        为什么给一串地址而不是一个：官方下载地址在 ``github.com``，它在部分
        网络下不可达（用户实测：应用里连 21 秒都不通；本机也只有 ~48KB/s）。
        Release API 的 ``assets[].url`` 走 ``api.github.com`` → 302 → CDN，
        同一份文件通常快得多也更容易连上。多给几个候选，失败就换下一个。
        """
        if installed:
            urls = [u for u in (self.setup_api_url, self.setup_url) if u]
            return (urls, self.setup_size, self.setup_digest) if urls else None
        urls = [u for u in (self.zip_api_url, self.zip_url) if u]
        return (urls, self.zip_size, self.zip_digest) if urls else None


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
    #: 中断/取消时已落盘的部分大小（字节）。界面据此把按钮改成「继续下载」——
    #: 慢链路上一次下不完是常态，白扔掉已经下到的部分最伤用户。
    partial_bytes: int = 0


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


def _asset_fields(assets: list) -> tuple[tuple[str, str, int, str], tuple[str, str, int, str]]:
    """挑出安装包与免安装包各自的 (官方地址, API 资产地址, 大小, 摘要)。"""
    setup = ("", "", 0, "")
    portable = ("", "", 0, "")
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        api_url = str(item.get("url") or "")
        if not url and not api_url:
            continue
        size = int(item.get("size") or 0)
        digest = str(item.get("digest") or "")
        if name.endswith(SETUP_SUFFIX):
            setup = (url, api_url, size, digest)
        elif name.endswith(ZIP_SUFFIX):
            portable = (url, api_url, size, digest)
    return setup, portable


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
    (s_url, s_api, s_size, s_digest), (z_url, z_api, z_size, z_digest) = _asset_fields(assets)
    if not (s_url or s_api) and not (z_url or z_api):
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
        setup_api_url=s_api,
        zip_api_url=z_api,
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


def mirror_url(url: str, mirror: str | None) -> str:
    """把资产地址套上镜像前缀；镜像为空就原样返回。

    约定与常见 GitHub 加速服务一致：``https://mirror.example/`` 直接拼在原始
    完整 URL 前面；也支持 ``{url}`` 占位（``https://m.example/?u={url}``）。

    镜像**只影响传输**：下载完成后仍然按发布页给出的 SHA-256 校验，所以镜像
    即使被投毒也过不了校验这一关——这也是敢把它做成可配置的前提。
    """
    prefix = (mirror or "").strip()
    if not prefix or not url:
        return url
    if "{url}" in prefix:
        return prefix.replace("{url}", url)
    return prefix.rstrip("/") + "/" + url


def format_size(num_bytes: float) -> str:
    """人类可读的字节数：``1.2 MB`` / ``812 KB`` / ``95 B``。"""
    value = float(max(0.0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: float) -> str:
    """``45 秒`` / ``3 分 20 秒`` / ``1 小时 5 分``。"""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def download_status_text(done: int, total: int, elapsed: float) -> tuple[str, bool]:
    """下载中的状态文案与「是否确定进度」。

    纯函数，便于直接测：速度与剩余时间都由"已下字节 / 已用时间"估，不猜远端。
    ``total <= 0``（发布页没给大小时）只报已下载量与速度，进度条转不确定模式。
    """
    done = max(0, int(done))
    total = max(0, int(total))
    determinate = total > 0
    if determinate:
        parts = [f"已下载 {format_size(done)} / {format_size(total)}"]
    else:
        parts = [f"已下载 {format_size(done)}"]
    speed = done / elapsed if elapsed > 0.5 and done else 0.0
    if speed > 0:
        parts.append(f"{format_size(speed)}/s")
        if determinate and total > done:
            parts.append(f"剩余约 {format_duration((total - done) / speed)}")
    return " · ".join(parts), determinate


def asset_candidates(direct: list[str], mirror: str = "") -> list[str]:
    """把直连地址排成候选序列：用户镜像 → 直连 → 内置加速源。

    规则：
    - ``mirror`` 非空：用户指定的镜像优先，然后才是直连（不再用内置源）；
    - ``mirror`` 为 ``"off"`` / ``"none"``：只用直连，彻底不用任何镜像；
    - 默认（空）：直连优先，**全部失败才**回退到内置加速源。

    为什么内置源只做兜底：它们是第三方服务，能不用就不用；但用户那边
    github.com 完全连不上时，没有兜底就等于永远更新不了（实测镜像 5.3 MB/s，
    直连只有 48 KB/s 甚至超时）。装之前仍按 SHA-256 校验，镜像无法投毒。
    """
    direct = [url for url in direct if url]
    if not direct:
        return []
    key = (mirror or "").strip().lower()
    if key in ("off", "none", "-"):
        return list(direct)
    # 只有「官方下载地址（github.com）」需要套镜像；API 资产地址本身就可达，
    # 绕第三方反而多一跳（它只负责 302 到 CDN）。
    github_urls = [url for url in direct if _is_release_host(url)]
    others = [url for url in direct if not _is_release_host(url)]
    if mirror and mirror.strip():
        return [mirror_url(url, mirror) for url in github_urls] + others + github_urls
    extra = [mirror_url(url, prefix) for prefix in BUILTIN_MIRRORS for url in github_urls]
    return others + github_urls + extra


def release_headline(notes: str) -> str:
    """从发布说明里取出「这一版改了什么」的一句话标题。

    更新窗口不该把整篇 Markdown 糊上去：用户第一眼要知道的是这版改了什么，
    所以取说明里第一个标题（``## xxx`` → ``xxx``）；没有标题就取第一行非空文本。
    """
    for line in (notes or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            text = text.lstrip("#").strip()
        if text:
            return text
    return ""


def download_curl_args(url: str, part: Path, *, stall_seconds: int = 30) -> list[str]:
    """构造下载用的 curl 参数（抽出来便于测试断言）。

    **故意不设 ``--max-time``**：它是整个传输的总时限。国内直连 GitHub 实测
    只有几十 KB/s，35.8 MB 的安装包要十几分钟——30 秒总时限的结果是每到 30 秒
    就被掐断、再点一次又只下 30 秒，用户看到的就是"又慢又永远下不完"。
    真正要防的是"卡住不动"，那由 ``--speed-limit/--speed-time`` 负责。
    """
    args = [
        "curl", "-sS", "-L", "--fail",
        "--noproxy", "localhost,127.0.0.1",
        # 走 Release API 的资产地址时必须带这个头，否则拿到的是 JSON 元数据；
        # 对 github.com 的官方下载地址无副作用。
        "-H", "Accept: application/octet-stream",
        "-o", str(part),
    ]
    if part.is_file() and part.stat().st_size:
        args += ["-C", "-"]  # 断点续传
    args += [
        "--speed-limit", "1024",   # 低于 1 KB/s 持续 stall_seconds 秒 → 判定卡死
        "--speed-time", str(stall_seconds),
        url,
    ]
    return args


def _host_of(url: str) -> str:
    """取 URL 的主机名，把失败原因说清楚（例如 ``github.com：curl 28``）。"""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url[:40]
    except Exception:  # noqa: BLE001
        return url[:40]


def _is_release_host(url: str) -> bool:
    """是不是「官方下载主机」（github.com）。

    注意不能用 ``"github.com" in url`` 判断：``api.github.com`` 也含这个子串，
    而它本身可达、不该被套加速源，也不该被算成「连不上官方主机」。
    """
    return _host_of(url) in ("github.com", "www.github.com")


def _download_failure_hint(urls: list[str]) -> str:
    """全部候选都失败时给一句可操作的话。"""
    if any(_is_release_host(u) for u in urls):
        return (
            "（github.com 在部分网络下连不上：可点「发布页」手动下载，"
            "或按 README 在 config.json 里配 update_mirror 走镜像）"
        )
    return ""


def _download_to_part(
    url: str,
    part: Path,
    *,
    size: int,
    on_progress,
    cancel_check,
    stall_seconds: int,
) -> tuple[str, str, int]:
    """用一个地址把数据下到 ``.part``（不校验、不改名）。

    返回 ``(状态, 说明, 已落盘字节)``，状态是 ``ok`` / ``cancelled`` / ``failed``。
    单独拆出来，是为了让 :func:`download_asset` 能在多个候选地址之间依次重试。
    """
    args = download_curl_args(url, part, stall_seconds=stall_seconds)
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return "failed", "本机没有 curl", 0
    except OSError as exc:
        return "failed", f"无法启动下载：{exc}", 0

    def got() -> int:
        try:
            return part.stat().st_size if part.is_file() else 0
        except OSError:
            return 0

    last_report = 0.0
    try:
        while True:
            if cancel_check and cancel_check():
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                except Exception:  # noqa: BLE001 - 收尾尽力而为
                    pass
                return "cancelled", "已取消", got()
            code = process.poll()
            current = got()
            now = time.monotonic()
            if on_progress and (now - last_report > 0.2 or code is not None):
                on_progress(current, size, part.name, 1, 1)
                last_report = now
            if code is not None:
                break
            time.sleep(0.1)
        detail = (process.stderr.read() or b"").decode("utf-8", "replace").strip()
    finally:
        if process.poll() is None:
            process.kill()

    if code != 0:
        # 28 = 连不上，或触发 --speed-limit 的"卡死"判定；其余非零码多半是 HTTP 错误
        return "failed", f"curl {code}：{detail[:160] or '连接失败'}", got()
    return "ok", "", got()


def download_asset(
    urls: str | list[str],
    target: Path,
    *,
    digest: str = "",
    size: int = 0,
    on_progress=None,
    cancel_check=None,
    on_source=None,
    mirror: str = "",
    stall_seconds: int = 30,
) -> DownloadOutcome:
    """下载资产到 ``target``；``urls`` 是候选地址列表（也接受单个字符串）。

    为什么要多候选：官方下载地址在 ``github.com``，实测在部分网络下**完全连不上**
    （用户机连 21 秒都不通；本机也只有 ~48KB/s），而 Release API 给的资产地址走
    ``api.github.com`` → 302 → CDN，通常快一个数量级。候选之间共用同一个
    ``.part``，换地址时已下载的部分不浪费（curl ``-C -`` 续传）。

    契约不变：过程持续回报进度、取消/中断保留断点、最后按 SHA-256 校验。
    """
    if isinstance(urls, str):
        direct = [urls] if urls else []
    else:
        direct = [u for u in (urls or []) if u]
    candidates = asset_candidates(direct, mirror)
    if not candidates:
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

    failures: list[str] = []
    for url in candidates:
        if on_source is not None:
            try:
                on_source(_host_of(url))
            except Exception:  # noqa: BLE001 - 展示用回调，失败不该影响下载
                pass
        status, detail, partial = _download_to_part(
            url,
            part,
            size=size,
            on_progress=on_progress,
            cancel_check=cancel_check,
            stall_seconds=stall_seconds,
        )
        if status == "ok":
            break
        if status == "cancelled":
            return DownloadOutcome(
                ok=False,
                cancelled=True,
                message=f"已取消，已保留 {format_size(partial)}（再点一次可继续）。",
                partial_bytes=partial,
            )
        failures.append(f"{_host_of(url)}：{detail}")
    else:
        partial = part.stat().st_size if part.is_file() else 0
        joined = "；".join(failures[:3])
        if partial:
            return DownloadOutcome(
                ok=False,
                cancelled=True,
                message=(
                    f"下载中断，已保留 {format_size(partial)}，可点「继续下载」重试（{joined}）"
                ),
                partial_bytes=partial,
            )
        return DownloadOutcome(
            ok=False,
            message=f"下载失败：{joined}{_download_failure_hint(candidates)}",
        )

    expected = (
        digest.split(":", 1)[1].strip().lower() if ":" in digest else digest.strip().lower()
    )
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

    final = target.stat().st_size if target.is_file() else 0
    if on_progress:
        on_progress(final, final, target.name, 1, 1)
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
