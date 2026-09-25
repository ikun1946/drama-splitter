"""滚动容器助手。

单页工作台里到处需要"内容保持自身最小尺寸、空间不够时出滚动条，而不是把
控件压扁"：左右两栏、对话框正文、预览的控制条。原先这个助手挂在
``app/gui/panes/_common.py`` 下，但 ``widgets`` 也要用它（媒体预览的控制条
必须能横向滚动，否则 10 个按钮的最小宽度会顶住整个中栏）——控件层不该反向
依赖分区层，于是搬到这里。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

__all__ = ["scrollable", "h_scrollable"]


def scrollable(widget: QWidget) -> QScrollArea:
    """把控件装进无边框的滚动容器（按需出横竖滚动条）。"""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area


def h_scrollable(widget: QWidget) -> QScrollArea:
    """只允许横向滚动的容器（用于一排固定尺寸的按钮）。

    与 :func:`scrollable` 的差别是高度恒等于内容高度——用在预览控制条这类
    "行内"场景，避免出现一条无意义的纵向滚动条。
    """
    area = scrollable(widget)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
    return area