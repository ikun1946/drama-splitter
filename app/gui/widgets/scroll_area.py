"""滚动容器助手。

单页工作台里到处需要"内容保持自身最小尺寸、空间不够时出滚动条，而不是把
控件压扁"：左右两栏、对话框正文、预览的控制条。原先这个助手挂在
``app/gui/panes/_common.py`` 下，但 ``widgets`` 也要用它（媒体预览的控制条
必须能横向滚动，否则 10 个按钮的最小宽度会顶住整个中栏）——控件层不该反向
依赖分区层，于是搬到这里。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QScrollArea, QWidget

__all__ = ["scrollable", "h_scrollable"]


def scrollable(widget: QWidget) -> QScrollArea:
    """把控件装进**只纵向**滚动的无边框容器。

    横向滚动条必须关掉（原先是 ``ScrollBarAsNeeded``）：左栏内容——``QLineEdit`` 加
    按钮一行、审核态的表格——的最小宽度之和超过栏宽 320px，容器就会出横滚动条，
    **所有文字被裁**，用户得左右拖才能读完一句话。关掉横条 + 下面把最小宽度清零，
    内容才会换行而不是把容器撑宽。

    真需要横向滚动的地方（预览控制条那一排按钮）用 :func:`h_scrollable`。
    """
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    # 内容自身的最小宽度会顶住容器宽度，导致"关掉横条"变成"直接裁掉"而不是"换行"。
    # 清零后 QLabel 的 setWordWrap 才有机会生效。
    widget.setMinimumWidth(0)
    for child in widget.findChildren(QWidget):
        # 列表/表格的横向滚动条已由各自的策略控制；把它们的视口最小宽度也清零，
        # 内容能换行，但条目本身仍会按列宽省略，不会把整页顶宽。
        if not isinstance(child, QAbstractScrollArea):
            child.setMinimumWidth(0)
    return area


def h_scrollable(widget: QWidget) -> QScrollArea:
    """只允许横向滚动的容器（用于一排固定尺寸的按钮）。

    与 :func:`scrollable` 的差别是高度恒等于内容高度——用在预览控制条这类
    "行内"场景，避免出现一条无意义的纵向滚动条。横向条在这里**必须重新打开**：
    ``scrollable`` 已经为了防裁剪关掉了它。
    """
    area = scrollable(widget)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
    return area
