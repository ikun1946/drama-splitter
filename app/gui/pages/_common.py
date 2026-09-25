"""页面层共享符号。

从 ``main_window.py`` 抽出来的，供 ``app/gui/pages/`` 下各页类共用。
只放真正跨页的东西，避免这里变成新的杂物间。

内容
----
- ``LEVEL_*`` 兼容性等级到中文/颜色的映射（导入页与多处日志共用）
- ``register_worker`` 把后台任务登记到外层 MainWindow
- ``scrollable`` 把宽控件装进无边框滚动容器（抽屉正文用）
- ``section_label`` / ``hint_label`` 两种高频小标签的构造快捷方式
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ...core.probe import LEVEL_BLOCK, LEVEL_INFO, LEVEL_WARN

__all__ = [
    "LEVEL_TEXT",
    "LEVEL_COLOR",
    "register_worker",
    "scrollable",
    "section_label",
    "hint_label",
    "FormSection",
]

LEVEL_TEXT = {LEVEL_BLOCK: "阻断", LEVEL_WARN: "警告", LEVEL_INFO: "提示"}
LEVEL_COLOR = {LEVEL_BLOCK: "#b3261e", LEVEL_WARN: "#9a6700", LEVEL_INFO: "#3d6b35"}


def register_worker(owner: QWidget, name: str, worker) -> None:
    """把后台任务登记到所属 MainWindow。

    页面类与外层窗口是松耦合的（页面不该知道 MainWindow 的类型细节），
    这里通过 ``window()`` 上溯，拿到就登记、拿不到就静默跳过——
    单测直接构造页面、不挂窗口时不该因此报错。
    """
    top = owner.window()
    register = getattr(top, "register_worker", None)
    if callable(register):
        register(name, worker)


def scrollable(widget: QWidget) -> QScrollArea:
    """把 widget 装进一个无边框的横向滚动容器。

    抽屉宽度有限（720px），双栏页面塞进去会被压成单列。套一层滚动容器后
    页面保持自身最小宽度，放不下时横向滚动，而不是把文字挤成竖排。
    """
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area


def section_label(text: str) -> QLabel:
    """小节标题（role=title 的 QLabel）。"""
    label = QLabel(text)
    label.setProperty("role", "title")
    return label


def hint_label(text: str = "") -> QLabel:
    """次要说明文字（role=subtitle 的 QLabel，自动换行）。"""
    label = QLabel(text)
    label.setProperty("role", "subtitle")
    label.setWordWrap(True)
    return label


class FormSection(QWidget):
    """带标题的一段表单区。页面里反复出现的「标题 + 若干行」结构。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.title_label = section_label(title)
        outer.addWidget(self.title_label)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        outer.addLayout(self.body)

    def add(self, widget: QWidget) -> None:
        self.body.addWidget(widget)
