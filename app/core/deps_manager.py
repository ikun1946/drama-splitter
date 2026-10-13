"""运行期依赖安装：让打包版也能装上语音转写引擎（faster-whisper）。

为什么需要这个模块
------------------
打包版是个 PyInstaller 冻结的 exe，**里面没有 pip**，而 faster-whisper 及其
依赖（ctranslate2 / av / onnxruntime / tokenizers …）因为体积（实测约 227 MB）
被有意排除在包外。结果是：用户下载好了 whisper 模型，却因为没有转写引擎而
根本跑不了转写——界面只能给一句"改用源码版"，对多数人等于没救。

本模块把这条路补上：**在应用内下载官方预编译 wheel、解压到一个独立目录、
挂上 sys.path**，不改动打包产物、不需要 pip、不需要编译器。

关键设计：外挂目录必须**追加**到 sys.path 末尾
--------------------------------------------
实测（本机 PyInstaller onedir 产物）把依赖解压进 `_internal/` 会直接崩：
打包产物自带 numpy 2.5.4，而 faster-whisper 的依赖链指向 numpy 2.0.1，两个
版本的 `numpy/` 目录混在一起后，导入 numpy 抛
``AttributeError: 'numpy.ufunc' object has no attribute '__module__'``。

因此这里的做法是：依赖装进**独立目录**，通过 :func:`register_deps` 把它
**append 到 sys.path 末尾**。这样打包产物里已有的包（numpy 等）仍优先命中，
外挂目录只补产物里没有的那些（ctranslate2 / av / faster_whisper …）。
numpy 2.5.4 满足 faster-whisper 的 ``numpy>=2``，故无需降级，只需别覆盖。

为什么用固定清单而不是通用依赖解析
--------------------------------
真做依赖解析（版本回溯、冲突求解）是 pip 的活，代价与风险都不成比例。转写
引擎的依赖集是**已知且收敛**的：钉死一组实测可用的版本，缺什么补什么即可。
清单里的版本以 :data:`DEPS_MANIFEST` 为准；若将来 faster-whisper 升级，改这
一处即可（`tests/test_deps_manager.py` 会盯住清单与选择逻辑）。

与 ``model_manager`` 的分工
--------------------------
`model_manager` 管**模型**（whisper/GGUF 权重文件，从 hf-mirror 下）；本模块
管**代码依赖**（wheel，从 PyPI 镜像下）。二者都在应用内下载，但来源、格式、
落点、生效方式都不同，故各自成模块，只共用 ``_curl`` 这一层。
"""

from __future__ import annotations

import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .model_manager import _curl  # 复用 curl 封装（含 --noproxy 与超时降级）

__all__ = [
    "DEPS_MANIFEST",
    "DepsFile",
    "DepsInstallResult",
    "default_deps_root",
    "deps_installed",
    "deps_present",
    "install_deps",
    "register_deps",
    "resolve_wheel_url",
    "uninstall_deps",
]

#: 默认 PyPI 索引（simple 页）。国内把 pypi.org 换成清华镜像实测快数十倍，
#: 与 requirements.txt 里的建议一致。
DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

#: 转写引擎的完整依赖清单：(包名, 钉死版本)。
#:
#: 这一组是**实测可协同工作**的版本：由 pip 在 cp312/win_amd64 上解析
#: ``faster-whisper`` 得到，再经"解压到独立目录 + 真导入一遍"验证通过
#: （见 tests/test_deps_manager.py 与本次实现的验证记录）。顺序无关，但要装齐。
#:
#: **不能简单钉 faster-whisper 1.0.3 那一组**：那组把 onnxruntime 钉在 1.18.1，
#: 而它针对 NumPy 1.x 编译，装进带 NumPy 2.5.4 的环境会
#: ``ImportError: A module that was compiled using NumPy 1.x cannot be run in
#: NumPy 2.5.4``。实测 1.18.1 与当前环境不兼容，必须用 1.31.0。
#: 新版这一组同时把总体积从 ~227 MB 降到 ~82 MB（ctranslate2 4.8.2 仅 18 MB）。
#:
#: **``av`` 必须钉在 16.x**——这一条是实跑才发现的，pip 解析不出来：
#: - ``av`` 19.0.x 删掉了 ``metadata_errors`` 形参，而 faster-whisper 1.2.1 的
#:   ``decode_audio`` 仍在传它，于是真跑转写时抛
#:   ``TypeError: open() got an unexpected keyword argument 'metadata_errors'``；
#: - ``av`` 14.x 及更早在这套 Python 3.12 环境里导入即崩
#:   （``KeyError: '__reduce_cython__'``，Cython 生成代码与解释器不匹配）；
#: - **15.0 / 16.0.1 实测可用**：能导入，且接受 ``metadata_errors``。
#: 钉 16.0.1 是取"足够新又不越界"的一版。仅看 ``av>=11`` 会解析出 19.x 而踩雷。
DEPS_MANIFEST: tuple[tuple[str, str], ...] = (
    ("faster-whisper", "1.2.1"),
    ("ctranslate2", "4.8.2"),
    ("tokenizers", "0.23.3"),
    ("onnxruntime", "1.31.0"),
    ("av", "16.0.1"),
    ("huggingface-hub", "2.2.0"),
    ("hf-xet", "1.7.0"),
    ("httpx2", "2.13.1"),
    ("httpcore2", "2.13.1"),
    ("anyio", "4.15.1"),
    ("h11", "0.16.0"),
    ("click", "8.5.0"),
    ("truststore", "0.10.5"),
    ("numpy", "2.5.4"),
    ("pyyaml", "6.0.3"),
    ("tqdm", "4.70.1"),
    ("colorama", "0.4.6"),
    ("typing-extensions", "4.16.0"),
    ("filelock", "4.1.1"),
    ("fsspec", "2026.9.0"),
    ("packaging", "26.3"),
    ("flatbuffers", "25.12.19"),
    ("protobuf", "7.36.2"),
    ("idna", "3.20"),
)

#: 装好后**必须**能导入的包——用来判断"引擎真的可用了"。
#: 只查 faster_whisper 不够：它的顶层 import 会拉起 ctranslate2/av/tokenizers，
#: 少任何一个都会在真跑转写时才炸。这里把关键链条一次列全。
REQUIRED_IMPORTS: tuple[str, ...] = (
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "av",
    "huggingface_hub",
)

#: 外挂依赖目录在 sys.path 上的标记（测试与排错用）
_DEPS_MARKER = ".drama-splitter-deps"


def default_deps_root() -> Path:
    """默认外挂依赖目录：与 exe / 项目根同级的 ``deps/``。

    与模型目录（``models/``）同样的落点约定——用户换个盘符或放进共享目录
    时，靠配置指定；不配置就用这里。
    """
    from .app_config import app_dir

    return app_dir() / "deps"


# ---------------------------------------------------------------------------
# 版本与 wheel 选择
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """PEP 503 归一化：``faster-whisper`` / ``faster_whisper`` 等价。"""
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")


def _running_python_tags() -> set[str]:
    """当前解释器能接受的纯 Python 标签集合（用于挑 wheel）。

    打包后解释器就是冻进 exe 的那个（实测 Python 3.12），所以这里必须按
    **运行时**版本挑 wheel，不能写死 cp312——源码版可能是 3.13。
    """
    major, minor = sys.version_info[:2]
    return {f"cp{major}{minor}", "cp3", f"py{major}", f"py{major}{minor}", "py3"}


def _wheel_compatible(filename: str) -> bool:
    """文件名形如 ``pkg-1.2.3-pyTag-abiTag-platformTag.whl``，判断能否用。

    规则（够用即可，不做完整 PEP 425 实现）：
    - 平台：``any`` 或含 ``win_amd64``；
    - Python 标签：与运行时标签有交集，或是 ``abi3``（前向兼容 cp3x）；
    - ``abi3`` 的 wheel（如 protobuf 的 cp310-abi3）在 cp312 上可用。
    """
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]
    if plat_tag != "any" and "win_amd64" not in plat_tag:
        return False
    py_tokens = set(py_tag.split("."))
    if py_tokens & _running_python_tags():
        return True
    # abi3：cp3x-abi3 在 cp3y(y>=x) 上兼容
    if abi_tag == "abi3" and any(t.startswith("cp3") for t in py_tokens):
        return True
    return False


def _version_prefix_matches(file_version: str, wanted: str) -> bool:
    """文件名里的版本是否匹配清单版本。

    用 ``startswith`` 而非全等，是为了容下 ``6.0.2rc1`` 这种上游把预发布版
    推到 simple 索引的情况（实测清华镜像上 PyYAML/colorama 就是如此）——
    它们与目标版本同源，可用。
    """
    return file_version == wanted or file_version.startswith(wanted)


def resolve_wheel_url(
    package: str, version: str, *, index: str = DEFAULT_INDEX
) -> tuple[str, str] | None:
    """在 simple 索引里查该包该版本在**本机**可用的 wheel，返回 (文件名, URL)。

    取不到返回 None（网络不可达、版本不在索引里、或没有匹配平台的轮子）。
    优先 ``win_amd64``、次选 ``any``；同优先级里偏好带 ``cp3x`` 的具体轮子。
    """
    import re
    import urllib.parse

    base = index.rstrip("/") + f"/{package}/"
    code, output = _curl(["-sSL", "--fail", "-m", "60", base], timeout=90)
    if code != 0 or not output:
        return None

    # simple 页里 wheel 表现为 <a href="...whl#sha256=...">；相对/绝对链接都要认
    hrefs = re.findall(r'href="([^"]+\.whl)(?:#[^"]*)?"', output)
    norm = _normalize(package)
    candidates: list[tuple[int, str, str]] = []
    for href in hrefs:
        filename = href.rsplit("/", 1)[-1]
        if not filename.endswith(".whl"):
            continue
        parts = filename[:-4].split("-")
        if len(parts) < 5 or _normalize(parts[0]) != norm:
            continue
        if not _version_prefix_matches(parts[1], version):
            continue
        if not _wheel_compatible(filename):
            continue
        url = urllib.parse.urljoin(base, href)
        # 打分：win_amd64 优先、具体 cp3x 轮子次之
        score = 0
        if "win_amd64" in parts[-1]:
            score += 2
        if "cp3" in parts[-3]:
            score += 1
        if parts[-1] == "any":
            score -= 1
        candidates.append((score, filename, url))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1], candidates[0][2]


# ---------------------------------------------------------------------------
# 安装状态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepsFile:
    """一个待装的 wheel。"""

    package: str
    version: str
    filename: str
    url: str


@dataclass
class DepsInstallResult:
    """一次"装引擎"的结果。契约与 model_manager.DownloadResult 一致：不抛异常。"""

    ok: bool
    installed: int = 0            # 成功装上的 wheel 数
    total: int = 0               # 计划装的 wheel 数
    downloaded_bytes: int = 0
    cancelled: bool = False
    message: str = ""
    deps_root: Path | None = None

    def describe(self) -> str:
        if self.ok:
            return (
                f"语音转写引擎安装完成（{self.installed} 个包，"
                f"{self.downloaded_bytes / 1024 / 1024:.0f} MB）→ {self.deps_root}"
            )
        if self.cancelled:
            return "语音转写引擎安装已取消"
        return f"语音转写引擎安装失败：{self.message}"


def deps_present(deps_root: str | Path | None = None) -> bool:
    """依赖目录里是否**看起来**装齐了（查关键包目录存在，不导入）。

    只做文件级判断——供环境自检这类不能有副作用、也不该真加载重型模块的地方
    使用。真正的可用性判定见 :func:`deps_installed`（会真的 import）。
    """
    root = Path(deps_root) if deps_root is not None else default_deps_root()
    probe = {
        "faster_whisper": "faster_whisper",
        "ctranslate2": "ctranslate2",
        "tokenizers": "tokenizers",
        "onnxruntime": "onnxruntime",
        "av": "av",
    }
    for package_dir in probe.values():
        if not (root / package_dir).is_dir():
            return False
    return True


def deps_installed(deps_root: str | Path | None = None) -> bool:
    """:func:`deps_present` 之外，**真的把关键包导一遍**，确认真能用。

    导入失败（DLL 缺失、ABI 不匹配等）都要如实返回 False——"文件在"不等于
    "能用"，这正是本功能此前最容易骗人的地方。
    """
    root = Path(deps_root) if deps_root is not None else default_deps_root()
    if not deps_present(root):
        return False
    register_deps(root)
    for name in REQUIRED_IMPORTS:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001 - 任何导入失败都算"没装好"
            return False
    return True


# ---------------------------------------------------------------------------
# sys.path 挂载
# ---------------------------------------------------------------------------


def register_deps(deps_root: str | Path | None = None) -> bool:
    """把依赖目录**追加**到 sys.path 末尾，并把其中的 DLL 目录全部登记好；
    返回是否有目录被挂上。

    必须是 append 而不是 insert(0)：打包产物自带 numpy 2.5.4，依赖链里也有
    numpy。若把外挂目录插到最前，外挂的 numpy 会覆盖产物里的，实测直接崩
    （``'numpy.ufunc' object has no attribute '__module__'``）。追加到末尾
    则产物内的包优先命中，外挂目录只补缺的那些。

    **DLL 目录必须显式登记，且不止一层**——这是实测踩出来的坑：CPython 3.8+
    起不再从 sys.path 搜 DLL，而各包把 DLL 放在不同位置：

    - ``ctranslate2/*.dll``（包**内部**）
    - ``onnxruntime/capi/*.dll``（包内部子目录）
    - ``av.libs/*.dll``、``numpy.libs/*.dll``（顶层 ``*.libs``）

    早先只登记了顶层与 ``*.libs``，于是 ``import ctranslate2`` 抛
    ``DLL load failed while importing _core``。这里改为递归找出**所有含 DLL
    的目录**并逐一登记，不再依赖对包布局的假设。

    幂等：重复调用不会重复插入 sys.path；DLL 句柄缓存在模块级，避免
    ``os.add_dll_directory`` 返回的句柄被 GC 回收后立刻失效（这是该 API 的
    著名陷阱）。
    """
    root = Path(deps_root) if deps_root is not None else default_deps_root()
    if not root.is_dir():
        return False
    text = str(root)
    if text not in sys.path:
        sys.path.append(text)
    _register_dll_dirs(root)
    return True


#: 已登记的 DLL 目录句柄。**必须持有引用**：`os.add_dll_directory` 返回的
#: 句柄一旦被 GC，对应目录会立即从搜索路径移除（CPython 的既有行为）。这里
#: 存成模块级列表，进程存活期间不释放。
_DLL_HANDLES: list[object] = []


def _register_dll_dirs(root: Path) -> None:
    """递归找出 root 下所有直接含 ``.dll`` 的目录，逐一登记为 DLL 搜索目录。"""
    if sys.platform != "win32":
        return
    try:
        import os
    except Exception:  # noqa: BLE001
        return
    for directory in _dll_dirs(root):
        try:
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        except (AttributeError, OSError, FileNotFoundError):
            continue


def _dll_dirs(root: Path) -> list[Path]:
    """root 下所有直接包含 ``.dll`` 的目录（含 root 自身）；按深度排序。

    深度排序让外层先登记（子目录 DLL 往往依赖同包的外层 DLL，先登记外层
    更稳）。扫描深度有限（见下），不担心大目录树。
    """
    found: list[tuple[int, str, Path]] = []
    try:
        for path in root.rglob("*.dll"):
            directory = path.parent
            depth = len(directory.relative_to(root).parts)
            found.append((depth, str(directory).lower(), directory))
    except OSError:
        return []
    # 去重后按 (深度, 路径) 排序
    seen: set[str] = set()
    ordered: list[Path] = []
    for _depth, key, directory in sorted(found):
        if key in seen:
            continue
        seen.add(key)
        ordered.append(directory)
    return ordered


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------


DepsProgressCallback = Callable[[int, int, str, int, int], None]
#: (已下载字节, 总字节, 当前文件, 第几个, 共几个)


def _plan(endpoint: str) -> tuple[list[DepsFile], list[str]]:
    """解析清单，列出要下的 wheel。返回 (待装列表, 解析失败的包名)。

    解析失败的包不直接当致命错误抛——统一在 :func:`install_deps` 里报告，
    便于一次性把"哪些包在镜像上找不到"讲清楚。
    """
    plan: list[DepsFile] = []
    failed: list[str] = []
    for package, version in DEPS_MANIFEST:
        resolved = resolve_wheel_url(package, version, index=endpoint)
        if resolved is None:
            failed.append(package)
            continue
        filename, url = resolved
        plan.append(DepsFile(package, version, filename, url))
    return plan, failed


def install_deps(
    deps_root: str | Path | None = None,
    *,
    endpoint: str = DEFAULT_INDEX,
    on_progress: DepsProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> DepsInstallResult:
    """下载并解压转写引擎的全部依赖。**永不抛异常**，失败以结果返回。

    幂等且可续装：已解压到位的 wheel（同名目录/文件存在）会跳过，所以取消后
    再点一次不会重下已经下好的部分。下载走 ``.part`` 临时文件，中断不留半成品。
    """
    root = Path(deps_root) if deps_root is not None else default_deps_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DepsInstallResult(ok=False, message=f"依赖目录不可写：{exc}")

    plan, failed = _plan(endpoint)
    if failed and not plan:
        return DepsInstallResult(
            ok=False,
            message=(
                f"无法从镜像取到这些包：{'、'.join(failed)}。"
                f"请检查网络，或在设置里换一个镜像源。"
            ),
        )

    cache = root / ".wheels"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DepsInstallResult(ok=False, message=f"无法创建缓存目录：{exc}")

    installed = 0
    downloaded = 0
    total = len(plan)

    for index, item in enumerate(plan, start=1):
        if cancel_check is not None and cancel_check():
            return DepsInstallResult(
                ok=False, cancelled=True, installed=installed, total=total,
                downloaded_bytes=downloaded, deps_root=root, message="已取消",
            )
        if on_progress is not None:
            on_progress(downloaded, 0, item.filename, index, total)

        wheel_path = cache / item.filename
        if not wheel_path.is_file() or wheel_path.stat().st_size < 1024:
            part = wheel_path.with_name(wheel_path.name + ".part")
            code, detail = _download(item.url, part, timeout=1800)
            if code != 0:
                _quiet_unlink(part)
                return DepsInstallResult(
                    ok=False, installed=installed, total=total,
                    downloaded_bytes=downloaded, deps_root=root,
                    message=f"{item.package} 下载失败（curl {code}）：{detail[:200]}",
                )
            part.replace(wheel_path)

        size = wheel_path.stat().st_size
        try:
            _extract_wheel(wheel_path, root)
        except (zipfile.BadZipFile, OSError) as exc:
            return DepsInstallResult(
                ok=False, installed=installed, total=total,
                downloaded_bytes=downloaded, deps_root=root,
                message=f"{item.package} 解压失败：{exc}",
            )
        downloaded += size
        installed += 1
        if on_progress is not None:
            on_progress(downloaded, 0, item.filename, index, total)

    if failed:
        return DepsInstallResult(
            ok=False, installed=installed, total=total,
            downloaded_bytes=downloaded, deps_root=root,
            message=f"以下包在镜像上没找到，安装不完整：{'、'.join(failed)}",
        )

    # 装完立刻挂载并真导一遍——"文件在"不等于"能用"，这里必须验到底
    register_deps(root)
    if not deps_installed(root):
        return DepsInstallResult(
            ok=False, installed=installed, total=total,
            downloaded_bytes=downloaded, deps_root=root,
            message="依赖已下载，但引擎仍无法导入（可能是 DLL 缺失或版本不匹配）。",
        )

    return DepsInstallResult(
        ok=True, installed=installed, total=total,
        downloaded_bytes=downloaded, deps_root=root,
    )


def _download(url: str, target: Path, *, timeout: int) -> tuple[int, str]:
    """下载单个文件到 target（支持断点续传：curl ``-C -``）。"""
    code, output = _curl(
        ["-L", "--fail", "-C", "-", "-o", str(target), url], timeout=timeout
    )
    return code, output


def _extract_wheel(wheel_path: Path, destination: Path) -> None:
    """把 wheel（就是个 zip）解压到依赖目录。

    不做 ``.data/scripts`` 的 entry_points 处理——本功能只装**库**，不需要
    命令行入口脚本（装了也没人调用）。
    """
    with zipfile.ZipFile(wheel_path) as archive:
        archive.extractall(destination)


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def uninstall_deps(deps_root: str | Path | None = None) -> tuple[bool, str]:
    """删除外挂依赖目录（"卸掉引擎"）。返回 (成功, 说明)。"""
    import shutil

    root = Path(deps_root) if deps_root is not None else default_deps_root()
    if not root.exists():
        return True, "依赖目录本就不存在。"
    try:
        shutil.rmtree(root)
    except OSError as exc:
        return False, f"删除失败：{exc}"
    text = str(root)
    while text in sys.path:
        sys.path.remove(text)
    return True, f"已删除 {root}"
