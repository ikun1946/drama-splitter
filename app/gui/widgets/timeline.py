"""TimelineRuler —— 轻量时间轴（切点标记 + 播放头 + 拖拽，不做帧缩略图）。

为什么不做缩略图
----------------
1 小时 1080p 约 10.8 万帧，直读必卡，需要另建缩略图金字塔与缓存策略。本版
先给出"导航能力"：一眼看到所有切点在哪、锁定与否、当前在哪一集，并能直接
拖动切点。缩略图作为后续独立模块，不影响本控件的接口。

绘制分两层（性能要求）
----------------------
- **静态层**：轨道、集分段、切点标记、选中集、导出完成标记 —— 绘入缓存
  ``QPixmap``，键 =（尺寸, DPR, 主题模式, 方案结构, 选中集, 错误切点, 视窗）。
  只有这些变化才重建。
- **动态层**：播放头与拖拽预览线，直接画在缓存之上。

``set_playhead`` 只重绘新旧播放头所在的窄条。做成这样是因为 ``StreamPlayer.
position_changed`` 在播放时每帧都触发，若每帧重建整条时间轴，播放会掉帧。
``_static_rebuild_count`` 是给测试用的计数器（1000 次 set_playhead 后必须为 0）。

坐标与精度
----------
秒 ↔ 像素的换算全部走 :class:`fractions.Fraction`，不经过 float；拖动松手后
先用 ``media.snap_to_frame`` 吸附到帧起点，再由 ``plan.time_base`` 转成整数
ticks 发出。float 直转 ticks 会在长片尾部累积可见误差。

视窗（放大 / 平移）
-------------------
控件默认把整片映射到宽度上；``zoom_by`` / ``zoom_to`` 等把可见区间收缩到
一部分，坐标换算始终以当前视窗为基准。视窗变化会重建静态层（刻度、分段的
裁切都变了），但播放头仍走脏矩形增量刷新——所以任何缩放/平移操作都不该
出现在每帧调用的路径上。
"""

from __future__ import annotations

import time
from fractions import Fraction

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import QToolTip, QWidget

from ..theme import FONT_MONO, FONT_SIZE_CAPTION, current_mode, qcolor

__all__ = ["TimelineRuler"]


class TimelineRuler(QWidget):
    """整片时间轴：集分段 + 切点 + 播放头。

    信号里的时间一律给接收方最原始的形式：播放头给 float 秒（帧解码器接受
    float），切点给**整数 ticks**（方案模型的唯一真值形式，避免二次换算）。
    """

    seek_requested = Signal(float)          # 松手/点击：接收方要精确落定
    scrub_requested = Signal(float)         # 拖动中（30Hz）：接收方只需尽快出画面
    boundary_move_requested = Signal(int, int)   # (边界下标, 新 ticks)
    episode_activated = Signal(int)              # 集号（1 起）
    view_changed = Signal(float, float)          # (视窗起点秒, 视窗跨度秒)

    MARGIN = 10                                  # 左右留白（放时长标注）
    TRACK_TOP = 30                               # 轨道上沿
    TRACK_HEIGHT = 28                            # 轨道高：20 偏扁，集号挤在中间看不清
    # 播放头的**时间气泡**：轨道下方那一条（拖动时不必回头看上面的时间码行）。
    # 高度 72 → 92 就是为它留的：轨道到 58 结束，气泡占 62–80，两侧留 4px 余量。
    BUBBLE_TOP = 62
    BUBBLE_HEIGHT = 18
    BUBBLE_RADIUS = 6
    BUBBLE_MAX_WIDTH = 132                       # 比它还宽就截（HH:MM:SS.mmm 够用）
    HEIGHT = 92
    SEGMENT_GAP = 2                              # 分段之间的缝
    SEGMENT_RADIUS = 4                           # 分段圆角
    MARKER_OVERHANG = 7                          # 切点竖线超出轨道的高度
    HIT_RADIUS = 6                               # 命中半径（像素）
    DRAG_THROTTLE = 1 / 30                       # 拖动播放头的发信号节流
    # 集号两侧留白：分段宽度至少要有「字形宽 + 这么多」才画（否则贴边看不清）
    EPISODE_LABEL_PAD = 14

    # 视窗下限取 2 秒：比这更窄时一屏只剩几帧，刻度与切点全挤在一起。
    MIN_VIEW_SPAN = Fraction(2)
    ZOOM_STEP = Fraction(3, 2)                   # zoom_in / zoom_out 每级倍数
    WHEEL_ZOOM_STEP = Fraction(6, 5)             # 滚轮一格 1.2×
    WHEEL_PAN_FRACTION = Fraction(3, 20)         # Shift+滚轮平移 15% 视窗
    ZOOM_TO_PADDING = Fraction(1, 25)            # zoom_to 两侧各留 4%
    FOLLOW_LOW = Fraction(1, 10)                 # 跟随播放头：视窗 10%~90% 之外就翻页
    FOLLOW_HIGH = Fraction(9, 10)
    # 自适应刻度间隔（秒）。从最小档往上找，第一档像素间距 ≥ GRID_MIN_PX 就用它。
    GRID_INTERVALS = (
        Fraction(1, 5), Fraction(1, 2), Fraction(1), Fraction(2), Fraction(5),
        Fraction(10), Fraction(15), Fraction(30), Fraction(60), Fraction(120),
        Fraction(300), Fraction(600), Fraction(900), Fraction(1800), Fraction(3600),
    )
    GRID_MIN_PX = 78
    GRID_TOP = 13                                # 刻度标签占的高度（轨道从 30 开始）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._media = None
        self._plan = None
        self._exported: dict = {}
        self._selected: int | None = None

        # 几何缓存（秒制，绘制时再换算像素）
        self._segments: list[tuple[int, Fraction, Fraction]] = []
        self._markers: list[tuple[int, Fraction, bool]] = []   # (边界下标, 秒, 锁定)
        self._vfr = False

        self._playhead = Fraction(0)
        self._error_index: int | None = None
        self._error_message = ""

        self._static: QPixmap | None = None
        self._static_key: tuple | None = None
        self._static_rebuild_count = 0
        #: 本次静态层里真正画出来的集号个数（测试用：见 test_episode_numbers）
        self._episode_labels_drawn = 0

        # 视窗：_view_span 为 None 表示"适应全片"（此时 _view_start 恒为 0）。
        # 用 None 而不是 Fraction(total) 表示全片，避免同一个状态有两种写法，
        # 让"是否变化"的比较与缓存键保持唯一。
        self._view_start = Fraction(0)
        self._view_span: Fraction | None = None
        self._follow_playhead = False

        # 拖拽状态
        self._drag: str | None = None                # "playhead" / "boundary" / "pan"
        self._drag_index = 0
        self._drag_seconds = Fraction(0)
        self._last_emit = 0.0
        self._pan_origin_x = 0.0                    # 中键平移：按下时的光标 x
        self._pan_origin_start = Fraction(0)        # 中键平移：按下时的视窗起点

    # ---- 外部接口 ------------------------------------------------------

    def set_project(self, media, plan, exported: dict | None = None,
                    selected_episode: int | None = None) -> None:
        """重建静态层。媒体/方案变了就调它（这是唯一的结构来源）。"""
        self._media = media
        self._plan = plan
        self._exported = dict(exported or {})
        self._vfr = bool(getattr(media, "is_vfr", False))
        self._segments = []
        self._markers = []
        if media is not None and plan is not None:
            for episode in plan.episodes():
                self._segments.append(
                    (episode.index, episode.start_seconds, episode.end_seconds)
                )
            for episode in plan.episodes()[:-1]:
                ticks = episode.end_ticks
                self._markers.append(
                    (episode.index, episode.end_seconds, ticks in plan.locked_ticks)
                )
        self._selected = selected_episode
        self._error_index = None
        self._error_message = ""
        # 换片/重排后旧视窗的秒数不再有意义：一律回到全片适应。走 _apply_view
        # 是为了让"视窗只有一个变更入口"这条不变式不被打破（也确实变了才发信号）。
        if self._total_seconds() > 0:
            self._apply_view(Fraction(0), self._total_seconds())
        else:
            self._view_start = Fraction(0)
            self._view_span = None
        self.invalidate_static()

    def set_selected_episode(self, index: int | None) -> None:
        if index != self._selected:
            self._selected = index
            self.invalidate_static()

    def set_playhead(self, seconds) -> None:
        """外部（播放器 / 选集）驱动播放头。拖动中忽略，避免与手势打架。"""
        if self._drag == "playhead":
            return
        position = Fraction(seconds).limit_denominator(1_000_000)
        if position == self._playhead:
            # 位置没变不代表不用动：用户可能把视窗平移走了，跟随模式要
            # 把播放头重新拉回来。
            self._follow_page()
            return
        old = self._playhead_rect()
        self._playhead = position
        if self._follow_page():
            return
        new = self._playhead_rect()
        if old is not None:
            self.update(old)
        if new is not None:
            self.update(new)

    def _follow_page(self) -> bool:
        """跟随播放头：落到视窗 10%~90% 之外时整窗居中。

        一次翻到中央，而不是把播放头钉在边界：后者每帧都要挪窗并重建
        静态层，播放会掉帧。翻页后播放头落在 50%，还能再跑 40%。
        """
        if not (self._follow_playhead and self.is_zoomed()):
            return False
        start, span = self._view_start, self.visible_span()
        ratio = (self._playhead - start) / span
        if self.FOLLOW_LOW <= ratio <= self.FOLLOW_HIGH:
            return False
        self._apply_view(self._playhead - span / 2, span)
        return True

    def set_error(self, boundary_index: int, message: str = "") -> None:
        """把某个切点标红并附上原因（非法拖动时由接线方调用）。"""
        self._error_index = boundary_index
        self._error_message = message
        self.invalidate_static()

    def clear_error(self) -> None:
        if self._error_index is not None or self._error_message:
            self._error_index = None
            self._error_message = ""
            self.invalidate_static()

    def invalidate_static(self) -> None:
        self._static = None
        self.update()

    def playhead_seconds(self) -> Fraction:
        return self._playhead

    # ---- 视窗（缩放 / 平移） -------------------------------------------

    def visible_start(self) -> Fraction:
        if self._total_seconds() <= 0:
            return Fraction(0)
        return self._view_start

    def visible_span(self) -> Fraction:
        total = self._total_seconds()
        if total <= 0:
            return Fraction(0)
        if self._view_span is None:
            return total
        return min(self._view_span, total)

    def is_zoomed(self) -> bool:
        total = self._total_seconds()
        return total > 0 and self.visible_span() < total

    def zoom_by(self, factor, anchor_seconds=None) -> None:
        """以锚点为不动点缩放（factor > 1 放大）。

        锚点默认取播放头；播放头不在视窗内时取视窗中心，避免"缩一下却
        跳到片子另一头"。锚点在视窗内的相对位置保持不变。
        """
        if self._total_seconds() <= 0:
            return
        factor = self._as_fraction(factor)
        if factor <= 0:
            return
        start, span = self._view_start, self.visible_span()
        if anchor_seconds is None:
            anchor = self._playhead
            if not (start <= anchor <= start + span):
                anchor = start + span / 2
        else:
            anchor = self._as_fraction(anchor_seconds)
        new_span = span / factor
        relative = (anchor - start) / span
        self._apply_view(anchor - relative * new_span, new_span)

    def zoom_in(self) -> None:
        self.zoom_by(self.ZOOM_STEP)

    def zoom_out(self) -> None:
        self.zoom_by(1 / self.ZOOM_STEP)

    def zoom_to(self, start, end) -> None:
        """放大到指定区间：两侧各留 4% 余量，并夹到最小跨度。"""
        if self._total_seconds() <= 0:
            return
        start = self._as_fraction(start)
        end = self._as_fraction(end)
        if end < start:
            start, end = end, start
        minimum = min(self._total_seconds(), self.MIN_VIEW_SPAN)
        duration = end - start
        if duration <= 0:
            # 退化成一个点：直接以该点为中心取最小跨度。
            self._apply_view(start - minimum / 2, minimum)
            return
        padding = duration * self.ZOOM_TO_PADDING
        new_start = start - padding
        new_span = duration + 2 * padding
        if new_span < minimum:
            # 最小跨度比区间还宽：以区间中心为锚扩到下限，而不是贴左边界。
            center = (start + end) / 2
            new_span = minimum
            new_start = center - new_span / 2
        self._apply_view(new_start, new_span)

    def fit_all(self) -> None:
        self._apply_view(Fraction(0), self._total_seconds())

    def pan(self, seconds_delta) -> None:
        self._apply_view(
            self._view_start + self._as_fraction(seconds_delta),
            self.visible_span(),
        )

    def reveal(self, seconds, margin: float = 0.08) -> None:
        """确保目标在视窗内；已经在内侧安全区就不动。

        margin 是安全区占视窗宽度的比例。留这条安全带是因为播放头贴着
        边缘时要来回挪窗（每挪一次就重建静态层），看起来会抖。
        """
        if self._total_seconds() <= 0:
            return
        target = self._as_fraction(seconds)
        start, span = self._view_start, self.visible_span()
        margin = max(Fraction(0), min(self._as_fraction(margin), Fraction(1, 2)))
        if start + span * margin <= target <= start + span * (1 - margin):
            return
        self._apply_view(target - span / 2, span)

    def set_follow_playhead(self, enabled: bool) -> None:
        self._follow_playhead = bool(enabled)

    def _apply_view(self, start, span) -> None:
        """所有视窗变更的唯一入口：夹取 → 更新 → 重建 → 通知。

        夹取只在这里做，调用方不必各自防守；只有值确实变化时才重建与发
        信号，避免重复缩放空转重绘。
        """
        total = self._total_seconds()
        if total <= 0:
            return
        start = self._as_fraction(start)
        span = self._as_fraction(span)
        minimum = min(total, self.MIN_VIEW_SPAN)
        span = max(minimum, min(span, total))
        start = max(Fraction(0), min(start, total - span))
        if span >= total:
            start, span_state = Fraction(0), None
        else:
            span_state = span
        if start == self._view_start and span_state == self._view_span:
            return
        self._view_start = start
        self._view_span = span_state
        self.invalidate_static()
        self.view_changed.emit(float(start), float(span))

    @staticmethod
    def _as_fraction(value) -> Fraction:
        if isinstance(value, Fraction):
            return value
        if isinstance(value, int):
            return Fraction(value)
        return Fraction(value).limit_denominator(1_000_000)

    # ---- 坐标换算（测试直接调用） --------------------------------------

    def _usable_width(self) -> int:
        return max(1, self.width() - 2 * self.MARGIN)

    def _total_seconds(self) -> Fraction:
        if self._media is None:
            return Fraction(0)
        return Fraction(self._media.timeline_duration)

    def _x_from_seconds(self, seconds) -> float:
        total = self._total_seconds()
        if total <= 0:
            return float(self.MARGIN)
        span = self.visible_span()
        if span <= 0:
            return float(self.MARGIN)
        ratio = (Fraction(seconds) - self._view_start) / span
        return self.MARGIN + float(ratio) * self._usable_width()

    def _seconds_from_x(self, x: float) -> Fraction:
        total = self._total_seconds()
        if total <= 0:
            return Fraction(0)
        span = self.visible_span()
        if span <= 0:
            return Fraction(0)
        ratio = Fraction(x - self.MARGIN) / self._usable_width()
        seconds = self._view_start + ratio * span
        return max(Fraction(0), min(seconds, total))

    def _seconds_in_view(self, seconds) -> bool:
        if self._media is None or self._total_seconds() <= 0:
            return False
        start, span = self._view_start, self.visible_span()
        value = seconds if isinstance(seconds, Fraction) else Fraction(seconds)
        return start <= value <= start + span

    # ---- 绘制 ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._media is None or self._total_seconds() <= 0:
            painter.setPen(QColor(qcolor(current_mode(), "TEXT_MUTED")))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "未载入素材")
            return

        key = (
            self.width(),
            self.height(),
            # 屏幕缩放比也是关键：换到另一块缩放不同的屏幕时必须重建，
            # 否则那张位图的分辨率对不上，字会发虚。
            self.devicePixelRatioF(),
            current_mode(),
            self._structure_key(),
            self._selected,
            self._error_index,
            self._view_start,
            self._view_span,
        )
        if self._static is None or key != self._static_key:
            self._static = self._rebuild_static()
            self._static_key = key
            self._static_rebuild_count += 1
        painter.drawPixmap(0, 0, self._static)

        if self._drag == "boundary":
            self._draw_drag_preview(painter)
        self._draw_playhead(painter)

    def _structure_key(self) -> tuple:
        """方案结构指纹：只有边界/锁定/例外变化才需要重建静态层。"""
        if self._plan is None:
            return ()
        return (
            tuple(self._plan.boundary_ticks),
            tuple(sorted(self._plan.locked_ticks)),
            tuple(sorted(self._plan.exceptions)),
            tuple(sorted(self._exported)),
        )

    def _rebuild_static(self) -> QPixmap:
        mode = current_mode()
        self._episode_labels_drawn = 0
        # 位图必须按屏幕缩放比建：不设 devicePixelRatio 的话，在 125% / 150%
        # 缩放的屏幕上这张图会被**插值放大**——里面的时间码与刻度线全部发虚
        # （用户报「这几个数字看着很模糊」）。analysis_pane 的缩略图一直是对的，
        # 这里当初漏了。
        dpr = max(1.0, self.devicePixelRatioF())
        pixmap = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        top, height = self.TRACK_TOP, self.TRACK_HEIGHT
        left, right = self.MARGIN, self.width() - self.MARGIN
        track_rect = QRect(left, top, max(1, right - left), height)

        # 轨道底
        painter.setPen(QPen(QColor(qcolor(mode, "BORDER")), 1))
        painter.setBrush(QBrush(QColor(qcolor(mode, "TL_TRACK"))))
        painter.drawRoundedRect(track_rect, 4, 4)

        # 集分段：左右各留一条缝并画圆角，相邻集在视觉上才分得开。
        # 放大后分段会延伸到轨道外，必须裁剪到轨道——否则首尾露出圆角，看着
        # 像"这里真的又是一集的边界"。完全落在视窗外的分段连画都不画。
        view_start, view_span = self._view_start, self.visible_span()
        view_end = view_start + view_span
        if self._segments:
            painter.save()
            painter.setClipRect(track_rect)
            for position, (index, segment_start, segment_end) in enumerate(self._segments):
                if segment_end <= view_start or segment_start >= view_end:
                    continue
                x0 = self._x_from_seconds(segment_start)
                x1 = self._x_from_seconds(segment_end)
                gap = self.SEGMENT_GAP if position < len(self._segments) - 1 else 0
                rect = QRect(
                    int(x0) + self.SEGMENT_GAP // 2,
                    top + 1,
                    max(1, int(x1 - x0) - self.SEGMENT_GAP - gap // 2),
                    height - 2,
                )
                if index == self._selected:
                    painter.setBrush(QBrush(QColor(qcolor(mode, "BG_SELECTED"))))
                else:
                    tone = "TL_SEGMENT" if position % 2 == 0 else "TL_SEGMENT_ALT"
                    painter.setBrush(QBrush(QColor(qcolor(mode, tone))))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(
                    rect, self.SEGMENT_RADIUS, self.SEGMENT_RADIUS
                )
                if index in self._plan.exceptions:
                    self._draw_stripes(painter, rect, qcolor(mode, "WARN"))
                if index in self._exported:
                    # 右上角一枚小圆点：比底部那条色带更轻，不与集号打架
                    painter.setBrush(QBrush(QColor(qcolor(mode, "SUCCESS"))))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(
                        rect.right() - 9, rect.top() + 5, 5, 5
                    )
                # 集号：没有它，用户得回左栏按时间对数才知道色块是哪一集。
                # 只在分段宽度容得下整个数字时画（阈值按实际字形宽度算，不写
                # 魔法数），窄分段让位给切点标记与导出圆点。
                label = str(index)
                font = QFont(FONT_MONO.split(",")[0].strip("'"))
                font.setPointSize(FONT_SIZE_CAPTION)
                painter.setFont(font)
                label_w = painter.fontMetrics().horizontalAdvance(label)
                # 放大后一集往往比屏幕宽得多：集号若仍按整段居中，文字会落在
                # 屏幕外，屏幕上反而看不出这是第几集。因此按"可见部分"居中。
                label_rect = rect.intersected(track_rect)
                if label_rect.width() >= label_w + self.EPISODE_LABEL_PAD:
                    # 用正常文字色：11pt 的 TEXT_MUTED 压在浅底上发虚（两端时长
                    # 标注踩过同一个坑），而集号是要被"读"的导航信息。
                    painter.setPen(QColor(qcolor(mode, "TEXT")))
                    painter.drawText(
                        label_rect, Qt.AlignmentFlag.AlignCenter, label
                    )
                    self._episode_labels_drawn += 1
            painter.restore()

        # 选中集顶线
        if self._selected is not None:
            for index, start, end in self._segments:
                if index != self._selected:
                    continue
                x0 = self._x_from_seconds(start)
                x1 = self._x_from_seconds(end)
                painter.setPen(QPen(QColor(qcolor(mode, "PRIMARY")), 2))
                painter.drawLine(int(x0), top, int(x1), top)
                break

        # 放大时用自适应刻度替代两端时间码：全片两端的时间码在放大后离屏幕
        # 很远，只有刻度能回答"当前这一屏具体是哪一段"。
        if self.is_zoomed():
            self._draw_grid(painter, left, right, top)

        # 切点标记
        for boundary_index, seconds, locked in self._markers:
            x = int(self._x_from_seconds(seconds))
            if x < left or x > right:
                continue
            if boundary_index == self._error_index:
                color = QColor(qcolor(mode, "DANGER"))
            else:
                color = QColor(qcolor(mode, "PRIMARY") if locked else qcolor(mode, "NEUTRAL"))
            painter.setPen(QPen(color, 2 if locked else 1))
            painter.drawLine(x, top - self.MARKER_OVERHANG, x,
                             top + height + self.MARKER_OVERHANG)
            self._draw_diamond(painter, x, top - self.MARKER_OVERHANG - 4,
                               color, filled=locked)

        # 两端时长标注（只在全片适应时画；放大后由上面的刻度接管）
        #
        # 两端统一用 format_timecode（HH:MM:SS.mmm）——这是本项目时间码的规范
        # 格式，与预览 HUD、审核卡片一致。原先左端写死 "00:00.000"、右端是
        # "600.067s"，一边像时间码一边像秒表读数，看着像 bug（用户就是这么报的）。
        # 时间码用正常文字色而不是 TEXT_MUTED：浅灰压浅底本身就发虚，
        # 加上缩放屏的插值就变成用户说的「看着很模糊」。
        if not self.is_zoomed():
            painter.setPen(QColor(qcolor(mode, "TEXT")))
            font = QFont(FONT_MONO.split(",")[0].strip("'"))
            font.setPointSize(FONT_SIZE_CAPTION)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            start_text, end_text, show_start = self._time_labels(metrics)
            top_label_h = 16
            start_w = metrics.horizontalAdvance(start_text) + 4
            end_w = metrics.horizontalAdvance(end_text) + 4
            if show_start:
                painter.drawText(QRect(left, 2, start_w, top_label_h),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 start_text)
            painter.drawText(QRect(right - end_w, 2, end_w, top_label_h),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             end_text)

        if self._vfr:
            painter.setPen(QColor(qcolor(mode, "WARN")))
            if self.is_zoomed():
                # 放大的刻度占满 0–13px：把 VFR 提示下移一条，避免压住刻度数字。
                painter.drawText(QRect(left, self.GRID_TOP + 1, right - left, 15),
                                 Qt.AlignmentFlag.AlignCenter, "VFR 源 · 按时间吸附")
            else:
                painter.drawText(QRect(left, 2, right - left, 16),
                                 Qt.AlignmentFlag.AlignCenter, "VFR 源 · 按时间吸附")
        painter.end()
        return pixmap

    def _draw_grid(self, painter: QPainter, left: int, right: int, top: int) -> None:
        """放大时的自适应时间刻度：顶部标签 + 轨道上沿短刻线。

        间隔不固定：从 ``GRID_INTERVALS`` 里取第一档"像素间距 ≥ 78"的，
        任何放大倍数下标签都不会挤在一起；否则要么密集到读不出，要么
        一屏只有一两个数字。
        """
        from ...core.timebase import format_timecode

        mode = current_mode()
        span = self.visible_span()
        if span <= 0:
            return
        pixels_per_second = max(1, right - left) / span
        interval = self.GRID_INTERVALS[-1]
        for candidate in self.GRID_INTERVALS:
            if candidate * pixels_per_second >= self.GRID_MIN_PX:
                interval = candidate
                break

        font = QFont(FONT_MONO.split(",")[0].strip("'"))
        font.setPointSize(FONT_SIZE_CAPTION)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        start = self._view_start
        end = start + span
        tick = (start // interval) * interval
        if tick < start:
            tick += interval
        while tick <= end:
            x = int(round(self._x_from_seconds(tick)))
            painter.setPen(QPen(QColor(qcolor(mode, "BORDER")), 1))
            painter.drawLine(x, top, x, top + 4)
            if interval < 1:
                # 亚秒级：M:SS.s 比完整时间码短一截，78px 才放得下。
                minutes = int(tick // 60)
                text = f"{minutes}:{float(tick - minutes * 60):04.1f}"
            else:
                text = format_timecode(tick).split(".")[0]
            width = metrics.horizontalAdvance(text) + 6
            # 两端放不下的刻度直接不画：贴着边缘居中会被窗口切开，拉回控件内
            # 又会压到相邻刻度上（实测 0s 与 5s 两格会叠成一串数字）。
            left_edge = x - width // 2
            if left_edge < 0 or left_edge + width > self.width():
                tick += interval
                continue
            painter.setPen(QColor(qcolor(mode, "TEXT_MUTED")))
            painter.drawText(
                QRect(left_edge, 0, width, self.GRID_TOP),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )
            tick += interval

    def _time_labels(self, metrics) -> tuple[str, str, bool]:
        """两端时长标注的文案。

        两端统一走 ``format_timecode``（HH:MM:SS.mmm）——这是本项目时间码的
        规范格式，与预览 HUD、审核卡片一致。原先左端写死 "00:00.000"、右端是
        "600.067s"（一边像时间码一边像秒表读数），用户直接把它当 bug 报了回来。

        返回 ``(左端文案, 右端文案, 是否画左端)``：窄窗口下 Qt 不会自动避让，
        两段文字会重叠。左端恒为 00:00:00.000 没有信息量，空间不够时优先舍弃它，
        保住右端的总时长。阈值用实际字形宽度算，不用魔法数。
        """
        from ...core.timebase import format_timecode

        start_text = format_timecode(Fraction(0))
        end_text = format_timecode(self._total_seconds())
        gap = 12
        needed = (
            metrics.horizontalAdvance(start_text)
            + metrics.horizontalAdvance(end_text)
            + gap
        )
        show_start = needed <= max(1, self.width() - 2 * self.MARGIN)
        return start_text, end_text, show_start

    def _draw_stripes(self, painter: QPainter, rect: QRect, color: str) -> None:
        """例外集斜纹：对角细线，稀疏到不与集号冲突。

        放大后分段可能宽过轨道：先按当前裁剪区（轨道）收窄再画，避免为
        最终被裁掉的像素生成成千上万条线。
        """
        painter.save()
        area = rect
        clip = painter.clipBoundingRect().toRect()
        if not clip.isEmpty():
            area = rect.intersected(clip)
        if area.width() <= 0 or area.height() <= 0:
            painter.restore()
            return
        painter.setClipRect(area)
        painter.setPen(QPen(QColor(color), 1))
        step = 10
        for offset in range(0, area.width() + area.height(), step):
            painter.drawLine(area.left() + offset, area.bottom(),
                             area.left() + offset - area.height(), area.top())
        painter.restore()

    def _draw_diamond(self, painter: QPainter, x: int, y: int,
                      color: QColor, filled: bool) -> None:
        size = 4
        polygon = QPolygon([
            QPoint(x, y - size),
            QPoint(x + size, y),
            QPoint(x, y + size),
            QPoint(x - size, y),
        ])
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(color) if filled else Qt.BrushStyle.NoBrush)
        painter.drawPolygon(polygon)

    def _draw_playhead(self, painter: QPainter) -> None:
        seconds = self._drag_seconds if self._drag == "playhead" else self._playhead
        if not self._seconds_in_view(seconds):
            return
        x = int(self._x_from_seconds(seconds))
        color = QColor(qcolor(current_mode(), "PRIMARY"))
        painter.setPen(QPen(color, 1))
        painter.drawLine(x, 12, x, self.BUBBLE_TOP - 2)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygon([
            QPoint(x - 4, 12), QPoint(x + 4, 12), QPoint(x, 18),
        ]))
        self._draw_playhead_bubble(painter, x, seconds)

    def _draw_playhead_bubble(self, painter: QPainter, x: int, seconds) -> None:
        """播放头下方的当前时间气泡（拖动播放头时看得最清楚）。"""
        from ...core.timebase import format_timecode

        mode = current_mode()
        text = format_timecode(seconds)
        font = QFont(FONT_MONO.split(",")[0].strip("'"))
        font.setPointSize(FONT_SIZE_CAPTION)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = min(self.BUBBLE_MAX_WIDTH, metrics.horizontalAdvance(text) + 12)
        # 贴着边缘时把气泡拉回可视区内（不然会画到控件外面）
        left = min(max(0, x - width // 2), max(0, self.width() - width))
        rect = QRect(left, self.BUBBLE_TOP, width, self.BUBBLE_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(qcolor(mode, "PRIMARY"))))
        painter.drawRoundedRect(rect, self.BUBBLE_RADIUS, self.BUBBLE_RADIUS)
        painter.setPen(QColor(qcolor(mode, "TEXT_INVERSE")))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.setFont(self.font())          # 画笔状态还给调用方

    def _draw_drag_preview(self, painter: QPainter) -> None:
        if not self._seconds_in_view(self._drag_seconds):
            return
        x = int(self._x_from_seconds(self._drag_seconds))
        pen = QPen(QColor(qcolor(current_mode(), "PRIMARY")), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(x, 12, x, self.BUBBLE_TOP - 2)
        if self._error_message:
            painter.setPen(QColor(qcolor(current_mode(), "DANGER")))
            painter.drawText(QRect(x + 6, 2, 320, 16),
                             Qt.AlignmentFlag.AlignLeft, self._error_message)

    def _playhead_rect(self) -> QRect | None:
        if self._media is None or self._total_seconds() <= 0:
            return None
        if not self._seconds_in_view(self._playhead):
            return None
        x = int(self._x_from_seconds(self._playhead))
        # 气泡比播放头宽得多：脏矩形要把它整个罩住，否则拖动会留下残影
        half = self.BUBBLE_MAX_WIDTH // 2 + 4
        return QRect(x - half, 0, half * 2, self.height())

    # ---- 交互 ----------------------------------------------------------

    def _hit_playhead(self, x: float) -> bool:
        if self._media is None or self._total_seconds() <= 0:
            return False
        if not self._seconds_in_view(self._playhead):
            return False
        return abs(x - self._x_from_seconds(self._playhead)) <= self.HIT_RADIUS

    def _hit_marker(self, x: float) -> tuple[int, Fraction, bool] | None:
        best = None
        for boundary_index, seconds, locked in self._markers:
            distance = abs(x - self._x_from_seconds(seconds))
            if distance <= self.HIT_RADIUS and (best is None or distance < best[0]):
                best = (distance, boundary_index, seconds, locked)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _episode_at(self, seconds: Fraction) -> int | None:
        for index, start, end in self._segments:
            if start <= seconds < end:
                return index
        return self._segments[-1][0] if self._segments else None

    @staticmethod
    def _event_button(event):
        """取事件按键；测试里的轻量事件没有 button() 时按左键处理。"""
        method = getattr(event, "button", None)
        return method() if callable(method) else Qt.MouseButton.LeftButton

    @staticmethod
    def _answer_event(event, accepted: bool) -> None:
        """给事件回执；测试里的轻量事件可能没有 accept() / ignore()。"""
        method = getattr(event, "accept" if accepted else "ignore", None)
        if callable(method):
            method()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._media is None or self._total_seconds() <= 0:
            return
        x = float(event.position().x())
        button = self._event_button(event)
        if button == Qt.MouseButton.MiddleButton:
            # 中键平移以按下瞬间为原点：拖到端点再往回拖能立即回退，
            # 不会因为逐帧夹取而丢失已经"多拖出去"的距离。
            self._drag = "pan"
            self._pan_origin_x = x
            self._pan_origin_start = self._view_start
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._answer_event(event, True)
            return
        if button != Qt.MouseButton.LeftButton:
            return
        marker = self._hit_marker(x)
        if self._hit_playhead(x):
            self._drag = "playhead"
            self._drag_seconds = self._playhead
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        if marker is not None:
            if marker[2]:
                # 已锁定：既不拖也不当作轨道点击，只提示怎么解锁
                QToolTip.showText(
                    event.globalPos(), "该切点已锁定，先解锁才能拖动。", self
                )
                return
            self._drag = "boundary"
            self._drag_index = marker[0]
            self._drag_seconds = marker[1]
            self.clear_error()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.update()
            return
        # 空白处：跳转并选中该集，随后继续按着拖即为拖动播放头
        seconds = self._seconds_from_x(x)
        self._drag = "playhead"
        self._drag_seconds = seconds
        self.episode_activated.emit(self._episode_at(seconds) or 0)
        self.seek_requested.emit(float(seconds))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        x = float(event.position().x())
        if self._drag == "pan":
            span = self.visible_span()
            delta = (
                self._as_fraction(self._pan_origin_x - x)
                / self._usable_width() * span
            )
            self._apply_view(self._pan_origin_start + delta, span)
            return
        if self._drag == "playhead":
            self._drag_seconds = self._seconds_from_x(x)
            now = time.monotonic()
            if now - self._last_emit >= self.DRAG_THROTTLE:
                self._last_emit = now
                # 拖动中发 scrub_requested 而不是 seek_requested：接收方据此走
                # **异步**取帧，不阻塞 GUI 线程。松手时才发 seek_requested 做
                # 精确落定（见 mouseReleaseEvent）。
                self.scrub_requested.emit(float(self._drag_seconds))
            self.update()
            return
        if self._drag == "boundary":
            self._drag_seconds = self._seconds_from_x(x)
            self.update()
            return
        self._update_hover(x, event.globalPos())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._drag == "pan":
            self._drag = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._answer_event(event, True)
            return
        if self._drag == "boundary" and self._plan is not None and self._media is not None:
            _, snapped = self._media.snap_to_frame(self._drag_seconds)
            ticks = self._plan.time_base.seconds_to_ticks(snapped)
            self._drag = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._drag_seconds = Fraction(0)
            self.boundary_move_requested.emit(self._drag_index, ticks)
            self.update()
            return
        if self._drag == "playhead":
            seconds = self._drag_seconds
            # 必须先清掉拖动标记再发信号：接收方（StageView.goto）会同步回写
            # set_playhead，而它在拖动期间是"忽略外部驱动"的护栏——顺序反了
            # 这次回写就会被吞掉，松手后播放头弹回原处（实测就是这个 bug）。
            self._drag = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._drag_seconds = Fraction(0)
            self.seek_requested.emit(float(seconds))
            self.update()
            return
        self._drag = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """滚轮缩放：普通滚轮与 Ctrl+滚轮同义，Shift+滚轮横向平移。

        缩放锚点取光标下的时间，用户会觉得"鼠标指着的那一帧没动"，
        比锚在播放头更符合滚轮的手感。
        """
        if self._media is None or self._total_seconds() <= 0:
            self._answer_event(event, False)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            self._answer_event(event, False)
            return
        # 按 1/120 格折算而不是取整：精密触控板一格会拆成多个十几像素的小
        # 事件，每来一个就按一整格缩放会让画面瞬间放大到底。
        notches = abs(delta) / 120.0
        x = float(event.position().x())
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            direction = 1 if delta < 0 else -1
            self.pan(
                self.visible_span() * self.WHEEL_PAN_FRACTION * notches * direction
            )
        else:
            factor = self.WHEEL_ZOOM_STEP ** notches
            if delta < 0:
                factor = 1 / factor
            self.zoom_by(factor, self._seconds_from_x(x))
        self._answer_event(event, True)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """双击集分段：放大到该集范围（两侧留 4% 余量）。"""
        if self._media is None or self._total_seconds() <= 0:
            return
        if self._event_button(event) != Qt.MouseButton.LeftButton:
            return
        x = float(event.position().x())
        index = self._episode_at(self._seconds_from_x(x))
        for episode_index, start, end in self._segments:
            if episode_index == index:
                self.zoom_to(start, end)
                self._answer_event(event, True)
                return

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        QToolTip.hideText()
        super().leaveEvent(event)

    def _update_hover(self, x: float, global_pos) -> None:
        marker = self._hit_marker(x)
        if marker is None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            QToolTip.hideText()
            return
        boundary_index, seconds, locked = marker
        if locked:
            self.setCursor(Qt.CursorShape.ForbiddenCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        from ...core.timebase import format_timecode

        parts = [f"第{boundary_index:02d}集结束 · {format_timecode(seconds)}"]
        if self._media is not None:
            frame = self._media.frame_index_at(seconds)
            parts.append(f"帧 #{frame}")
        if locked:
            parts.append("已锁定（先解锁再拖动）")
        if self._error_index == boundary_index and self._error_message:
            parts.append(self._error_message)
        QToolTip.showText(global_pos, "\n".join(parts), self)