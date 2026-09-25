"""IconButton —— 顶栏圆形图标按钮。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QPushButton


class IconButton(QPushButton):
    """圆形 / 圆角小按钮，用于顶栏图标操作。

    特性：
    - 36×36 固定大小（顶栏标准）
    - 默认扁平，hover 时浅蓝背景
    - 支持 set_notify(True) 显示右上角小红点（未读提示）
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setFixedSize(QSize(36, 36))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setProperty("role", "icon-button")
        self.style().unpolish(self)
        self.style().polish(self)
        self._notify = False

    def set_notify(self, on: bool) -> None:
        """是否在右上角显示未读小红点。"""
        self._notify = bool(on)
        self.setProperty("notify", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def has_notify(self) -> bool:
        return self._notify

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        if not self._notify:
            return
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#b3261e"))
        cx = self.width() - 10
        cy = 10
        painter.drawEllipse(cx - 4, cy - 4, 8, 8)