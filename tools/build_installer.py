# -*- coding: utf-8 -*-
"""编译 Windows 安装程序（Inno Setup 6）。

为什么需要这个包装脚本，而不是直接跑 ISCC
----------------------------------------
ISCC.exe 的安装位置不固定：本机 winget 装到了
``%LOCALAPPDATA%\\Programs\\Inno Setup 6\\``，而官网安装包通常落在
``C:\\Program Files (x86)\\Inno Setup 6\\``。写死任何一条路径，别人克隆仓库后
第一件事就会失败。这里按顺序探测，探测不到再给出明确的安装指引。

前提：
    1) 已装 Inno Setup 6：``winget install JRSoftware.InnoSetup``
    2) 已跑过 ``python tools/build_package.py`` 生成 ``dist/drama-splitter/``

用法：
    python tools/build_installer.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve().parent / "installer.iss"
PACKAGE_DIR = ROOT / "dist" / "drama-splitter"


def find_iscc() -> Path | None:
    """按优先级找 ISCC.exe：环境变量 → 常见安装位置 → PATH。"""
    candidates = []

    override = os.environ.get("ISCC")
    if override:
        candidates.append(Path(override))

    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        root = Path(base)
        # winget 的 per-user 安装落在 LOCALAPPDATA\Programs\<产品名>\
        candidates.append(root / "Inno Setup 6" / "ISCC.exe")
        candidates.append(root / "Programs" / "Inno Setup 6" / "ISCC.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    from shutil import which

    found = which("ISCC") or which("iscc")
    return Path(found) if found else None


def main() -> int:
    if not PACKAGE_DIR.is_dir():
        print("未找到打包产物 dist/drama-splitter/。")
        print("请先运行：python tools/build_package.py")
        return 1

    iscc = find_iscc()
    if iscc is None:
        print("未找到 Inno Setup 编译器（ISCC.exe）。")
        print("请先安装：winget install JRSoftware.InnoSetup")
        print("已装但不在常见位置的话，可设环境变量 ISCC 指向 ISCC.exe 的完整路径。")
        return 1

    print(f"使用编译器：{iscc}")
    proc = subprocess.run([str(iscc), str(SCRIPT)], cwd=str(ROOT))
    if proc.returncode != 0:
        print()
        print("编译失败。")
        return proc.returncode

    setup = ROOT / "dist" / "drama-splitter-1.0.0-setup.exe"
    print()
    if not setup.is_file():
        print(f"编译结束但未找到产物：{setup}")
        return 1
    print(f"产物：{setup}")
    print(f"大小：{setup.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
