"""Drawer —— 右侧滑入抽屉基类。

用法::

    drawer = Drawer(title="设置", parent=main_window)
    drawer.set_body(my_settings_widget)
    drawer.show()       # 显示
    drawer.hide()       # 关闭

版式约束
--------
WIDTH 是固定的。正文区若装了比 WIDTH 更宽的内容，必须由调用方自己套一层
滚动容器（见 ``app.gui.main_window._scrollable``），否则内容会被压扁。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPoint, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    QPushButton,
)

from ..theme import FONT_FAMILY, FONT_SYMBOL


class Drawer(QFrame):
    """右侧抽屉：遮罩 + 滑动面板。"""

    closed = Signal()

    # 420 装不下设置页的双栏卡片（页面自身最小 640），会横向滚动。
    # 720 是"完整可见"与"不遮住主工作区"之间的实测折中。
    WIDTH = 720

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Drawer")
        self.setProperty("role", "drawer")
        self.setFixedWidth(self.WIDTH)
        # 半透明遮罩背景
        self.setStyleSheet(
            "QFrame[role='drawer'] { background: rgba(255, 255, 255, 0.98);"
            "  border-left: 1px solid #d9dce4; }"
        )

        self._title = QLabel(title)
        # font-family 必须显式写出：只设 font-size 会让 Qt 把字体族重置为默认，
        # 与字体族无关联的符号会渲染成豆腐块（实测）。
        self._title.setStyleSheet(
            f"font-family: {FONT_SYMBOL}, {FONT_FAMILY};"
            " font-weight: 600; font-size: 13pt; padding: 4px 0;"
        )

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_close.setStyleSheet(
            f"QPushButton {{ font-family: {FONT_SYMBOL}, {FONT_FAMILY};"
            "  border: none; background: transparent; color: #5f6368; }"
            "QPushButton:hover { background: #eef1f7; border-radius: 4px; }"
        )
        self.btn_close.clicked.connect(self.hide)

        header = QHBoxLayout()
        header.setContentsMargins(16, 16, 12, 12)
        header.addWidget(self._title, 1)
        header.addWidget(self.btn_close)

        self._body_holder = QFrame()
        self._body_layout = QVBoxLayout(self._body_holder)
        self._body_layout.setContentsMargins(16, 8, 16, 16)
        self._body_layout.setSpacing(8)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(header)
        # 分割线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background: #d9dce4; max-height: 1px;")
        outer.addWidget(line)
        outer.addWidget(self._body_holder, 1)

        self._anim: QPropertyAnimation | None = None
        self.hide()  # 默认隐藏

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_body(self, widget: QWidget) -> None:
        """把目标 widget 装入抽屉正文区（替换旧内容）。"""
        # 清空旧的
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._body_layout.addWidget(widget)

    def add_body(self, widget: QWidget) -> None:
        """追加一个 widget 到正文区。"""
        self._body_layout.addWidget(widget)

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    def show(self) -> None:  # noqa: A003 - Qt 命名
        parent = self.parentWidget()
        if parent is None:
            super().show()
            return
        # 定位到父窗体右侧外
        ph = parent.height()
        pw = parent.width()
        x_hidden = pw  # 完全在父窗体右外侧
        x_shown = pw - self.WIDTH
        y = 0

        self.setGeometry(x_hidden, y, self.WIDTH, ph)
        self.setVisible(True)
        self.raise_()

        # 滑动动画
        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(QPoint(x_hidden, y))
        self._anim.setEndValue(QPoint(x_shown, y))
        self._anim.start()

    def hide(self) -> None:  # noqa: A003 - Qt 命名
        parent = self.parentWidget()
        if parent is None or not self.isVisible():
            super().hide()
            self.closed.emit()
            return
        pw = parent.width()
        x_hidden = pw
        y = 0

        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(x_hidden, y))
        self._anim.finished.connect(self._on_close_finished)
        self._anim.start()

    def _on_close_finished(self) -> None:
        super().hide()
        self.closed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()