"""滚轮不改值的输入控件。

Qt 的 ``QAbstractSpinBox`` / ``QComboBox`` 默认用滚轮改值：鼠标停在输入框上
滚页面，数值就被悄悄改掉了。设置对话框与模型对话框的正文都套在滚动区里，
这几乎是必然踩到的操作。这三个子类把滚轮改成"滚页面"而不是"改值"。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
)

__all__ = ["NoWheelComboBox", "NoWheelDoubleSpinBox", "NoWheelSpinBox"]


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
    """下拉框：滚轮不切换选项。"""