"""滚动容器助手。

单页工作台里到处需要"内容保持自身最小尺寸、空间不够时出滚动条，而不是把
控件压扁"：左右两栏、对话框正文。原先这个助手挂在 ``app/gui/panes/_common.py``
下，但 ``widgets`` 也要用它——控件层不该反向依赖分区层，于是搬到这里。

只保留**纵向**滚动这一种形态：横向滚动会把"要读的东西"藏到视口外，本项目的
界面里没有一处需要它（预览控制条收成 5 键 + 步进菜单后也不再需要）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QScrollArea, QWidget

__all__ = ["scrollable"]


def scrollable(widget: QWidget) -> QScrollArea:
    """把控件装进**只纵向**滚动的无边框容器。

    横向滚动条必须关掉（原先是 ``ScrollBarAsNeeded``）：左栏内容——``QLineEdit`` 加
    按钮一行、审核态的表格——的最小宽度之和超过栏宽 320px，容器就会出横滚动条，
    **所有文字被裁**，用户得左右拖才能读完一句话。关掉横条 + 下面把最小宽度清零，
    内容才会换行而不是把容器撑宽。
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