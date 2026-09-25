"""模型目录服务：清单、状态检查、下载与安装（供界面调用）。

为什么这个模块在 `app/core/` 而不是 `tools/`
--------------------------------------------
用户要求"在打包的桌面应用里选择并下载模型"。`tools/` 下的脚本**不随包分发**
（打包时排除了整个目录），因此下载逻辑必须落在核心层，界面才能调得动。
`tools/fetch_whisper_model.py` 与 `tools/fetch_llm.py` 保留为命令行入口，
内部改为调用本模块——**一份实现，两个入口**，避免两处逻辑漂移。

从既往实测继承的三条硬约束
--------------------------
1. **必须用 curl，不能用 urllib**：hf-mirror 会 403 拒绝 `Python-urllib` 的
   User-Agent（实测），而 curl 正常。
2. **huggingface.co 在本机不可达**（HTTP 000 超时），默认走 hf-mirror.com。
3. **不能用 huggingface_hub 的缓存**：它依赖符号链接，本机账户没有该权限，
   会产出 0 字节占位文件。所以这里直接下载真实文件到普通目录。

下载行为
--------
- 逐文件下载，先写 `.part` 再改名（中断不留半个文件冒充完整文件）；
- 支持断点续传（curl `-C -`）；
- 完成后按文件大小校验，不足即判失败；
- 全程可取消，取消时清理临时文件；
- 进度按已下载字节回报，总大小由 HEAD 探测；探测不到时 total=0，
  界面按"未知总大小"显示而不是编一个数字。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

__all__ = [
    "ModelFile",
    "ModelSpec",
    "InstallState",
    "DownloadResult",
    "MODEL_CATALOG",
    "whisper_spec",
    "llm_spec",
    "spec_by_key",
    "installed_state",
    "download_model",
    "remote_size",
    "list_remote_gguf",
    "validate_models_root",
    "DEFAULT_ENDPOINT",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DEFAULT_ENDPOINT = "https://hf-mirror.com"

# 逐文件的最小字节数：权重文件必须是"真的大文件"，否则说明下载被截断
_MIN_BYTES = {"model.bin": 1024 * 1024}


@dataclass(frozen=True)
class ModelFile:
    """模型仓库里的一个文件。

    体积下限只对**权重文件**有意义：权重被截断会让模型静默出错，必须拦住；
    而 config.json / tokenizer.json 这类配置文件的正常体积可能只有几十字节
    （实测一个合法的 `{}` 就是 2 字节），用统一的字节下限会把它们误判成
    "不完整"。因此默认下限只要求"非空"，权重在 `_WHISPER_FILES` 等处以
    1MB / 64MB 的显式下限单独把关。
    """

    name: str
    min_bytes: int = 1


@dataclass(frozen=True)
class ModelSpec:
    """一个可下载的模型。"""

    key: str                 # 唯一标识，形如 "whisper:small" / "llm:2B:Q4_K_M"
    kind: str                # whisper / llm
    label: str               # 界面显示名
    repo: str
    subdir: str              # 相对模型根目录的安装位置
    files: tuple[ModelFile, ...]
    approx_mb: int | None = None   # 已知的实测体积；未知则 None（不编数字）
    note: str = ""

    @property
    def big_file(self) -> str:
        return "model.bin" if self.kind == "whisper" else self.files[-1].name

    def target_dir(self, models_root: str | Path) -> Path:
        return Path(models_root) / self.subdir

    def urls(self, endpoint: str = DEFAULT_ENDPOINT) -> dict[str, str]:
        base = f"{endpoint.rstrip('/')}/{self.repo}/resolve/main"
        return {item.name: f"{base}/{item.name}" for item in self.files}

    def describe(self) -> str:
        size = f"{self.approx_mb} MB" if self.approx_mb else "体积未知"
        return f"{self.label}（{size}）"


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

WHISPER_TIERS: tuple[tuple[str, int, str], ...] = (
    ("tiny", 75, "最快，中文准确率低（CER 约 6.6%），仅适合试跑"),
    ("base", 145, "较快，中文准确率一般"),
    ("small", 484, "推荐：中文 CER 3.3%，CPU 上约 3 倍速"),
    ("medium", 1500, "更准但明显更慢，适合有耐心时使用"),
    ("large-v3", 3090, "最高准确率，CPU 上通常不实用"),
)

LLM_SIZES: tuple[tuple[str, str], ...] = (
    ("2B", "轻量，CPU 可用"),
    ("4B", "更准，CPU 上偏慢"),
    ("9B", "最准，建议有独显"),
)

_WHISPER_FILES = (
    ModelFile("config.json"),
    ModelFile("model.bin", _MIN_BYTES["model.bin"]),
    ModelFile("tokenizer.json"),
    ModelFile("vocabulary.txt"),
)


def whisper_spec(tier: str) -> ModelSpec:
    size = dict((t, s) for t, s, _ in WHISPER_TIERS).get(tier)
    note = dict((t, n) for t, _, n in WHISPER_TIERS).get(tier, "")
    return ModelSpec(
        key=f"whisper:{tier}",
        kind="whisper",
        label=f"语音转写 whisper-{tier}",
        repo=f"Systran/faster-whisper-{tier}",
        subdir=f"faster-whisper-{tier}",
        files=_WHISPER_FILES,
        approx_mb=size,
        note=note,
    )


def llm_spec(size: str, quant: str) -> ModelSpec:
    repo = f"unsloth/Qwen3.5-{size}-GGUF"
    filename = f"Qwen3.5-{size}-{quant}.gguf"
    note = dict(LLM_SIZES).get(size, "")
    return ModelSpec(
        key=f"llm:{size}:{quant}",
        kind="llm",
        label=f"语义判断 Qwen3.5-{size} {quant}",
        repo=repo,
        subdir=f"llm/Qwen3.5-{size}-GGUF",
        files=(ModelFile(filename, 64 * 1024 * 1024),),
        approx_mb=1222 if (size == "2B" and quant == "Q4_K_M") else None,
        note=note,
    )


# 默认保证 available_models() 立刻可用的最小集合；界面会另外从远端发现更多量化
MODEL_CATALOG: tuple[ModelSpec, ...] = tuple(
    whisper_spec(tier) for tier, _, _ in WHISPER_TIERS
) + (llm_spec("2B", "Q4_K_M"),)


def spec_by_key(key: str) -> ModelSpec | None:
    for spec in MODEL_CATALOG:
        if spec.key == key:
            return spec
    if key.startswith("whisper:"):
        tier = key.split(":", 1)[1]
        if tier in {t for t, _, _ in WHISPER_TIERS}:
            return whisper_spec(tier)
    if key.startswith("llm:"):
        parts = key.split(":")
        if len(parts) == 3:
            return llm_spec(parts[1], parts[2])
    return None


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


@dataclass
class InstallState:
    """某个模型在本地是否可用。"""

    spec: ModelSpec
    directory: Path
    present_bytes: int = 0
    missing: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing and not self.truncated

    @property
    def partial(self) -> bool:
        return bool(self.present_bytes) and not self.ready

    def describe(self) -> str:
        if self.ready:
            return f"已安装（{self.present_bytes / 1024 / 1024:.0f} MB）"
        if self.partial:
            return f"不完整（缺 {len(self.missing + self.truncated)} 个文件，已占 {self.present_bytes / 1024 / 1024:.0f} MB）"
        return "未安装"


def installed_state(spec: ModelSpec, models_root: str | Path) -> InstallState:
    directory = spec.target_dir(models_root)
    state = InstallState(spec=spec, directory=directory)
    for item in spec.files:
        path = directory / item.name
        if not path.exists():
            state.missing.append(item.name)
            continue
        size = path.stat().st_size
        state.present_bytes += size
        if size < item.min_bytes:
            state.truncated.append(item.name)
    return state


def validate_models_root(models_root: str | Path) -> tuple[bool, str]:
    """检查模型目录是否可写。返回 (可用, 说明)。"""
    directory = Path(models_root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建目录：{exc}"
    probe = directory / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"目录不可写：{exc}"
    return True, str(directory)


def free_space_bytes(models_root: str | Path) -> int | None:
    directory = Path(models_root)
    while not directory.exists() and directory.parent != directory:
        directory = directory.parent
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 远端探测
# ---------------------------------------------------------------------------


def _curl(args: list[str], *, timeout: int = 60) -> tuple[int, str]:
    """调用 curl。始终带 --noproxy 例外，避免系统代理拦掉回环地址（测试要用）。"""
    proc = subprocess.run(
        ["curl", "-sS", "--noproxy", "localhost,127.0.0.1", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip() or (proc.stderr or "").strip()


def remote_size(url: str) -> int | None:
    """取远端文件字节数；取不到返回 None（界面据此显示"未知"）。

    必须解析 `Content-Length` 头，**不能用 `%{size_download}`**：HEAD 请求
    按定义不下载响应体，`size_download` 恒为 0，返回的永远是 None——
    这是个很隐蔽的错（进度百分比与磁盘预检会静默失效，表现为"总大小未知"）。
    跟随重定向时会收到多个响应头，取**最后一个** content-length（最终资源的）。

    部分镜像不支持 HEAD 时，退回 Range 探测（`Content-Range` 的 `总长` 段）。
    """
    code, output = _curl(["-sIL", "-D", "-", "-o", "/dev/null", url], timeout=30)
    if code == 0:
        size = None
        for line in (output or "").splitlines():
            if line.lower().startswith("content-length:"):
                tail = line.split(":", 1)[1].strip()
                if tail.isdigit():
                    size = int(tail)
        if size:
            return size

    code, output = _curl(
        ["-sL", "-r", "0-0", "-D", "-", "-o", "/dev/null", url], timeout=30
    )
    if code != 0:
        return None
    for line in (output or "").splitlines():
        if line.lower().startswith("content-range:"):
            tail = line.split("/")[-1].strip()
            if tail.isdigit():
                return int(tail)
    return None


def list_remote_gguf(repo: str, endpoint: str = DEFAULT_ENDPOINT) -> list[str]:
    """列出仓库内的 GGUF 文件（供界面发现可选的量化档位）。"""
    code, output = _curl(["-sL", "--fail", "-m", "30", f"{endpoint}/api/models/{repo}"], timeout=60)
    if code != 0:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    return sorted(
        item["rfilename"]
        for item in data.get("siblings", [])
        if str(item.get("rfilename", "")).endswith(".gguf")
    )


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


@dataclass
class DownloadResult:
    spec: ModelSpec
    ok: bool
    downloaded_bytes: int = 0
    cancelled: bool = False
    message: str = ""
    installed_dir: Path | None = None

    def describe(self) -> str:
        if self.ok:
            return (
                f"{self.spec.label} 安装完成（"
                f"{self.downloaded_bytes / 1024 / 1024:.0f} MB）→ {self.installed_dir}"
            )
        if self.cancelled:
            return f"{self.spec.label} 下载已取消（已清理临时文件）"
        return f"{self.spec.label} 下载失败：{self.message}"


ProgressCallback = Callable[[int, int, str, int, int], None]


def download_model(
    spec: ModelSpec,
    models_root: str | Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    on_progress: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    verify_remote: bool = True,
) -> DownloadResult:
    """下载并安装一个模型。

    返回值永远不抛异常——界面需要的是"结果 + 可读原因"，不是崩溃。
    """
    directory = spec.target_dir(models_root)
    ok, detail = validate_models_root(models_root)
    if not ok:
        return DownloadResult(spec=spec, ok=False, message=detail)

    urls = spec.urls(endpoint)

    # 远端体积（用于进度百分比；取不到就按 0 处理，界面显示"未知总大小"）
    totals: dict[str, int] = {}
    if verify_remote:
        for name, url in urls.items():
            size = remote_size(url)
            if size:
                totals[name] = size

    needed = sum(totals.values()) if totals else 0
    if needed:
        free = free_space_bytes(models_root)
        if free is not None and free < needed * 105 // 100:
            return DownloadResult(
                spec=spec,
                ok=False,
                message=(
                    f"磁盘空间不足：需要约 {needed / 1024 / 1024:.0f} MB，"
                    f"可用 {free / 1024 / 1024:.0f} MB"
                ),
            )

    downloaded = 0
    file_total = len(spec.files)

    for index, item in enumerate(spec.files, start=1):
        url = urls[item.name]
        target = directory / item.name
        part = target.with_name(target.name + ".part")

        if target.exists() and target.stat().st_size >= item.min_bytes:
            downloaded += target.stat().st_size
            if on_progress:
                on_progress(downloaded, needed, item.name, index, file_total)
            continue

        directory.mkdir(parents=True, exist_ok=True)
        completed = _download_one(
            url, target, part, spec=spec, index=index, file_total=file_total,
            item=item, totals=totals, base_downloaded=downloaded,
            on_progress=on_progress, cancel_check=cancel_check,
        )
        if completed is None:
            _cleanup_part(part)
            return DownloadResult(
                spec=spec, ok=False, cancelled=True,
                downloaded_bytes=downloaded, message="已取消",
            )
        ok, size_or_message = completed
        if not ok:
            _cleanup_part(part)
            return DownloadResult(
                spec=spec, ok=False, downloaded_bytes=downloaded, message=size_or_message
            )
        downloaded += size_or_message

    state = installed_state(spec, models_root)
    if not state.ready:
        problems = state.missing + state.truncated
        return DownloadResult(
            spec=spec,
            ok=False,
            downloaded_bytes=downloaded,
            message=f"下载完成但校验未通过，缺少或不完整：{', '.join(problems)}",
        )
    return DownloadResult(
        spec=spec, ok=True, downloaded_bytes=downloaded, installed_dir=directory
    )


def _download_one(
    url: str,
    target: Path,
    part: Path,
    *,
    spec: ModelSpec,
    index: int,
    file_total: int,
    item: ModelFile,
    totals: dict[str, int],
    base_downloaded: int,
    on_progress: ProgressCallback | None,
    cancel_check: Callable[[], bool] | None,
):
    """下载单个文件。返回 None 表示被取消，(True, 字节数) 成功，(False, 原因) 失败。"""
    command = [
        "curl", "-sS", "-L", "--fail",
        "--noproxy", "localhost,127.0.0.1",
        "-C", "-",
        "-o", str(part),
        url,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return False, f"无法启动下载：{exc}"

    expected_file = totals.get(item.name, 0)
    last_report = 0.0
    try:
        while True:
            if cancel_check and cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                return None
            code = process.poll()
            current = part.stat().st_size if part.exists() else 0
            now = time.monotonic()
            if on_progress and (now - last_report > 0.2 or code is not None):
                total = sum(totals.get(f.name, 0) for f in spec.files) or 0
                on_progress(base_downloaded + current, total, item.name, index, file_total)
                last_report = now
            if code is not None:
                break
            time.sleep(0.1)

        if code != 0:
            detail = (process.stderr.read() or b"").decode("utf-8", "replace").strip()
            return False, f"{item.name} 下载失败（curl 退出码 {code}）：{detail[:200]}"
    finally:
        if process.poll() is None:
            process.kill()

    size = part.stat().st_size if part.exists() else 0
    if size < item.min_bytes:
        return False, (
            f"{item.name} 只得到 {size} 字节，低于下限 {item.min_bytes}（疑似被截断）"
        )
    if expected_file and size < expected_file:
        return False, (
            f"{item.name} 得到 {size} 字节，与远端声明 {expected_file} 不符（下载不完整）"
        )
    try:
        part.replace(target)
    except OSError as exc:
        return False, f"{item.name} 落盘失败：{exc}"
    return True, size


def _cleanup_part(part: Path) -> None:
    try:
        if part.exists():
            part.unlink()
    except OSError:
        pass


def delete_model(spec: ModelSpec, models_root: str | Path) -> tuple[bool, str]:
    """删除已安装的模型（只删该模型自己的目录，不碰其他模型）。"""
    directory = spec.target_dir(models_root)
    if not directory.exists():
        return False, "该模型未安装"
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        return False, f"删除失败：{exc}"
    return True, f"已删除 {directory}"


def download_many(
    specs: list[ModelSpec],
    models_root: str | Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    on_progress: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[DownloadResult]:
    """顺序下载多个模型（串行，避免占满带宽与磁盘）。"""
    results: list[DownloadResult] = []
    for spec in specs:
        result = download_model(
            spec,
            models_root,
            endpoint=endpoint,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )
        results.append(result)
        if result.cancelled:
            break
    return results
