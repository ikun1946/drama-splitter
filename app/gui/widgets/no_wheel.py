"""滚轮不改值的输入控件。

Qt 的 ``QAbstractSpinBox`` / ``QComboBox`` 默认用滚轮改值：鼠标停在输入框上
滚页面，数值就被悄悄改掉了。设置对话框与模型对话框的正文都套在滚动区里，
这几乎是必然踩到的操作。这三个子类把滚轮改成"滚页面"而不是"改值"。

``NoWheelComboBox`` 还多背一件事：**自己画下拉箭头**。原因见该类的文档字符串。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
)

__all__ = ["NoWheelComboBox", "NoWheelDoubleSpinBox", "NoWheelSpinBox"]

#: 箭头尺寸（画一条 V 形折线，两段各是这个长度）
CARET = 4.0
#: 箭头线宽
CARET_WIDTH = 1.6


class _WheelToScrollArea:
    """把滚轮量交给最近的滚动区；没有滚动区就只吞掉（绝不改值）。

    显式转发而不是只 ``event.ignore()``：依赖 Qt 的"未处理事件向上传播"在
    不同版本与嵌套层级下结果不一致，而直接写滚动条的值是可预测的——无论
    嵌套多深，"不改值 + 页面照常滚动"都成立。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        area = self._enclosing_scroll_area()
        if area is None:
            # 不在滚动区里：值不能改，事件也不必再传（父级没有可滚动的东西）
            event.ignore()
            return
        bar = area.verticalScrollBar()
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()

    def _enclosing_scroll_area(self) -> QAbstractScrollArea | None:
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QAbstractScrollArea):
                return widget
            widget = widget.parentWidget()
        return None


class NoWheelSpinBox(_WheelToScrollArea, QSpinBox):
    """整数输入框：滚轮不改变数值。"""


class NoWheelDoubleSpinBox(_WheelToScrollArea, QDoubleSpinBox):
    """小数输入框：滚轮不改变数值。"""


class NoWheelComboBox(_WheelToScrollArea, QComboBox):
    """下拉框：滚轮不切换选项，并且**自己画下拉箭头**。

    为什么必须自己画
    --------------
    QSS 里只要给子控件写了**任何**属性（哪怕是 ``border: none``），Qt 的样式表
    引擎就接管它的绘制，替换掉原生样式画的箭头；而 ``QComboBox::drop-down``
    规则里没有 ``image:``，于是**什么也不画**。实测（采样下拉区最深像素）：

    - 不套 QSS：93   → 有箭头
    - 现行 QSS：255  → 纯白，没有箭头
    - 删掉那条规则：28 → 箭头回来

    后果是整个应用的下拉框都长得跟输入框一样，用户看不出"这里可以展开"——
    尤其可编辑下拉（模型设置页）看起来就是个文本框。

    两条路：删掉规则拿回系统原生箭头（样式随系统走，与新版界面不搭），
    或者自己画（可控、无外部资源）。这里选后者。
    """

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 先让 QComboBox 画完边框、当前文本、弹出指示区
        super().paintEvent(event)

        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        # QSS 把原生箭头压掉了，这里补一条 V 形折线，颜色跟随当前文字色
        color = QColor(self.palette().color(self.foregroundRole()))
        if not self.isEnabled():
            color = QColor(self.palette().color(self.foregroundRole()))
            color.setAlpha(120)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, CARET_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(QPointF(rect.center().x() - CARET, rect.center().y() - CARET / 2))
        path.lineTo(QPointF(rect.center().x(), rect.center().y() + CARET / 2))
        path.lineTo(QPointF(rect.center().x() + CARET, rect.center().y() - CARET / 2))
        painter.drawPath(path)
        painter.end()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        _WheelToScrollArea.wheelEvent(self, event)