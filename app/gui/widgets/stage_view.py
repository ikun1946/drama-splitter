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
from ..frame_source import DecodeError, FrameDecoder, FrameScrubber, StreamPlayer
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
        self.scrubber = FrameScrubber(self.decoder)
        self.player = StreamPlayer()
        self.position = Fraction(0)
        self.current_episode = 1
        self._stitch_queue: list[tuple] = []
        # 当前绑定的媒体对象：换片（每次导入/打开项目都会产生新的 MediaInfo）
        # 时用来判断"播放头该不该回到片头"。用对象而不是路径，因为重新导入同一
        # 部片子也应当回到片头。
        self._bound_media: object | None = None

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
        # 「播放」键兼作「暂停」键：文案由 player.playing_changed 驱动
        self.preview.btn_play.clicked.connect(self.toggle_playback)
        self.preview.btn_boundary.clicked.connect(lambda: self.play_boundary())
        self.preview.btn_stitch.clicked.connect(lambda: self.play_stitched())
        self.preview.btn_prev_frame.clicked.connect(lambda: self.step_frames(-1))
        self.preview.btn_next_frame.clicked.connect(lambda: self.step_frames(1))
        self.preview.btn_back_1s.clicked.connect(lambda: self.step_seconds(-1.0))
        self.preview.btn_fwd_1s.clicked.connect(lambda: self.step_seconds(1.0))
        self.preview.btn_back_5s.clicked.connect(lambda: self.step_seconds(-5.0))
        self.preview.btn_fwd_5s.clicked.connect(lambda: self.step_seconds(5.0))

        self.player.frame_ready.connect(self.preview.set_image)
        self.player.position_changed.connect(self._on_position)
        self.player.playing_changed.connect(self.preview.set_playing)
        self.player.playback_finished.connect(self._on_playback_finished)
        self.player.error.connect(lambda text: message.warning(self, "播放失败", text))
        # 拖动取帧走后台线程：同步取一帧要 ~168ms，拖时间轴时会把 GUI 线程
        # 拖死（事件队列积压，"一拖就卡"）。这里只更新画面，播放头由拖动手势
        # 自己画（timeline 拖动中忽略外部 set_playhead）。
        self.scrubber.frame_ready.connect(self._on_scrub_frame)

        self.timeline.seek_requested.connect(self.goto)
        self.timeline.scrub_requested.connect(self._on_scrub)
        self.timeline.episode_activated.connect(self._on_timeline_episode)
        self.timeline.boundary_move_requested.connect(self.boundary_move_requested)

    # ---- 项目绑定 ------------------------------------------------------

    def set_project(self, selected_episode: int | None = None) -> None:
        """媒体或方案变化后重建预览与时间轴。"""
        media = self.state.media
        if selected_episode is not None:
            self.current_episode = selected_episode
        if media is None:
            self._bound_media = None
            self.player.stop()          # 换源为空时也要停掉旧片子的播放
            self.scrubber.stop()        # 后台取帧线程也要停，否则它还在读旧文件
            self.preview.clear("尚未选择源文件")
            self.timeline.set_project(None, None, {}, None)
            return
        if self._bound_media is not media:
            # 换了源片（每次导入/打开项目都是新的媒体对象）：播放头必须回到
            # 片头。否则新片子会在上一部片子的播放头位置起播，时间轴与预览
            # 也跟着停在那个位置。
            self._bound_media = media
            self._set_position(Fraction(0))
        video = media.video
        # 换源时停掉后台取帧线程：它持有旧 decoder，继续跑会读到旧文件。
        # 紧接着的 goto() 会用新源做一次同步取帧，画面不会空着。
        self.scrubber.stop()
        # 把已探测到的分辨率交给解码器：否则第一次取帧会在 GUI 线程里同步跑
        # 一次 ffprobe（大文件上可见卡顿）
        self.decoder.set_source(
            media.path, size=(video.width, video.height) if video else None
        )
        self.player.set_source(
            media.path, size=(video.width, video.height) if video else None
        )
        if video is not None:
            self.player.set_fps(video.nominal_fps)
        self.timeline.set_project(
            media, self.state.current_plan, self.state.exported, self.current_episode
        )
        self.timeline.set_playhead(self.position)
        if video is None:
            # 只有音轨的素材没有画面可解，如实说明而不是报"解码失败"
            self.preview.clear("源文件没有视频轨")
            return
        # 换源/重建后立刻把当前帧画出来。否则画面区还停在上一状态（导入后
        # 是「尚未选择源文件」的占位文字），要等用户点「下一帧」「−1s」或
        # 拖时间轴触发 goto() 才会出图——看起来像"导入后没有画面"。
        self.goto(self.position)

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
        # 落定前先丢弃拖动期间未完成的异步请求，否则它解完的旧帧会在这一帧
        # 之后才到，把精确落定的画面盖回拖动位置（表现为"松手后闪一下又跳回去"）。
        self.scrubber.cancel()
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

    def _on_scrub(self, seconds: float) -> None:
        """拖动时间轴中的高频回调（30Hz）：只请求后台取帧，绝不阻塞。

        这里**不**调 ``_set_position``：拖动手势由 TimelineRuler 自己画播放头，
        而 ``position_changed`` 会驱动左栏卡片重排文字，30Hz 重排纯属浪费。
        位置状态在松手（``goto``）时一次性落定。
        """
        media = self.state.media
        if media is None:
            return
        self.scrubber.request(seconds)

    def _on_scrub_frame(self, image, seconds: float) -> None:
        """后台解出的拖动帧：只更新画面与 HUD，不动播放头。"""
        media = self.state.media
        if media is None:
            return
        self.preview.set_frame_info(frame_index=media.frame_index_at(Fraction(seconds)))
        self.preview.set_image(image)
        self.preview.timecode.setText(
            f"{format_timecode(Fraction(seconds))}　（{seconds:.3f}s）"
        )

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

    def _play(self) -> None:
        """起播前把音频通道的参数同步给播放器，然后播放。

        左栏换了主对白轨、或换成没音轨的片子，都只改 ``state``（没有别的刷新
        时机），所以每次播放都从这里读一次，避免"选了新音轨却还放旧轨/没声音"。
        成片连看走的是另一条路（成片是单音轨导出，见 ``_play_next_stitch``）。
        """
        media = self.state.media
        self.scrubber.cancel()   # 起播后画面由 StreamPlayer 接管，拖动请求作废
        self.player.set_audio_track(self.state.audio_stream_index)
        self.player.set_audio_enabled(bool(media.audio_tracks) if media else False)
        self.player.play()

    def toggle_playback(self) -> None:
        """「播放」键兼作「暂停」键（按钮文案随状态变）。

        播放中 → 暂停，播放头停在原处；未播放 → 从**当前播放头**接着播，
        直到所在集的片尾为止。暂停后再点不跳回集首——把播放与暂停合并成一
        个键后，这是唯一说得通的预期；要回集首用「上一帧」或拖时间轴。
        """
        if self.player.is_playing:
            self.player.pause()
            return
        for episode in self._episodes():
            if episode.start_seconds <= self.position < episode.end_seconds:
                self.player.set_range(float(self.position), float(episode.end_seconds))
                self._play()
                return
        # 没有方案（就绪阶段，整片连播）或播放头不在任何一集内（例如停在片尾）：
        # 交给 play_episode 决定范围（整片从播放头起 / 当前集从集首起）
        self.play_episode()

    def play_episode(self, index: int | None = None) -> None:
        episodes = self._episodes()
        if not episodes:
            # 还没有方案（就绪阶段）：播放整片，便于先看素材
            self.player.set_range(float(self.position), None)
            self._play()
            return
        index = index or self.current_episode
        if not 1 <= index <= len(episodes):
            return
        episode = episodes[index - 1]
        self.player.set_range(float(episode.start_seconds), float(episode.end_seconds))
        self._play()

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
        self._play()

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
        # 成片是单音轨导出：不让它按源片的流序号挑轨（序号对不上会直接没声音），
        # 交给 ffplay 自己取第一条音轨
        self.player.set_audio_track(None)
        self.player.set_audio_enabled(True)
        self.player.play()

    def _on_playback_finished(self) -> None:
        if self._stitch_queue:
            self._play_next_stitch()
        else:
            # 回到整片源，避免下次播放还停在成片上（带上已知分辨率，
            # 否则下一次播放又要在 GUI 线程里同步跑一次 ffprobe）
            media = self.state.media
            if media is not None:
                video = media.video
                self.player.set_source(
                    media.path, size=(video.width, video.height) if video else None
                )