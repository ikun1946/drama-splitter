"""运行时图标。

为什么 exe 有图标还不够
------------------------
**PE 资源图标 ≠ 运行时窗口图标。** ``tools/build_package.py`` 给 PyInstaller 传了
``--icon``，那只负责 exe 文件本身的资源（资源管理器、开始菜单、任务栏快捷方式
都看它）。Qt 的主窗口是**另一个窗口**，它的图标要由 ``QApplication.setWindowIcon``
显式给；PyInstaller 的窗口版引导程序不会替我们做这件事。

漏掉这一步的症状很有迷惑性：exe 文件的属性里能看到自定义图标，但双击运行后
任务栏和标题栏仍是默认图标——很容易被当成"``--icon`` 没生效"而查错方向。

图标在产物里的位置
------------------
- 源码运行：``<项目根>/assets/icon.ico``
- 打包运行：``sys._MEIPASS/assets/icon.ico``（PyInstaller 把 ``--add-data``
  放进 ``_internal/``，而 ``_MEIPASS`` 就指向它）

找不到图标不是错误：直接返回 ``None``，界面照常用，只是没有图标。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

__all__ = ["icon_file", "apply_app_icon"]


def icon_file() -> Path | None:
    """定位 ``assets/icon.ico``；找不到返回 ``None``（不抛）。"""
    candidates = []
    if getattr(sys, "frozen", False):
        # 打包版：_MEIPASS 指向 _internal，--add-data 落在它下面
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass) / "assets" / "icon.ico")
    candidates.append(ROOT / "assets" / "icon.ico")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def apply_app_icon(app) -> bool:
    """把应用图标设到 ``QApplication`` 上。成功返回 ``True``。

    由 ``run.py`` 在创建主窗口**之前**调用——图标必须在第一个窗口出现前就位，
    否则任务栏会先显示默认图标再跳变。
    """
    path = icon_file()
    if path is None:
        return False
    try:
        from PySide6.QtGui import QIcon

        icon = QIcon(str(path))
        if icon.isNull():
            return False
        app.setWindowIcon(icon)
    except Exception:  # noqa: BLE001 - 图标是装饰，失败不该挡住启动
        return False
    return True
