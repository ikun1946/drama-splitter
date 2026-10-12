"""TimelineRuler —— 轻量时间轴（切点标记 + 播放头 + 拖拽，不做帧缩略图）。

为什么不做缩略图
----------------
1 小时 1080p 约 10.8 万帧，直读必卡，需要另建缩略图金字塔与缓存策略。本版
先给出"导航能力"：一眼看到所有切点在哪、锁定与否、当前在哪一集，并能直接
拖动切点。缩略图作为后续独立模块，不影响本控件的接口。

绘制分两层（性能要求）
----------------------
- **静态层**：轨道、集分段、切点标记、选中集、导出完成标记 —— 绘入缓存
  ``QPixmap``，键 =（尺寸, 主题模式, 方案结构, 选中集）。只有这四者变化才重建。
- **动态层**：播放头与拖拽预览线，直接画在缓存之上。

``set_playhead`` 只重绘新旧播放头所在的窄条。做成这样是因为 ``StreamPlayer.
position_changed`` 在播放时每帧都触发，若每帧重建整条时间轴，播放会掉帧。
``_static_rebuild_count`` 是给测试用的计数器（1000 次 set_playhead 后必须为 0）。

坐标与精度
----------
秒 ↔ 像素的换算全部走 :class:`fractions.Fraction`，不经过 float；拖动松手后
先用 ``media.snap_to_frame`` 吸附到帧起点，再由 ``plan.time_base`` 转成整数
ticks 发出。float 直转 ticks 会在长片尾部累积可见误差。
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

    MARGIN = 10                                  # 左右留白（放时长标注）
    TRACK_TOP = 30                               # 轨道上沿
    TRACK_HEIGHT = 28                            # 轨道高：20 偏扁，集号挤在中间看不清
    SEGMENT_GAP = 2                              # 分段之间的缝
    SEGMENT_RADIUS = 4                           # 分段圆角
    MARKER_OVERHANG = 7                          # 切点竖线超出轨道的高度
    HIT_RADIUS = 6                               # 命中半径（像素）
    DRAG_THROTTLE = 1 / 30                       # 拖动播放头的发信号节流

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(72)
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

        # 拖拽状态
        self._drag: str | None = None                # "playhead" / "boundary"
        self._drag_index = 0
        self._drag_seconds = Fraction(0)
        self._last_emit = 0.0

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
            return
        old = self._playhead_rect()
        self._playhead = position
        new = self._playhead_rect()
        if old is not None:
            self.update(old)
        if new is not None:
            self.update(new)

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
        ratio = Fraction(seconds) / total
        return self.MARGIN + float(ratio) * self._usable_width()

    def _seconds_from_x(self, x: float) -> Fraction:
        total = self._total_seconds()
        if total <= 0:
            return Fraction(0)
        ratio = Fraction(x - self.MARGIN) / self._usable_width()
        seconds = ratio * total
        return max(Fraction(0), min(seconds, total))

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
            current_mode(),
            self._structure_key(),
            self._selected,
            self._error_index,
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
        pixmap = QPixmap(self.size())
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

        # 集分段：左右各留一条缝并画圆角，相邻集在视觉上才分得开
        if self._segments:
            for position, (index, start, end) in enumerate(self._segments):
                x0 = self._x_from_seconds(start)
                x1 = self._x_from_seconds(end)
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
                if rect.width() >= 22:
                    painter.setPen(QColor(qcolor(mode, "TEXT_MUTED")))
                    font = QFont(FONT_MONO.split(",")[0].strip("'"))
                    font.setPointSize(FONT_SIZE_CAPTION)
                    painter.setFont(font)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(index))

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

        # 切点标记
        for boundary_index, seconds, locked in self._markers:
            x = int(self._x_from_seconds(seconds))
            if boundary_index == self._error_index:
                color = QColor(qcolor(mode, "DANGER"))
            else:
                color = QColor(qcolor(mode, "PRIMARY") if locked else qcolor(mode, "NEUTRAL"))
            painter.setPen(QPen(color, 2 if locked else 1))
            painter.drawLine(x, top - self.MARKER_OVERHANG, x,
                             top + height + self.MARKER_OVERHANG)
            self._draw_diamond(painter, x, top - self.MARKER_OVERHANG - 4,
                               color, filled=locked)

        # 两端时长标注
        #
        # 两端统一用 format_timecode（HH:MM:SS.mmm）——这是本项目时间码的规范
        # 格式，与预览 HUD、审核卡片一致。原先左端写死 "00:00.000"、右端是
        # "600.067s"，一边像时间码一边像秒表读数，看着像 bug（用户就是这么报的）。
        painter.setPen(QColor(qcolor(mode, "TEXT_MUTED")))
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
            painter.drawText(QRect(left, 2, right - left, 16),
                             Qt.AlignmentFlag.AlignCenter, "VFR 源 · 按时间吸附")
        painter.end()
        return pixmap

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
        """例外集斜纹：对角细线，稀疏到不与集号冲突。"""
        painter.save()
        painter.setClipRect(rect)
        painter.setPen(QPen(QColor(color), 1))
        step = 10
        for offset in range(0, rect.width() + rect.height(), step):
            painter.drawLine(rect.left() + offset, rect.bottom(),
                             rect.left() + offset - rect.height(), rect.top())
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
        x = int(self._x_from_seconds(seconds))
        painter.setPen(QPen(QColor(qcolor(current_mode(), "PRIMARY")), 1))
        painter.drawLine(x, 12, x, self.TRACK_TOP + self.TRACK_HEIGHT + 8)
        painter.setBrush(QBrush(QColor(qcolor(current_mode(), "PRIMARY"))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygon([
            QPoint(x - 4, 12), QPoint(x + 4, 12), QPoint(x, 18),
        ]))

    def _draw_drag_preview(self, painter: QPainter) -> None:
        x = int(self._x_from_seconds(self._drag_seconds))
        pen = QPen(QColor(qcolor(current_mode(), "PRIMARY")), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(x, 12, x, self.TRACK_TOP + self.TRACK_HEIGHT + 8)
        if self._error_message:
            painter.setPen(QColor(qcolor(current_mode(), "DANGER")))
            painter.drawText(QRect(x + 6, 2, 320, 16),
                             Qt.AlignmentFlag.AlignLeft, self._error_message)

    def _playhead_rect(self) -> QRect | None:
        if self._media is None or self._total_seconds() <= 0:
            return None
        x = int(self._x_from_seconds(self._playhead))
        return QRect(x - 6, 10, 13, self.TRACK_TOP + self.TRACK_HEIGHT + 10)

    # ---- 交互 ----------------------------------------------------------

    def _hit_playhead(self, x: float) -> bool:
        if self._media is None or self._total_seconds() <= 0:
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

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._media is None or self._total_seconds() <= 0:
            return
        x = float(event.position().x())
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