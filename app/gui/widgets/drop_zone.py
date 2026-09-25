"""DropZone —— 拖放导入区。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
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

from ..theme import current_mode, qcolor


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

        # 文字颜色与字号全部走全局 QSS 角色（深浅主题各取令牌），
        # 这里只负责结构与 compact 属性切换。注意不要再用 inline
        # stylesheet 写颜色——inline 会盖过主题，切换深色时背刺。
        self.icon_label = QLabel("📁")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setProperty("role", "drop-icon")

        self.title_label = QLabel("拖放视频到此处，或点击选择")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setProperty("role", "drop-title")

        self.hint_label = QLabel("支持 .mp4 / .mov / .mkv / .avi / .ts / .m4v")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setProperty("role", "drop-hint")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(8)
        outer.addStretch(1)
        outer.addWidget(self.icon_label)
        outer.addWidget(self.title_label)
        outer.addWidget(self.hint_label)
        outer.addStretch(1)

        self._hover = False
        self._compact = False

    def set_compact(self, compact: bool) -> None:
        """收成一行「更换源视频」提示。

        导入完成后拖放大图的任务已经结束，留着 140px 起步的 hero 只会把
        下方的分析区顶出可视范围。这里保留可点击（换片）与可拖放（换片）能力，
        只是把高度压到一行；字号/颜色交给 QSS 的 [compact="true"] 变体。
        """
        if compact == self._compact:
            return
        self._compact = compact
        self.icon_label.setVisible(not compact)
        self.hint_label.setVisible(not compact)
        self.title_label.setText("更换源视频" if compact else "拖放视频到此处，或点击选择")
        for label in (self.icon_label, self.title_label):
            label.setProperty("compact", "true" if compact else "false")
            # 动态属性变化不会自动触发 QSS 重算，必须手动 repolish
            label.style().unpolish(label)
            label.style().polish(label)
        margins = (12, 6, 12, 6) if compact else (24, 24, 24, 24)
        self.layout().setContentsMargins(*margins)
        self.setMinimumHeight(40 if compact else 140)
        self.setMaximumHeight(40 if compact else 280)
        self.update()

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

        # 颜色实时取当前主题令牌（浅色下的取值与旧硬编码几乎一致）：
        # 常态底色 BG_HOVER / 悬浮底色 PRIMARY_SOFT / 常态描边 BORDER_STRONG /
        # 悬浮描边 PRIMARY。主题切换后下一次重绘自动跟随。
        mode = current_mode()
        if self._hover:
            bg = qcolor(mode, "PRIMARY_SOFT")
            pen_color = qcolor(mode, "PRIMARY")
            width = 2
        else:
            bg = qcolor(mode, "BG_HOVER")
            pen_color = qcolor(mode, "BORDER_STRONG")
            width = 1
        painter.fillRect(self.rect(), bg)

        pen = QPen(pen_color, width)
        pen.setDashPattern([6, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        margin = 4
        painter.drawRoundedRect(
            self.rect().adjusted(margin, margin, -margin, -margin),
            12, 12,
        )