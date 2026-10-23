# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本（阶段5）。

为什么用 onedir 而不是 onefile
------------------------------
- onefile 每次启动都要把整包解压到临时目录，PySide6 的体量下启动要十几秒；
- onedir 启动快、便于用户替换/排查（日志、模型目录都看得见）。
代价是产物是一个文件夹而非单个 exe——若要分发给终端用户，可在产物外再套一层
安装包（Inno Setup / NSIS），把整个 `dist/drama-splitter/` 装进用户可写目录。
实测产物约 124 MB（排除下列重型可选依赖之后）。

依赖：PyInstaller 属构建期依赖，不在 requirements.txt 里，打包前需先
    pip install pyinstaller

为什么固定 Qt 6.8
----------------
PySide6 6.12 改成以命名空间包方式延迟初始化 Qt 扩展模块。源码环境能正常
导入，但 PyInstaller 6.16/6.22 的冻结产物只带 ``QtWidgets.pyd`` 等扩展，
不带初始化所需的包代码，启动时统一报
``ImportError: DLL load failed while importing QtWidgets``。v1.0.29 起发布
构建使用已验证的 Qt 6.8.3；requirements.txt 同步把上限钉在 6.12 之前。

为什么要显式排除一批模块
------------------------
faster-whisper / scenedetect / llama_cpp 都是**在函数内部延迟导入**的，
PyInstaller 的静态分析照样会把它们整包拖进来（ctranslate2 + onnxruntime
加起来上百 MB，而且它们对本程序的界面与精确切割功能并非必需）。
排除后：转写与镜头检测在打包版里走"能力缺失即降级"的既有路径，
界面会明确提示，不会静默失败。

用法：
    python tools/build_package.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 应用图标。既写进 exe 的 PE 资源，也作为数据文件带进产物——两件事都得做：
#: PE 资源决定资源管理器 / 快捷方式看到的图标，数据文件是给运行时
#: ``QApplication.setWindowIcon`` 用的（详见 app/gui/branding.py 的说明）。
#: 缺失时跳过而不是报错：没有图标的产物仍可用，只是难看。
ICON = ROOT / "assets" / "icon.ico"

# 延迟导入的重型可选依赖：打包时排除，包体从数百 MB 降到几十 MB
EXCLUDES = [
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "scenedetect",
    "cv2",
    "llama_cpp",
    "transformers",
    "torch",
    "comtypes",
    "tkinter",
    "pytest",
    "matplotlib",
    "IPython",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
]

# ---------------------------------------------------------------------------
# 外挂依赖所需的**标准库**——必须显式带上
# ---------------------------------------------------------------------------
# 为什么需要这一组：上面 EXCLUDES 把 faster_whisper / llama_cpp / scenedetect
# 等排除了，PyInstaller 就**不会分析它们的 import**，于是这些包在运行时需要的
# **标准库**（纯 Python 部分）也没被打进产物。而它们是在应用内下载、运行时从
# 外部 ``deps/`` 加载的——那时才发现 ``import ctypes`` / ``import multiprocessing``
# 直接 ModuleNotFoundError。
#
# 实测：真实产物里 ``_ctypes.pyd`` / ``_multiprocessing.pyd`` 这些**扩展模块在**，
# 但 ``ctypes/__init__.py``、``multiprocessing/`` 这些**纯 Python 包装层**一个都
# 没有——所以 ``llama_cpp`` 一导入就 ``No module named 'ctypes'``（伪装成"推理栈
# 不可用"，界面显示降级运行）。
#
# 这一组是 llama_cpp + diskcache + faster_whisper + scenedetect 的 stdlib 并集，
# 逐个从它们的源码 import 语句里抠出来的。都是标准库，体积可忽略。
HIDDEN_STDLIB = [
    "ctypes", "ctypes.util", "ctypes.wintypes",   # llama_cpp 的核心机制
    "multiprocessing", "multiprocessing.spawn",    # llama_cpp 多进程推理
    "pickletools", "pickle", "dis",                # diskcache 序列化
    "sqlite3", "sqlite3.dbapi2",                   # diskcache 后端
    "csv", "bisect", "gc", "inspect", "struct",
    "zlib", "fnmatch", "uuid", "random", "string",
    "numbers", "fractions", "platform", "importlib.util",
    "queue", "weakref", "copyreg", "operator", "shutil",
    # numpy 2.x 的 `numpy.core` 向后兼容垫片 —— **不能漏**。
    # numpy 2 把 `core` 改名为 `_core`，只留 `numpy/core/` 做转发。它是纯 Python
    # 转发层，PyInstaller 静态分析**看不到**（产品自身代码不 import 它），于是
    # 产物里只有 `numpy/_core/` 而没有 `numpy/core/`。而 opencv(cv2) 这类老代码
    # 仍在 `import numpy.core.multiarray`，运行时直接
    # `ModuleNotFoundError: No module named 'numpy.core.multiarray'`
    # ——表现为"镜头检测导入失败"，但报的却是 numpy 找不到。
    "numpy.core", "numpy.core.multiarray", "numpy.core._multiarray_umath",
    "numpy.core.numeric", "numpy.core._dtype", "numpy.core._dtype_ctypes",
    "numpy.core.umath", "numpy.core.multiarray", "numpy.core.fromnumeric",
    "numpy.core.shape_base", "numpy.core.numerictypes", "numpy.core.function_base",
    # xml 一族：scenedetect / click 的间接依赖会用到（缺了报 xml.dom 找不到）。
    # 纯标准库，体积小，一次带全省得逐个试。
    "xml", "xml.dom", "xml.dom.minidom", "xml.etree", "xml.etree.ElementTree",
    "xml.parsers", "xml.parsers.expat", "xml.sax", "xml.sax.saxutils",
    "xmlrpc", "xmlrpc.client",
    # 其它外挂包常用到的标准库
    "concurrent", "concurrent.futures", "subprocess", "signal",
    "importlib.metadata", "pkgutil", "dataclasses", "enum", "typing",
    "logging", "logging.handlers", "threading", "tempfile", "errno",
    "io", "codecs", "unicodedata", "locale", "gettext",
]

#: setuptools 71+ 把 ``jaraco.*`` 等依赖放进 ``setuptools._vendor``，再由
#: ``pkg_resources`` 在运行时从那个目录导入。PyInstaller 的 setuptools 钩子
#: 会处理 ``setuptools._vendor`` 里的常规模块，但本机的 zhconv 仍会触发
#: ``pkg_resources``，而冻结启动脚本最先执行的就是它：只要 jaraco 没进包，
#: 程序连 ``--version`` 都会在 Qt 之前退出（报 ``No module named 'jaraco'``）。
#: 显式收集这一小簇模块比依赖构建机上的 setuptools / hooks 版本更稳。
HIDDEN_SETUPTOOLS_VENDOR = [
    "setuptools._vendor",
    "setuptools._vendor.jaraco",
    "setuptools._vendor.jaraco.collections",
    "setuptools._vendor.jaraco.context",
    "setuptools._vendor.jaraco.functools",
    "setuptools._vendor.jaraco.text",
    "setuptools._vendor.jaraco.text.layouts",
    "setuptools._vendor.packaging",
    "setuptools._vendor.platformdirs",
    "setuptools._vendor.platformdirs.api",
    "setuptools._vendor.platformdirs.version",
    "setuptools._vendor.platformdirs.windows",
]


def stable_abi_dll() -> Path | None:
    """定位当前解释器的 ``python3.dll``（稳定 ABI 转发库）；找不到返回 None。

    它就在解释器可执行文件旁边。用 ``sys.base_prefix`` 而不是
    ``sys.prefix``：venv 里通常**没有**这个 DLL（它属于基础安装），
    而是要往前找基础解释器——实测本机 venv 的 Scripts/ 下确实没有。
    """
    candidates = [
        Path(sys.executable).resolve().parent / "python3.dll",
        Path(sys.base_prefix) / "python3.dll",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# zhconv 的**词典数据**——必须显式收集，否则打包版一分析就炸
# ---------------------------------------------------------------------------
# 为什么要这一段：zhconv 是纯 Python 包，PyInstaller 的静态分析会把它的
# **代码**冻进产物，但**不会带上词典数据** ``zhcdict.json``——那份 500 KB 的
# JSON 是包自己用 ``os.path.dirname(__file__)`` 直接 ``open()`` 读的，不走
# importlib.resources，PyInstaller 看不出它算数据文件。
#
# 后果（v1.0.7 实测）：``import zhconv`` 一切正常，一 ``convert()`` 就
#   FileNotFoundError: [Errno 2] No such file or directory:
#       ...\_internal\zhconv\zhcdict.json
# 而 asr 那一步只把 ImportError 当“没装”（记一条风险继续跑），这个异常会直接
# 冒到界面——用户看到的是“分析失败”弹窗，整次分析白跑。
#
# 落点必须是 ``_internal/zhconv/``：zhconv 用包目录找词典，冻结后模块的
# ``__file__`` 就落在这里。文件名随版本变过（1.4 起叫 ``zhcdict.json``，
# 更早叫 ``zhdict.json``），所以按 ``*.json`` 全收，不写死名字。
def zhconv_data_args() -> list[str]:
    """收集 zhconv 的词典文件，返回 ``--add-data`` 参数；找不到包则返回空表。

    返回空表是“本机没装 zhconv”的信号，由 :func:`main` 决定是否致命——
    这里不自己抛异常，方便测试直接调用。
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("zhconv")
    except (ImportError, ValueError):
        return []
    if spec is None or not spec.submodule_search_locations:
        return []

    package_dir = Path(next(iter(spec.submodule_search_locations)))
    command: list[str] = []
    for path in sorted(package_dir.glob("*.json")):
        command += ["--add-data", f"{path}{os.pathsep}zhconv"]
    return command


def build_pythonpath() -> str:
    """返回打包时优先使用的模块搜索路径。

    发布机同时装了多个 PySide6 版本和独立 PyInstaller 目录时，``python -m
    PyInstaller`` 只会选中其中一套。这里允许通过环境变量显式指定，避免
    “源码里是 6.8、打包时却收进 6.12”这类不可见漂移。

    环境变量（可选）：
    - ``DRAMA_BUILD_PYTHONPATH``：额外的模块根目录，多个用 ``os.pathsep`` 连接；
    - ``DRAMA_BUILD_QT_ROOT``：PySide6 所在的模块根目录（通常是一份
      ``pip install --target`` 的输出目录）。
    """
    parts: list[str] = []
    override = os.environ.get("DRAMA_BUILD_PYTHONPATH", "")
    if override:
        parts.extend(path for path in override.split(os.pathsep) if path)
    qt_root = os.environ.get("DRAMA_BUILD_QT_ROOT", "")
    if qt_root:
        parts.append(qt_root)

    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        parts.extend(path for path in existing.split(os.pathsep) if path)
    return os.pathsep.join(dict.fromkeys(parts))


def packaging_env() -> dict[str, str]:
    """构造只影响本次 PyInstaller 子进程的环境变量。"""
    env = os.environ.copy()
    pythonpath = build_pythonpath()
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


def verify_qt_runtime() -> bool:
    """在打包前确认当前解释器实际选中的 PySide6 是冻结链兼容版本。"""
    probe = (
        "import PySide6; "
        "from PySide6.QtWidgets import QApplication; "
        "print(PySide6.__version__)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT),
        env=packaging_env(),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print("PySide6 运行库导入失败，拒绝继续打包：")
        print((proc.stderr or proc.stdout).strip())
        return False

    version = (proc.stdout or "").strip().splitlines()
    if not version:
        print("无法确认 PySide6 版本，拒绝继续打包。")
        return False
    try:
        major, minor = (int(part) for part in version[-1].split(".")[:2])
    except (ValueError, TypeError):
        print(f"无法解析 PySide6 版本：{version[-1]!r}")
        return False
    if (major, minor) >= (6, 12):
        print(
            f"当前 PySide6 {version[-1]} 会被 PyInstaller 打成无法启动的产物。\n"
            "请安装 6.8.x（requirements.txt 已限制 <6.12），或设置 "
            "DRAMA_BUILD_QT_ROOT 指向兼容版本目录。"
        )
        return False
    print(f"Qt 版本检查通过：PySide6 {version[-1]}")
    return True


def main() -> int:
    name = "drama-splitter"
    dist = ROOT / "dist"
    work = ROOT / "build" / "pyinstaller"
    spec = ROOT / "build" / f"{name}.spec"

    for path in (dist / name, work):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        name,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec.parent),
    ]
    if ICON.is_file():
        # Windows 专用：--icon 只在 win32 上被接受，别的平台加了会报错。
        if sys.platform == "win32":
            command += ["--icon", str(ICON)]
        # 目标写成 "assets"（无点号）——PyInstaller 的分隔符是平台相关的，
        # 冒号在 Windows 上才对；这里用相对 dest，落到 _internal/assets/。
        command += ["--add-data", f"{ICON}{os.pathsep}assets"]
    # `python3.dll` —— Python 的**稳定 ABI 转发库**，PyInstaller 不会自动带上。
    #
    # 为什么必须显式加：外挂的转写引擎依赖（tokenizers / av / faster_whisper）
    # 是编译时链接 ``python3.dll``（abi3 约定）的 .pyd，而不是版本专用的
    # ``python312.dll``。普通 Python 安装目录里有它，所以源码版能跑；冻结后
    # 只带 ``python312.dll``，这些 .pyd 就一律 ``DLL load failed while importing
    # _core: 找不到指定的模块``。实测把 python3.dll 放进产物后三个包全部导入成功。
    #
    # 它只有 ~70 KB，随包分发无负担；缺了它，应用内装好的引擎会静默不可用。
    stable_abi = stable_abi_dll()
    if stable_abi is not None:
        command += ["--add-data", f"{stable_abi}{os.pathsep}."]
    # zhconv 的词典数据（繁简转换）。漏了它，打包版“界面能开、一分析就炸”，
    # 而 --self-check / --ui-smoke 都发现不了（两者都不碰繁简转换）。
    # 这里当致命错误处理：与其出一个坏包，不如构建时就停下。
    zhconv_args = zhconv_data_args()
    if not zhconv_args:
        print("未找到 zhconv 的词典数据（本机没装 zhconv？）。")
        print("请先安装运行依赖：pip install -r requirements.txt")
        return 1
    command += zhconv_args
    # 代码也显式钉一次：asr 里是函数内延迟导入，静态分析理论上能看到，
    # 但这条链已经断过一次，显式带上不值钱。
    command += ["--hidden-import", "zhconv"]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    # 外挂依赖需要的标准库要显式带上（EXCLUDES 让 PyInstaller 分析不到它们）
    for module in HIDDEN_STDLIB:
        command += ["--hidden-import", module]
    for module in HIDDEN_SETUPTOOLS_VENDOR:
        command += ["--hidden-import", module]
    command.append(str(ROOT / "run.py"))

    print("执行打包：")
    print("  " + " ".join(command))
    print()
    if not verify_qt_runtime():
        return 1

    proc = subprocess.run(command, cwd=str(ROOT), env=packaging_env())
    if proc.returncode != 0:
        print()
        print("打包失败。")
        return proc.returncode

    exe = dist / name / f"{name}.exe"
    print()
    if not exe.exists():
        print(f"打包结束但未找到可执行文件：{exe}")
        return 1

    size_mb = sum(f.stat().st_size for f in (dist / name).rglob("*") if f.is_file()) / 1024 / 1024
    print(f"产物：{exe}")
    print(f"目录体积：{size_mb:.0f} MB")

    # 两项自检：运行前提（--self-check）与界面完整性（--ui-smoke）。
    # 后者是必须的：`--self-check` 根本不碰界面，漏打 Qt 插件或界面模块时
    # 它照样"通过"，而用户双击才崩。
    #
    # 注意：产物是 `--windowed` 编译的 GUI 子系统程序，标准输出没有宿主控制台，
    # 里面 `sys.stdout is None` → print() 是空操作。所以这里**拿不到自检文本**，
    # 只能依据返回码判定（返回码不受影响）。要看得见输出，把下面的
    # subprocess 换成 ["cmd", "/c", ...] 之类带控制台的包装，或改用源码运行自检。
    ok = True
    for flag, title in (("--self-check", "运行前提自检"), ("--ui-smoke", "界面完整性自检")):
        print()
        print(f"{title}（窗口版无控制台输出，仅看返回码）：")
        check = subprocess.run([str(exe), flag], timeout=300)
        print(f"  返回码：{check.returncode}")
        if check.returncode != 0:
            ok = False

    # 第三项检查：zhconv 的词典是否真的落进了产物。
    # 上面两项都碰不到繁简转换——v1.0.7 正是“两项目检全绿、用户一跑分析
    # 就报 FileNotFoundError”。直接查文件最省事，也最硬。
    print()
    dict_dir = dist / name / "_internal" / "zhconv"
    dict_files = sorted(dict_dir.glob("*.json")) if dict_dir.is_dir() else []
    if dict_files:
        print("zhconv 词典已进产物：" + "、".join(path.name for path in dict_files))
    else:
        print(f"产物里没有 zhconv 词典（{dict_dir}）——繁简转换会在分析时失败。")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
