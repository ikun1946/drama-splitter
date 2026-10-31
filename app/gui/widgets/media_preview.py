"""MediaPreview —— 帧级预览（带 HUD 与时间码）。

公开 API：
- set_image(image) / clear(text) / set_frame_info(frame_index, pts) / set_playing(bool)
- timecode (QLabel，当前时间) / duration_row (QLabel：「总时长 · 帧号」)
- btn_play / btn_prev_frame / btn_next_frame / btn_boundary / btn_stitch（常驻）
- btn_back_1s / btn_fwd_1s / btn_back_5s / btn_fwd_5s（收在「步进 ▾」菜单里）

btn_play 是播放与暂停**共用**的一个键：由 set_playing() 在「▶ 播放」与
「⏸ 暂停」之间切文案，不再单独设暂停键（原先那个「■ 停止」只停表、不复位
播放头，与暂停无可感知差别）。

控制条不再套横向滚动容器：9 个按钮单行接近 900px。现在常驻只剩 5 个
（播放 / 上一帧 / 下一帧 / 切点前后 / 成片连看），「±1s / ±5s」四个微调键收进
「步进 ▾」弹出菜单——它们仍然是真按钮（装在 QWidgetAction 里），``.clicked``
与 ``.click()`` 照旧可用，只是平时不占宽度。

这一排也不再决定整栏的最小宽度：按钮可被压窄（``_ControlButton``），栏宽不够
时文字裁切而不是顶宽或出横滚动条。配合画面区最小宽降到 260px，中栏的最小
宽度比"9 键 + 横滚动容器"那版低约 100px。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..theme import current_mode, qcolor


class _Divider(QFrame):
    """控制条里 1px 的分组竖线。

    颜色在 paintEvent 里现取：写死进样式表（或构造期缓存 ``qcolor()``）的话，
    切到深色主题后这条线会停在浅色，成为一格异色残留。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(1)

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(qcolor(current_mode(), "BORDER")))
        painter.end()


class _ControlButton(QPushButton):
    """控制条按钮：文字给自然宽度，但不把中栏的最小宽度钉在文字总宽上。

    QPushButton 默认的水平策略是 Minimum（不允许小于 sizeHint），五个中文
    标签加起来就把整栏最小宽度顶到 600+。这里只放开"可被压窄"这一条：宽裕时
    仍按自然宽度排布，栏变窄时按钮收窄、文字随之裁切——比让整栏无法收窄好。
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(0, super().minimumSizeHint().height())


class MediaPreview(QWidget):
    """带 HUD 的帧级预览。"""

    PLAY_TEXT = "▶ 播放"
    PAUSE_TEXT = "⏸ 暂停"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._frame_index: int | None = None
        self._pts: str = "--:--:--.---"
        self._total_frames: int | None = None

        # 画面区：最小高度按"能看清切点画面"给；宽度只留够认出构图的下限——
        # 画面本身按比例缩放，没有一个固定的像素下限需求，而 360px 曾把中栏
        # 的最小宽度钉死（三栏布局里中栏才是最该被压窄的那一栏）。
        self.view = QLabel("尚未选择源文件")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(260, 200)
        self.view.setProperty("role", "video-canvas")

        # 时间信息行：当前时间（大字号）+「总时长 · 帧号」一行小字。三块各占
        # 一格时，中栏一窄就折成两行；总时长和帧号都只是参考数，并成一行小字，
        # 让"现在在哪"独占视觉重心。
        self.timecode = QLabel("--:--:--.---")
        self.timecode.setProperty("role", "timecode-now")
        self.duration_row = QLabel("")
        self.duration_row.setProperty("role", "muted")
        # 兼容占位：外部脚本按名字读这两条文案（冒烟测试也在读）。它们不进
        # 布局、默认隐藏，正文由 duration_row 同一份数据派生，不另存一套数字。
        self.total_timecode = QLabel("", self)
        self.total_timecode.setProperty("role", "muted")
        self.total_timecode.hide()
        self.frame_number = QLabel("", self)
        self.frame_number.setProperty("role", "muted")
        self.frame_number.hide()
        # 时间信息：当前时间（大字号）独占一行，「总时长 · 帧号」放在下一行小字。
        # 两行而不是并排——并排时两段文字在 1180 宽的窗口里合计 675px，中栏给不了，
        # 后一段会被静默裁掉半截。上下叠放后任何可用宽度都放得下，参考数也不抢重心。
        time_row = QVBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(2)
        time_row.addWidget(self.timecode)
        time_row.addWidget(self.duration_row)
        time_holder = QWidget()
        time_holder.setLayout(time_row)

        # 控制按钮：常驻 5 个 + 「步进 ▾」菜单（菜单里是 4 个微调键）
        self.btn_play = _ControlButton(self.PLAY_TEXT)
        self.btn_boundary = _ControlButton("⊞ 切点前后")
        self.btn_stitch = _ControlButton("⟳ 成片连看")
        self.btn_prev_frame = _ControlButton("|◀ 上一帧")
        self.btn_next_frame = _ControlButton("▶| 下一帧")
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
        ):
            b.setMinimumWidth(0)
            # 允许被压窄：这排按钮不该成为整栏的最小宽度上限
            b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
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
            if b is self.btn_play:
                # 只有播放是主操作；切点前后/成片连看降成次级，别和它抢注意力
                b.setProperty("role", "primary")
                b.setProperty("compact", "true")   # QSS: padding 4px 8px
                # primary 在 QSS 里要重新 polish 才生效
                b.style().unpolish(b)
                b.style().polish(b)
            else:
                # 紧凑档：控制条多个按钮单行，默认 padding 会把右端按钮
                # 挤出审核页右列视口（QSS role="mini"）
                b.setProperty("role", "mini")

        # 按用途分两组：逐帧（上一帧 / 播放 / 下一帧）｜检视（切点前后 / 成片连看）。
        # 微调（±秒）不在这一排：9 键单行近 900px，会把中栏最小宽度顶死。
        controls = QHBoxLayout()
        controls.setSpacing(6)
        for b in (self.btn_prev_frame, self.btn_play, self.btn_next_frame):
            controls.addWidget(b)
        self.group_divider = _Divider()
        controls.addWidget(self.group_divider)
        for b in (self.btn_boundary, self.btn_stitch):
            controls.addWidget(b)
        controls.addStretch(1)

        # 「步进 ▾」：四个微调键装在弹出菜单里，仍是真 QPushButton，
        # 所以 stage_view 的 clicked 接线、setEnabled 与 .click() 全都不用改。
        self.btn_step_menu = QPushButton("步进 ▾")
        self.btn_step_menu.setProperty("role", "mini")
        self.btn_step_menu.setMinimumWidth(0)
        self.step_menu = QMenu(self.btn_step_menu)
        step_holder = QWidget(self.step_menu)
        step_col = QGridLayout(step_holder)
        step_col.setContentsMargins(6, 6, 6, 6)
        step_col.setSpacing(4)
        for row, b in enumerate(
            (self.btn_back_5s, self.btn_back_1s, self.btn_fwd_1s, self.btn_fwd_5s)
        ):
            step_col.addWidget(b, row, 0)
            b.clicked.connect(self.step_menu.close)   # 选完即收，免得挡住画面
        step_action = QWidgetAction(self.step_menu)
        step_action.setDefaultWidget(step_holder)
        self.step_menu.addAction(step_action)
        self.btn_step_menu.clicked.connect(self._open_step_menu)
        controls.addWidget(self.btn_step_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.view, 1)
        layout.addWidget(time_holder)
        layout.addLayout(controls)

    def _open_step_menu(self) -> None:
        """在「步进 ▾」按钮正下方弹出微调菜单。"""
        self.step_menu.setMinimumWidth(self.btn_step_menu.width())
        self.step_menu.popup(
            self.btn_step_menu.mapToGlobal(QPoint(0, self.btn_step_menu.height()))
        )

    def set_total(self, duration: str, total_frames: int | None = None) -> None:
        """设置总时长与总帧数（合并行显示「总时长 00:02:00.000 · 帧 417 / 18441」）。"""
        self._total_frames = total_frames
        self.total_timecode.setText(f"/ {duration}" if duration else "")
        self._refresh_frame_label()

    def _refresh_frame_label(self) -> None:
        """一处写、两处读：帧号占位与合并行都从同一份数字派生。"""
        if self._frame_index is None:
            self.frame_number.setText("")
        elif self._total_frames:
            self.frame_number.setText(f"帧 {self._frame_index} / {self._total_frames}")
        else:
            self.frame_number.setText(f"帧 #{self._frame_index}")

        # 占位标签的 "/ 00:02:00.000" 是"当前 / 总时长"的旧写法，合并行里
        # 没有分子，这里补上名字再拼，读起来才不是一串孤零零的斜杠。
        total = self.total_timecode.text().lstrip("/ ").strip()
        parts = [f"总时长 {total}"] if total else []
        if self.frame_number.text():
            parts.append(self.frame_number.text())
        self.duration_row.setText(" · ".join(parts))

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self._repaint()

    def set_frame_info(self, frame_index: int | None = None, pts: str | None = None) -> None:
        if frame_index is not None:
            self._frame_index = frame_index
        if pts is not None:
            self._pts = pts
        self._refresh_frame_label()

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