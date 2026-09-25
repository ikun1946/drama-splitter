"""中栏舞台：帧级预览 + 时间轴 + 播放控制。

单页工作台里中栏只有三屏：空态（拖放 hero）、舞台（预览 + 时间轴）、分析进度。
本控件是"舞台"那一屏：所有与播放/定位相关的状态（解码器、播放器、当前位置、
选中集）都收敛在这里，左栏的审核 pane 只负责列表与操作按钮，不再各持一份。

信号
----
- ``position_changed(Fraction)``      播放头位置变化（左栏「当前位置」据此刷新）
- ``episode_activated(int)``          用户点了时间轴某一段
- ``boundary_move_requested(int,int)`` 时间轴拖拽切点松手（(边界下标, ticks)）
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...core.timebase import format_timecode
from ..frame_source import DecodeError, FrameDecoder, StreamPlayer
from ..state import ProjectState
from . import message
from .media_preview import MediaPreview
from .timeline import TimelineRuler

__all__ = ["StageView"]


class StageView(QWidget):
    position_changed = Signal(object)          # Fraction
    episode_activated = Signal(int)
    boundary_move_requested = Signal(int, int)

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.decoder = FrameDecoder()
        self.player = StreamPlayer()
        self.position = Fraction(0)
        self.current_episode = 1
        self._stitch_queue: list[tuple] = []

        self.preview = MediaPreview()
        # 中栏是自适应宽度，预览给一个能看清切点画面的最小高度
        self.preview.view.setMinimumHeight(240)
        self.timeline = TimelineRuler()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.timeline)

        # ---- 播放控制接线（原先散在审核页，现统一在这里） ----
        self.preview.btn_play.clicked.connect(lambda: self.play_episode())
        self.preview.btn_boundary.clicked.connect(lambda: self.play_boundary())
        self.preview.btn_stitch.clicked.connect(lambda: self.play_stitched())
        self.preview.btn_stop.clicked.connect(self.player.stop)
        self.preview.btn_prev_frame.clicked.connect(lambda: self.step_frames(-1))
        self.preview.btn_next_frame.clicked.connect(lambda: self.step_frames(1))
        self.preview.btn_back_1s.clicked.connect(lambda: self.step_seconds(-1.0))
        self.preview.btn_fwd_1s.clicked.connect(lambda: self.step_seconds(1.0))
        self.preview.btn_back_5s.clicked.connect(lambda: self.step_seconds(-5.0))
        self.preview.btn_fwd_5s.clicked.connect(lambda: self.step_seconds(5.0))

        self.player.frame_ready.connect(self.preview.set_image)
        self.player.position_changed.connect(self._on_position)
        self.player.playback_finished.connect(self._on_playback_finished)
        self.player.error.connect(lambda text: message.warning(self, "播放失败", text))

        self.timeline.seek_requested.connect(self.goto)
        self.timeline.episode_activated.connect(self._on_timeline_episode)
        self.timeline.boundary_move_requested.connect(self.boundary_move_requested)

    # ---- 项目绑定 ------------------------------------------------------

    def set_project(self, selected_episode: int | None = None) -> None:
        """媒体或方案变化后重建预览与时间轴。"""
        media = self.state.media
        if selected_episode is not None:
            self.current_episode = selected_episode
        if media is None:
            self.preview.clear("尚未选择源文件")
            self.timeline.set_project(None, None, {}, None)
            return
        self.decoder.set_source(media.path)
        self.player.set_source(media.path)
        if media.video is not None:
            self.player.set_fps(media.video.nominal_fps)
        self.timeline.set_project(
            media, self.state.current_plan, self.state.exported, self.current_episode
        )
        self.timeline.set_playhead(self.position)

    def refresh_theme(self) -> None:
        """主题切换后自绘控件需要重绘（时间轴静态层带主题色）。"""
        self.timeline.invalidate_static()
        self.preview.update()

    def set_transport_enabled(self, enabled: bool) -> None:
        for button in (
            self.preview.btn_play,
            self.preview.btn_boundary,
            self.preview.btn_stitch,
            self.preview.btn_prev_frame,
            self.preview.btn_next_frame,
            self.preview.btn_back_1s,
            self.preview.btn_fwd_1s,
            self.preview.btn_back_5s,
            self.preview.btn_fwd_5s,
            self.preview.btn_stop,
        ):
            button.setEnabled(enabled)
        self.timeline.setEnabled(enabled)

    def set_selected_episode(self, index: int) -> None:
        self.current_episode = index
        self.timeline.set_selected_episode(index)

    # ---- 定位 ----------------------------------------------------------

    def goto(self, seconds) -> None:
        media = self.state.media
        if media is None:
            return
        target = Fraction(seconds).limit_denominator(1_000_000)
        target = max(Fraction(0), min(target, media.timeline_duration))
        _, snapped = media.snap_to_frame(target)
        self._set_position(snapped)
        self.player.set_range(float(snapped))
        try:
            image = self.decoder.frame_at(snapped)
        except DecodeError as exc:
            self.preview.clear(f"解码失败：{exc}")
            return
        if image is not None:
            self.preview.set_frame_info(frame_index=media.frame_index_at(snapped))
            self.preview.set_image(image)

    def step_frames(self, delta: int) -> None:
        media = self.state.media
        if media is None:
            return
        index = max(0, media.frame_index_at(self.position) + delta)
        self.goto(media.frame_start_seconds(index))

    def step_seconds(self, delta: float) -> None:
        self.goto(self.position + Fraction(str(delta)))

    def _set_position(self, position: Fraction) -> None:
        self.position = position
        self.preview.timecode.setText(
            f"{format_timecode(position)}　（{float(position):.3f}s）"
        )
        self.timeline.set_playhead(position)
        self.position_changed.emit(position)

    def _on_position(self, seconds: float) -> None:
        # 播放中每帧都来，只更新轻量显示（timeline.set_playhead 有缓存，不重建静态层）
        self._set_position(Fraction(seconds).limit_denominator(1_000_000))

    def _on_timeline_episode(self, index: int) -> None:
        if index:
            self.current_episode = index
            self.timeline.set_selected_episode(index)
        self.episode_activated.emit(index)

    # ---- 播放 ----------------------------------------------------------

    def _episodes(self):
        plan = self.state.current_plan
        return plan.episodes() if plan else []

    def play_episode(self, index: int | None = None) -> None:
        episodes = self._episodes()
        if not episodes:
            # 还没有方案（就绪阶段）：播放整片，便于先看素材
            self.player.set_range(float(self.position), None)
            self.player.play()
            return
        index = index or self.current_episode
        if not 1 <= index <= len(episodes):
            return
        episode = episodes[index - 1]
        self.player.set_range(float(episode.start_seconds), float(episode.end_seconds))
        self.player.play()

    def play_boundary(self, index: int | None = None) -> None:
        episodes = self._episodes()
        index = index or self.current_episode
        if not 1 <= index <= len(episodes):
            return
        if index >= len(episodes):
            message.information(self, "末集", "末集终点即片尾，没有后续集可连看。")
            return
        boundary = episodes[index - 1].end_seconds
        self.player.set_range(float(max(Fraction(0), boundary - 3)), float(boundary + 3))
        self.player.play()

    def play_stitched(self, index: int | None = None) -> None:
        """用**已导出的成片**连看相邻两集（能发现编码层面的衔接问题）。"""
        episodes = self._episodes()
        index = index or self.current_episode
        if not 1 <= index <= len(episodes):
            return
        if index >= len(episodes):
            message.information(self, "末集", "末集没有后续集。")
            return
        current, following = episodes[index - 1], episodes[index]
        first = self.state.exported.get(current.index)
        second = self.state.exported.get(following.index)
        if not first or not second:
            message.information(
                self,
                "尚无成片",
                "成片连看需要相邻两集都已导出。请先完成导出，或使用「播放切点前后」检视规划。",
            )
            return
        self._stitch_queue = [
            (first[0], float(current.end_seconds) - 3.0, float(current.end_seconds)),
            (second[0], 0.0, 3.0),
        ]
        self._play_next_stitch()

    def _play_next_stitch(self) -> None:
        if not self._stitch_queue:
            return
        path, start, end = self._stitch_queue.pop(0)
        self.player.set_source(path)
        self.player.set_range(max(0.0, start), end)
        self.player.play()

    def _on_playback_finished(self) -> None:
        if self._stitch_queue:
            self._play_next_stitch()
        else:
            # 回到整片源，避免下次播放还停在成片上
            media = self.state.media
            if media is not None:
                self.player.set_source(media.path)