"""DropZone —— 拖放导入区。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class DropZone(QFrame):
    """大圆角虚线框：提示用户拖放视频到此处，或点击选择。"""

    clicked = Signal()
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setProperty("role", "drop-zone")
        self.setMinimumHeight(140)
        self.setMaximumHeight(280)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)

        self.icon_label = QLabel("📁")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("font-size: 48pt; background: transparent;")

        self.title_label = QLabel("拖放视频到此处，或点击选择")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet(
            "font-size: 14pt; font-weight: 600; color: #20232a; background: transparent;"
        )

        self.hint_label = QLabel("支持 .mp4 / .mov / .mkv / .avi / .ts / .m4v")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setStyleSheet(
            "font-size: 10pt; color: #5f6368; background: transparent;"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(8)
        outer.addStretch(1)
        outer.addWidget(self.icon_label)
        outer.addWidget(self.title_label)
        outer.addWidget(self.hint_label)
        outer.addStretch(1)

        self._hover = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(
            u.isLocalFile() for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()
            self._hover = True
            self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._hover = False
        self.update()
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.file_dropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                return

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._hover:
            self._hover = True
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover:
            self._hover = False
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景
        if self._hover:
            bg = QColor("#e8f0fc")
        else:
            bg = QColor("#eef4fb")
        painter.fillRect(self.rect(), bg)

        # 虚线边框
        pen_color = QColor("#3b6cb8") if self._hover else QColor("#b8bdc8")
        pen = QPen(pen_color, 2 if self._hover else 1)
        pen.setDashPattern([6, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        margin = 4
        painter.drawRoundedRect(
            self.rect().adjusted(margin, margin, -margin, -margin),
            12, 12,
        )