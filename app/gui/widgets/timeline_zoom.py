"""时间轴缩放条 —— 可见范围提示与缩放入口。

为什么不做拖动滑杆
------------------
时间轴本身已经有滚轮缩放与中键平移，这里只补一组能看见、能点按的入口。
若这里再实现一套拖拽手势与视窗状态，就会出现两个真值来源，时间轴与缩放条
很容易在边界处各说一套。

为什么按钮要可压窄
------------------
中栏宽度由三栏布局分配，固定宽度按钮会把时间轴顶宽。按钮的最小宽度给 0，
栏宽不足时先收按钮、再裁文字，控件本身不参与把整栏撑宽。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from ...core.timebase import format_timecode
from ..theme import (
    FONT_MONO,
    FONT_SIZE_CAPTION,
    current_mode,
    qcolor,
)

__all__ = ["TimelineZoomBar"]


class _MiniButton(QPushButton):
    """缩放条上的小按钮：矮、紧凑，但允许布局把它压到 0 宽。

    QPushButton 默认的最小宽度等于 sizeHint，四个控件排在一起就会给中栏
    设一条不必要的最小宽度。这里只放开"可被压窄"这一条，宽裕时仍按紧凑
    尺寸排布。
    """

    def __init__(
        self,
        text: str,
        max_width: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._max_width = max_width
        self.setProperty("role", "mini")
        self.setFixedHeight(20)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        hint = super().sizeHint()
        return QSize(min(hint.width(), self._max_width), 20)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(0, 20)


class TimelineZoomBar(QWidget):
    """显示当前可见范围，并提供缩放 / 适应入口。

    控件本身不持有视窗状态：范围、百分比与置灰条件全部由 ``set_view`` 传入。
    这样时间轴仍是唯一真值来源，缩放条只负责呈现与发请求。
    """

    zoom_in_requested = Signal()
    zoom_out_requested = Signal()
    fit_requested = Signal()

    HEIGHT = 24
    MIN_SPAN_SECONDS = 2.0
    _EPS = 1e-9
    TOOLTIP = "Ctrl+= / Ctrl+- / Ctrl+0、滚轮缩放、Shift+滚轮或中键平移"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setToolTip(self.TOOLTIP)
        # 只压几何，不写颜色：颜色仍由应用级 QSS 的角色规则决定。
        self.setStyleSheet(
            "QPushButton { min-height: 14px; padding: 0px 4px; }"
        )

        mono = QFont(FONT_MONO.split(",")[0].strip("'"))
        mono.setPointSize(FONT_SIZE_CAPTION)

        self.range_label = QLabel()
        self.range_label.setProperty("role", "mono")
        self.range_label.setFont(mono)
        self.range_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # Ignored 让标签在窄栏里裁切，而不是用文字宽度把整栏顶宽。
        self.range_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.range_label.setMinimumWidth(0)

        self.percent_label = QLabel("100%")
        self.percent_label.setProperty("role", "mono")
        self.percent_label.setFont(mono)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.percent_label.setMinimumWidth(24)
        self.percent_label.setMaximumWidth(56)

        self.btn_zoom_out = _MiniButton("−", 28)
        self.btn_zoom_in = _MiniButton("+", 28)
        self.btn_fit = _MiniButton("适应", 42)

        self.btn_zoom_out.clicked.connect(
            lambda _checked=False: self.zoom_out_requested.emit()
        )
        self.btn_zoom_in.clicked.connect(
            lambda _checked=False: self.zoom_in_requested.emit()
        )
        self.btn_fit.clicked.connect(
            lambda _checked=False: self.fit_requested.emit()
        )

        for child in (
            self.range_label,
            self.percent_label,
            self.btn_zoom_out,
            self.btn_zoom_in,
            self.btn_fit,
        ):
            child.setToolTip(self.TOOLTIP)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(4)
        row.addWidget(self.range_label, 1)
        row.addWidget(self.btn_zoom_out)
        row.addWidget(self.percent_label)
        row.addWidget(self.btn_zoom_in)
        row.addWidget(self.btn_fit)

        self.set_view(0.0, 0.0, 0.0, False)

    def set_view(
        self,
        start_seconds: float,
        span_seconds: float,
        total_seconds: float,
        zoomed: bool,
    ) -> None:
        """按时间轴传来的视窗刷新文案、百分比与按钮置灰状态。

        百分比用四舍五入显示；100% 表示全片适应，此时「适应」和「−」都置灰。
        跨度下限与时间轴一致（``min(total, 2s)``），到达下限时「+」置灰。
        """
        total = max(0.0, float(total_seconds))
        if total <= 0.0:
            start = 0.0
            span = 0.0
            full = True
        else:
            span = min(max(0.0, float(span_seconds)), total)
            start = min(max(0.0, float(start_seconds)), total - span)
            # 即使调用方误把 zoomed 传成真，跨度已经等于全片时也应显示为全片，
            # 否则会出现「100% + 适应可点」的矛盾状态。
            full = (not zoomed) or span >= total - self._EPS

        end = start + span
        prefix = "全片" if full else "可见"
        self.range_label.setText(
            f"{prefix} {format_timecode(start)} – {format_timecode(end)}"
        )

        if full or span <= 0.0:
            percent = 100
        else:
            # 四舍五入（Python 的 round 是银行家舍入，会在此处少进一）。
            percent = max(100, int(total / span * 100.0 + 0.5))
        self.percent_label.setText(f"{percent}%")

        min_span = min(total, self.MIN_SPAN_SECONDS)
        at_min = total <= 0.0 or span <= min_span + self._EPS
        self.btn_zoom_in.setEnabled(not at_min)
        self.btn_zoom_out.setEnabled(not full)
        self.btn_fit.setEnabled(not full)

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        """顶部画一条主题色分隔线，让缩放条与下方时间轴视觉上连成一组。

        颜色每次绘制时现取，切到深色主题后不会留下浅色残线。
        """
        painter = QPainter(self)
        painter.fillRect(
            QRect(0, 0, self.width(), 1),
            qcolor(current_mode(), "BORDER"),
        )
        painter.end()
