"""分区/表单层共享符号。

供 ``app/gui/panes/`` 下各分区与 ``app/gui/dialogs/`` 下各对话框共用。
只放真正跨文件的东西，避免这里变成新的杂物间。

内容
----
- ``LEVEL_*`` 兼容性等级到中文/颜色的映射
- ``register_worker`` 把后台任务登记到宿主 MainWindow（沿 parent 链上溯）
- ``scrollable`` 滚动容器（实现在 ``widgets/scroll_area.py``，这里转出）
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ...core.probe import LEVEL_BLOCK, LEVEL_INFO, LEVEL_WARN
from ..widgets.scroll_area import scrollable

__all__ = [
    "LEVEL_TEXT",
    "LEVEL_COLOR",
    "register_worker",
    "scrollable",
]

LEVEL_TEXT = {LEVEL_BLOCK: "阻断", LEVEL_WARN: "警告", LEVEL_INFO: "提示"}
LEVEL_COLOR = {LEVEL_BLOCK: "#b3261e", LEVEL_WARN: "#9a6700", LEVEL_INFO: "#3d6b35"}


def register_worker(owner: QWidget, name: str, worker) -> None:
    """把后台任务登记到宿主 MainWindow。

    先试 ``owner.window()``：页面直接挂在主窗口下时它就是要找的宿主。
    再沿 ``parentWidget()`` 上溯：对话框自己就是一个顶层窗口，``window()``
    返回的是对话框本身，必须往上找到它的父窗口（主窗口）才能登记。
    找不到宿主就静默跳过——单测直接构造控件时不该因此报错。
    """
    candidate = owner.window()
    while candidate is not None:
        register = getattr(candidate, "register_worker", None)
        if callable(register):
            register(name, worker)
            return
        candidate = candidate.parentWidget()
