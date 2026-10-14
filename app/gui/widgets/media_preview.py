"""MediaPreview —— 帧级预览（带 HUD 与时间码）。

公开 API：
- set_image(image) / clear(text) / set_frame_info(frame_index, pts) / set_playing(bool)
- timecode (QLabel)
- btn_play / btn_boundary / btn_stitch / btn_prev_frame / btn_next_frame /
  btn_back_1s / btn_fwd_1s / btn_back_5s / btn_fwd_5s

btn_play 是播放与暂停**共用**的一个键：由 set_playing() 在「▶ 播放」与
「⏸ 暂停」之间切文案，不再单独设暂停键（原先那个「■ 停止」只停表、不复位
播放头，与暂停无可感知差别）。

控制条放在横向滚动容器里：9 个按钮的单行最小宽度接近 900px，直接放进布局
会顶住整栏的最小宽度（中栏就再也不能被压窄了）。装进容器后空间不足时横向
滚动，按钮不会被压扁、也不会把窗口最小宽度顶上去。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .scroll_area import h_scrollable


class MediaPreview(QWidget):
    """带 HUD 的帧级预览。"""

    PLAY_TEXT = "▶ 播放"
    PAUSE_TEXT = "⏸ 暂停"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._frame_index: int | None = None
        self._pts: str = "--:--:--.---"

        # 画面区：最小高度按"能看清切点画面"给，宽度给得宽松（画面按比例缩放）
        self.view = QLabel("尚未选择源文件")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(360, 200)
        self.view.setProperty("role", "video-canvas")

        # 时间码（HUD 顶部条）
        self.timecode = QLabel("--:--:--.---")
        self.timecode.setProperty("role", "mono-hud")

        # 控制按钮
        self.btn_play = QPushButton(self.PLAY_TEXT)
        self.btn_boundary = QPushButton("⊞ 切点前后")
        self.btn_stitch = QPushButton("⟳ 成片连看")
        self.btn_prev_frame = QPushButton("|◀ 上一帧")
        self.btn_next_frame = QPushButton("▶| 下一帧")
        self.btn_back_1s = QPushButton("−1s")
        self.btn_fwd_1s = QPushButton("+1s")
        self.btn_back_5s = QPushButton("−5s")
        self.btn_fwd_5s = QPushButton("+5s")

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
        ):
            b.setMinimumWidth(0)
            if b in (self.btn_play, self.btn_boundary):
                b.setProperty("role", "primary")
                b.setProperty("compact", "true")   # QSS: padding 4px 8px
                # primary 在 QSS 里要重新 polish 才生效
                b.style().unpolish(b)
                b.style().polish(b)
            else:
                # 紧凑档：控制条多个按钮单行，默认 padding 会把右端按钮
                # 挤出审核页右列视口（QSS role="mini"）
                b.setProperty("role", "mini")

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
        ):
            controls.addWidget(b)

        controls_holder = QWidget()
        controls_holder.setLayout(controls)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.timecode)
        layout.addWidget(h_scrollable(controls_holder))

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self._repaint()

    def set_frame_info(self, frame_index: int | None = None, pts: str | None = None) -> None:
        if frame_index is not None:
            self._frame_index = frame_index
        if pts is not None:
            self._pts = pts

    def set_playing(self, playing: bool) -> None:
        """播放键兼作暂停键：按实际状态切文案（唯一的状态展示出口）。"""
        self.btn_play.setText(self.PAUSE_TEXT if playing else self.PLAY_TEXT)

    def clear(self, text: str = "尚未选择源文件") -> None:
        self._image = None
        self._frame_index = None
        self.view.setPixmap(QPixmap())
        self.view.setText(text)

    def _device_pixel_ratio(self) -> float:
        """画面区所在屏幕的缩放比。

        位图要按**物理像素**建、再标上缩放比：按逻辑尺寸建的话，Qt 把它贴到
        高缩放屏幕上时还得再放大一次，画面就发虚（时间轴文字此前是同一种病）。
        """
        try:
            return max(1.0, float(self.view.devicePixelRatioF()))
        except AttributeError:      # 没有该接口的旧 Qt
            return 1.0

    def _repaint(self) -> None:
        if self._image is None:
            return
        self.view.setText("")
        pixmap = QPixmap.fromImage(self._image)
        dpr = self._device_pixel_ratio()
        size = self.view.size()
        target = QSize(
            max(1, int(round(size.width() * dpr))),
            max(1, int(round(size.height() * dpr))),
        )
        scaled_pix = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled_pix.setDevicePixelRatio(dpr)
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
        # 画笔坐标是**逻辑**坐标（位图按逻辑画、按物理存），所以定位要用
        # 逻辑尺寸，不能拿 width()/height() 那对物理像素值来算
        dpr = max(1.0, float(pixmap.devicePixelRatio()))
        x = pixmap.width() / dpr - w - 8
        y = pixmap.height() / dpr - h - 8
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(x, y, w, h, 4, 4)
        painter.setPen(QColor("#3b6cb8"))
        painter.drawText(x + 6, y + 3, w - 12, h - 6, Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._repaint()