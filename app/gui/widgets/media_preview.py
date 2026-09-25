"""MediaPreview —— 帧级预览（带 HUD 与时间码）。

继承原 ``PreviewPanel`` 行为，但加上：
- 顶部 HUD（时间码 + 帧号 + PTS）
- 双击居中（保留旧 API）
- 播放控制按钮图标化

公开 API（与原 PreviewPanel 完全兼容）：
- set_image(image)
- clear(text)
- timecode (QLabel)
- btn_play / btn_boundary / btn_stitch / btn_prev_frame / btn_next_frame /
  btn_back_1s / btn_fwd_1s / btn_back_5s / btn_fwd_5s / btn_stop
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MediaPreview(QWidget):
    """带 HUD 的帧级预览。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._frame_index: int | None = None
        self._pts: str = "--:--:--.---"

        # 画面区
        self.view = QLabel("尚未选择源文件")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(480, 270)
        self.view.setStyleSheet("background:#0e0f13; color:#bbb; border:1px solid #1a1c22;")

        # 时间码（HUD 顶部条）
        self.timecode = QLabel("--:--:--.---")
        self.timecode.setProperty("role", "mono")
        self.timecode.setStyleSheet("color:#cfd2d8; background:#1a1c22; padding:4px 10px;")

        # 控制按钮
        self.btn_play = QPushButton("▶ 播放本集")
        self.btn_boundary = QPushButton("⊞ 切点前后")
        self.btn_stitch = QPushButton("⟳ 成片连看")
        self.btn_prev_frame = QPushButton("|◀ 上一帧")
        self.btn_next_frame = QPushButton("▶| 下一帧")
        self.btn_back_1s = QPushButton("−1s")
        self.btn_fwd_1s = QPushButton("+1s")
        self.btn_back_5s = QPushButton("−5s")
        self.btn_fwd_5s = QPushButton("+5s")
        self.btn_stop = QPushButton("■ 停止")

        for b in (
            self.btn_play,
            self.btn_boundary,
            self.btn_stitch,
            self.btn_prev_frame,
            self.btn_next_frame,
            self.btn_back_1s,
            self.btn_fwd_1s,
            self.btn_back_5s,
            self.btn_fwd_5s,
            self.btn_stop,
        ):
            b.setMinimumWidth(0)
            if b in (self.btn_play, self.btn_boundary):
                b.setProperty("role", "primary")
                # primary 在 QSS 里要重新 polish 才生效
                b.style().unpolish(b)
                b.style().polish(b)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        for b in (
            self.btn_prev_frame,
            self.btn_play,
            self.btn_next_frame,
            self.btn_boundary,
            self.btn_stitch,
        ):
            controls.addWidget(b)
        controls.addStretch(1)
        for b in (
            self.btn_back_5s,
            self.btn_back_1s,
            self.btn_fwd_1s,
            self.btn_fwd_5s,
            self.btn_stop,
        ):
            controls.addWidget(b)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.timecode)
        layout.addLayout(controls)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self._repaint()

    def set_frame_info(self, frame_index: int | None = None, pts: str | None = None) -> None:
        if frame_index is not None:
            self._frame_index = frame_index
        if pts is not None:
            self._pts = pts

    def clear(self, text: str = "尚未选择源文件") -> None:
        self._image = None
        self._frame_index = None
        self.view.setPixmap(QPixmap())
        self.view.setText(text)

    def _repaint(self) -> None:
        if self._image is None:
            return
        self.view.setText("")
        pixmap = QPixmap.fromImage(self._image)
        scaled_pix = pixmap.scaled(
            self.view.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 在画面右下角绘制 HUD（帧号 / PTS）
        if self._frame_index is not None:
            self._overlay_hud(scaled_pix)
        self.view.setPixmap(scaled_pix)

    def _overlay_hud(self, pixmap: QPixmap) -> None:
        if self._frame_index is None:
            return
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = f"#{self._frame_index}"
        font = QFont("Consolas")
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        w = metrics.horizontalAdvance(text) + 12
        h = metrics.height() + 6
        x = pixmap.width() - w - 8
        y = pixmap.height() - h - 8
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(x, y, w, h, 4, 4)
        painter.setPen(QColor("#3b6cb8"))
        painter.drawText(x + 6, y + 3, w - 12, h - 6, Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._repaint()