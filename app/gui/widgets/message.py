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
from PySide6.QtGui import QGuiApplication
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


#: 超过这个行数 / 字符数，information / warning 自动改用滚动框——
#: 导出几十集的汇总（每集一行截断明细）曾把 QMessageBox 撑出屏幕。
_LONG_TEXT_LINES = 8
_LONG_TEXT_CHARS = 600


def _is_long(text: str) -> bool:
    return text.count("\n") >= _LONG_TEXT_LINES or len(text) >= _LONG_TEXT_CHARS


def information(parent: QWidget | None, title: str, text: str) -> None:
    if _is_long(text):
        scrollable_notice(parent, title, text)
        return
    QMessageBox.information(parent, title, text)


def warning(parent: QWidget | None, title: str, text: str) -> None:
    """既是警告也是"此路不通"的硬约束提示（沿用既有文案习惯）。"""
    if _is_long(text):
        scrollable_notice(parent, title, text)
        return
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


def scrollable_notice(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    max_height: int = 420,
    max_width: int = 640,
) -> None:
    """单按钮「知道了」的滚动提示框（information / warning 的长内容形态）。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)

    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    from PySide6.QtGui import QFontMetrics

    metrics = QFontMetrics(label.font())
    longest = max(
        (metrics.horizontalAdvance(line) for line in text.split("\n")), default=0
    )
    label.setFixedWidth(min(longest + 24, max_width - 80))

    scroll = QScrollArea()
    scroll.setWidget(label)
    scroll.setWidgetResizable(False)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setFixedHeight(min(label.sizeHint().height() + 6, max_height))

    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 0, 0)
    body_layout.addWidget(scroll)
    box.layout().addWidget(body, 1, 0)

    box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
    avail_h = QGuiApplication.primaryScreen().availableGeometry().height() - 120
    height = min(box.sizeHint().height(), avail_h)
    box.setFixedSize(label.width() + 80, height)
    box.exec()


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
    """内容过长的确认框：固定尺寸 + 正文滚动，绝不让窗口顶出或塌缩。

    ``QMessageBox.setText`` 会被超长文本撑爆——零损失快速导出的切点明细
    （一行一个切点，几十上百行）曾把对话框顶出屏幕、按钮够不着。这里把
    正文装进 ``QScrollArea``：高度按内容自适应、超过上限（420px）就固定住
    开始滚动；「开始导出 / 取消」按钮固定在底部始终可见。

    宽度必须 ``setFixedSize`` 钉死（且在加完按钮之后）：``QScrollArea`` 的
    最小提示不约束内容宽度，只 ``resize()`` 的话真机 ``exec()`` 的布局回调
    会把宽度塌缩到约 200px（用户实测：窗口窄成一条，时间码全被裁掉）。

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
    # 标签宽度按**最长一行**自适应（封顶 max_width-80）：给换行一个确定参照，
    # 短内容不空旷、长内容封顶后由滚动条接管纵向。
    from PySide6.QtGui import QFontMetrics

    metrics = QFontMetrics(label.font())
    longest = max(
        (metrics.horizontalAdvance(line) for line in text.split("\n")), default=0
    )
    label.setFixedWidth(min(longest + 24, max_width - 80))

    scroll = QScrollArea()
    scroll.setWidget(label)
    scroll.setWidgetResizable(False)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # 短内容按实际高度自适应；超过上限才固定住开始滚动
    scroll.setFixedHeight(min(label.sizeHint().height() + 6, max_height))

    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 0, 0)
    body_layout.addWidget(scroll)
    box.layout().addWidget(body, 1, 0)

    accepted = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    refused = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)

    # 固定尺寸（宽 = max_width；高 = 内容实高，夹在屏幕可用区内）。
    # setFixedSize 让最小尺寸 = 最大尺寸，布局回调无法再把窗口压窄。
    avail_h = QGuiApplication.primaryScreen().availableGeometry().height() - 120
    height = min(box.sizeHint().height(), avail_h)
    box.setFixedSize(label.width() + 80, height)
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