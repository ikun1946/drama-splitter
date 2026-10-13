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
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "run.py"))

    print("执行打包：")
    print("  " + " ".join(command))
    print()
    proc = subprocess.run(command, cwd=str(ROOT))
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
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
