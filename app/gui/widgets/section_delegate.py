"""下拉里的分组标题：画成弱化的一行，让人一眼看出它不是可选项。

为什么需要它
------------
「模型设置」的下拉把「本地模型」「云端模型」当分组标题塞在同一份清单里。
``fill()`` 已经用 ``setEnabled(False)`` 把它们禁掉了——**但那只影响视图层**：
实测对禁用项调用 ``setCurrentIndex`` 照样成功，于是下拉框会一直显示
「本地模型」，而配置里的真实选择一点没变。真正的拦截在
``_CapabilityCard._on_index_changed``；这个委托负责的是**另一件事**：
让用户在点之前就知道那两行不该点。

为什么不直接 import ``ModelChoice`` 判断类型
-------------------------------------------
``ModelChoice`` 定义在 ``panes/model_setup_form.py``，而 widgets 是 panes 的
下层。这里若反向 import 就成了循环依赖。改用「索引是否 enabled」判断——
``fill()`` 对不可选项做的正是 ``setEnabled(False)``，两者天然一一对应，
且这个委托换去别的下拉也能用。

只改"画什么"，不改数据：``item.text()`` 与 ``item.data(UserRole)`` 都不动，
按文案或 id 的查找、测试断言都不受影响。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..theme import current_mode, qcolor

__all__ = ["SectionHeaderDelegate"]


class SectionHeaderDelegate(QStyledItemDelegate):
    """把禁用的分组行画成弱化文字，并且不给悬停/选中高亮。"""

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        if not self._is_header(index):
            return
        # 弱化 + 稍小：一眼看出这是"标题"而不是"一条可选的模型"
        option.palette.setColor(
            QPalette.ColorRole.Text,
            QColor(qcolor(current_mode(), "TEXT_MUTED")),
        )
        font = QFont(option.font)
        font.setBold(True)
        option.font = font
        # 去掉高亮矩形，否则灰字压在蓝色选中块上还是像可点
        option.state &= ~(
            QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Selected
        )

    def _is_header(self, index) -> bool:
        return not bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        self.initStyleOption(option, index)
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is None:
            super().paint(painter, option, index)
            return
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, widget)
