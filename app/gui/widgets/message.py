"""统一弹窗出口。

为什么要有这个模块
------------------
弹窗原先散落在各页类里直接调 ``QMessageBox.information/warning/critical``，
主窗口还得把 ``QMessageBox`` 重导出一次供冒烟脚本打补丁。补丁点分散带来一个
实测过的坑：``tools/gui_smoke.py`` 只拦截了 QMessageBox 的**静态方法**，
而某处用 ``QMessageBox(self).exec()`` 构造了实例，离屏跑链路时模态永久阻塞
（挂了 25 分钟才定位）。

因此：所有弹窗从这里出，测试与冒烟脚本只需要 patch 这一个模块的属性。

调用约定
--------
用 ``from ..widgets import message`` 后 ``message.warning(...)``，**不要**
``from .message import warning``——后者会把函数对象绑进调用方命名空间，
此后 patch 模块属性就不再生效。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QScrollArea, QVBoxLayout, QWidget

__all__ = [
    "information",
    "warning",
    "critical",
    "warning_with_action",
    "confirm",
    "confirm_continue",
    "scrollable_confirm",
]


def information(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def warning(parent: QWidget | None, title: str, text: str) -> None:
    """既是警告也是"此路不通"的硬约束提示（沿用既有文案习惯）。"""
    QMessageBox.warning(parent, title, text)


def warning_with_action(
    parent: QWidget | None, title: str, text: str, action_text: str
) -> bool:
    """带一个动作按钮的警告框：返回 True 表示点了 ``action_text``。

    与 :func:`confirm` 的区别只在语气：这里是"出问题了"，动作按钮是**补救**
    （如把无解的成因直接应用到设置上），次要按钮是"知道了"。
    同样是实例级 ``exec()``，离屏自动化必须覆写调用方的方法（见各页的
    ``request_*`` 钩子），不要直接调用本函数。
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    action = box.addButton(action_text, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is action


def critical(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    ok_text: str = "确定",
    cancel_text: str = "取消",
) -> bool:
    """两按钮确认框：返回 True 表示选了 ``ok_text``。

    实例级 ``exec()`` 会阻塞离屏自动化，因此凡"需要用户决策"的地方都应把
    决策提成可覆写方法（如 ``ExportDialog.request_precheck``），测试覆写它
    而不是走这里。
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    refused = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is not refused


def scrollable_confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    ok_text: str,
    cancel_text: str,
    *,
    max_height: int = 420,
    max_width: int = 640,
) -> bool:
    """内容过长的确认框：正文放固定上限的滚动区，绝不让窗口顶出屏幕。

    ``QMessageBox.setText`` 会被超长文本撑爆——零损失快速导出的切点明细
    （一行一个切点，几十上百行）曾把对话框顶出屏幕、按钮够不着。这里把
    正文装进 ``QScrollArea``（固定最大高度，超出即内部上下滚动），按钮行
    固定在底部始终可见。

    短文本场景请继续用 :func:`confirm_continue`（原生样式更简洁）。
    同样是实例级 ``exec()``——离屏自动化请覆写调用方的方法（如
    ``ExportForm._confirm_fast_copy``），不要直接调用本函数。
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)

    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    # 滚动区需要显式宽度参照，否则 QScrollArea 的 sizeHint 恒为最小值
    label.setFixedWidth(max_width - 48)

    scroll = QScrollArea()
    scroll.setWidget(label)
    scroll.setWidgetResizable(True)
    # 短内容不被强行撑到上限：按内容自适应，只有超出上限才固定住开始滚动
    content_h = label.sizeHint().height()
    scroll.setFixedHeight(min(content_h + 4, min(max_height, 420)))
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(scroll)
    box.layout().addWidget(body, 1, 0)

    box.resize(max_width, box.sizeHint().height())
    accepted = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    refused = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is not refused


def confirm_continue(
    parent: QWidget | None,
    title: str,
    text: str,
    ok_text: str,
    cancel_text: str,
) -> bool:
    """「继续 / 返回」式确认（§37 导出前置检查的默认实现）。"""
    return confirm(parent, title, text, ok_text, cancel_text)