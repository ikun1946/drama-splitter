"""省略号委托：条目放不下时画省略号，悬停给全文。

为什么需要它
------------
``QListWidget`` 不做省略号：条目文字超出可视宽度就直接被裁掉。而「模型管理 →
云端厂商」的左列表只有 230px，最短的条目「阿里云百炼（通义千问）（内置）」单行
就要 499px。拉宽列表会把右侧编辑区挤没（那才是真正要填表的地方），于是只剩两条路：
裁掉，或者省略。

这个委托**只改"画什么"**，不改数据：``item.text()`` 仍是完整文案，
``item.data(UserRole)`` 仍是厂商 id，所有按文案或 id 的查找、测试断言都不受影响。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

__all__ = ["ElidingItemDelegate"]


class ElidingItemDelegate(QStyledItemDelegate):
    """放不下时从**右侧**截断，并把全文放进悬停提示。

    为什么用 ``ElideRight`` 而不是 ``ElideMiddle``
    ------------------------------------------
    条目形如「阿里云百炼（通义千问）（内置）」：厂商名是**身份**，用户靠它认出
    这一行是谁；「（内置）」只是标记（能不能删）。中间省略会两边各留一半，读出来是
    「阿里云百炼（…问）（内…」这种两头半截的碎片。右省略保住名字的开头，完整文案
    （含标记）由 tooltip 补上。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        metrics = option.fontMetrics
        available = option.rect.width() - 4
        if metrics.horizontalAdvance(text) > available:
            option.text = metrics.elidedText(
                text, Qt.TextElideMode.ElideRight, max(available, 24)
            )
        # 省略掉的全文（含「（内置）」这类标记）必须还能拿到
        option.toolTip = text

    def paint(self, painter, option, index) -> None:  # noqa: N802
        self.initStyleOption(option, index)
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is None:
            super().paint(painter, option, index)
            return
        # 借用当前控件的样式绘制（选中态、悬停态都由它决定）
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, widget)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        size = super().sizeHint(option, index)
        # 宽度只作为**下限提示**：不给的话 QListWidget 的 minimumSizeHint 会按最长
        # 条目算，把这一栏顶到 499px。截到 260 就够表达"这栏大概这么宽"。
        return QSize(min(size.width(), 260), size.height())
