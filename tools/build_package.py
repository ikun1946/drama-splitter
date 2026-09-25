# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本（阶段5）。

为什么用 onedir 而不是 onefile
------------------------------
- onefile 每次启动都要把整包解压到临时目录，PySide6 的体量下启动要十几秒；
- onedir 启动快、便于用户替换/排查（日志、模型目录都看得见）。
代价是产物是一个文件夹而非单个 exe——用 `tools/make_installer.ps1` 可再封成安装包。

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

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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

    # 无界面自检：这是打包版能否真正工作的判据
    print()
    print("运行产物自检：")
    check = subprocess.run(
        [str(exe), "--self-check"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    print(check.stdout or "")
    if check.stderr:
        print("stderr:", check.stderr[:500])
    return 0 if check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
