"""页 4 审核：逐集复核切点，支持手动移动、锁定与例外标记。"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.timebase import format_seconds_brief, format_timecode
from ..frame_source import DecodeError, FrameDecoder, StreamPlayer
from ..state import ProjectState
from ..widgets import (
    Card,
    MediaPreview,
    StatusPill,
)
from ._common import scrollable


__all__ = ["ReviewPage"]


class ReviewPage(QWidget):
    """审核与手动微调（§13.2、§13.3）。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._decoder = FrameDecoder()
        self._player = StreamPlayer()
        self._position = Fraction(0)
        self._current_episode = 1
        self._stitch_queue: list[tuple[Path, float, float]] = []

        # ---- 集列表 ----
        self.episode_table = QTableWidget(0, 5)
        self.episode_table.setHorizontalHeaderLabels(
            ["集号", "区间", "时长", "切点", "审核"]
        )
        header = self.episode_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.episode_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.episode_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.episode_table.setAlternatingRowColors(True)
        self.episode_table.setMinimumWidth(480)
        self.episode_table.itemSelectionChanged.connect(self._episode_selected)

        list_card = Card("集列表", parent=self)
        list_card.body_layout.addWidget(self.episode_table, 1)

        # ---- 预览 ----
        self.preview = MediaPreview()
        # 审核页右列要同时放下预览 / 当前集 / 切点操作 / 方案层校验四张卡，
        # 预览最小高度给 190，900px 高的窗口即可完整展示（画面按比例缩放）
        self.preview.view.setMinimumHeight(190)

        # ---- 当前集卡片 ----
        self.card = QTextEdit()
        self.card.setReadOnly(True)
        # 96px 起步即可：QTextEdit 自带滚动，最小高度给小些能让右列在
        # 900px 高的窗口里完整放下（含切点操作卡），不出纵向滚动条
        self.card.setMinimumHeight(96)

        self._review_pill = StatusPill("未审核", tone="muted")

        card_card = Card("当前集", parent=self)
        pill_row = QHBoxLayout()
        pill_row.addWidget(self._review_pill)
        pill_row.addStretch(1)
        card_card.body_layout.insertLayout(0, pill_row)
        card_card.body_layout.addWidget(self.card, 1)

        # ---- 方案层校验 ----
        self.problem_box = QTextEdit()
        self.problem_box.setReadOnly(True)
        self.problem_box.setMinimumHeight(56)
        self._problem_pill = StatusPill("无问题", tone="success")

        problem_card = Card("方案层校验", parent=self)
        pill_row2 = QHBoxLayout()
        pill_row2.addWidget(QLabel("硬约束问题会阻止导出"))
        pill_row2.addStretch(1)
        pill_row2.addWidget(self._problem_pill)
        problem_card.body_layout.insertLayout(0, pill_row2)
        problem_card.body_layout.addWidget(self.problem_box, 1)

        # ---- 切点操作 ----
        self.btn_set_boundary = QPushButton("把当前位置设为该集结束切点")
        self.btn_set_boundary.setProperty("role", "primary")
        self.btn_set_boundary.style().unpolish(self.btn_set_boundary)
        self.btn_set_boundary.style().polish(self.btn_set_boundary)
        self.btn_lock = QPushButton("🔒  锁定该切点")
        self.btn_unlock = QPushButton("🔓  解除锁定")
        self.btn_confirm = QPushButton("✔  确认该集")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_confirm.style().unpolish(self.btn_confirm)
        self.btn_confirm.style().polish(self.btn_confirm)
        self.btn_snap = QPushButton("吸附到最近帧")

        for w in (
            self.btn_set_boundary,
            self.btn_lock,
            self.btn_unlock,
            self.btn_confirm,
            self.btn_snap,
        ):
            w.setToolTip(w.text())
            # QSS 的 min-height 不进 minimumSizeHint：可用高度不足时按钮会被
            # 布局压成一条细线、文字互相叠印（docs/screenshots 04-审核 缺陷）。
            # 显式最小高度 + 两列网格 + 右列滚动容器三重保险，任何窗口尺寸下
            # 按钮都保持可读。
            w.setMinimumHeight(32)

        self.btn_set_boundary.clicked.connect(self._set_boundary)
        self.btn_lock.clicked.connect(lambda: self._lock(True))
        self.btn_unlock.clicked.connect(lambda: self._lock(False))
        self.btn_confirm.clicked.connect(self._confirm_episode)
        self.btn_snap.clicked.connect(lambda: self._goto(self._position))

        ops_card = Card("切点操作", parent=self)
        ops_grid = QGridLayout()
        ops_grid.setContentsMargins(0, 0, 0, 0)
        ops_grid.setHorizontalSpacing(8)
        ops_grid.setVerticalSpacing(8)
        ops_grid.addWidget(self.btn_set_boundary, 0, 0, 1, 2)
        ops_grid.addWidget(self.btn_lock, 1, 0)
        ops_grid.addWidget(self.btn_unlock, 1, 1)
        ops_grid.addWidget(self.btn_confirm, 2, 0)
        ops_grid.addWidget(self.btn_snap, 2, 1)
        ops_card.body_layout.addLayout(ops_grid)

        # 右侧：preview + 当前集 + 切点操作 + 方案层校验
        right_col = QVBoxLayout()
        right_col.setSpacing(10)
        right_col.addWidget(self.preview, 2)
        right_col.addWidget(card_card, 1)
        right_col.addWidget(ops_card)
        right_col.addWidget(problem_card, 1)

        right_widget = QWidget()
        right_widget.setLayout(right_col)
        # 右列最小高度之和可能超过可用高度（同工作台 §13.1 的问题），
        # 套一层滚动容器：空间不够时出滚动条，而不是把按钮压扁
        right_holder = scrollable(right_widget)

        # 装配
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_widget = QWidget()
        left_widget.setLayout(QVBoxLayout())
        left_widget.layout().setContentsMargins(0, 0, 0, 0)
        left_widget.layout().addWidget(list_card)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_holder)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 920])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addWidget(splitter)

        # 播放控制接线
        self.preview.btn_play.clicked.connect(self._play_episode)
        self.preview.btn_boundary.clicked.connect(self._play_boundary)
        self.preview.btn_stitch.clicked.connect(self._play_stitched)
        self.preview.btn_stop.clicked.connect(self._player.stop)
        self.preview.btn_prev_frame.clicked.connect(lambda: self._step_frames(-1))
        self.preview.btn_next_frame.clicked.connect(lambda: self._step_frames(1))
        self.preview.btn_back_1s.clicked.connect(lambda: self._step_seconds(-1.0))
        self.preview.btn_fwd_1s.clicked.connect(lambda: self._step_seconds(1.0))
        self.preview.btn_back_5s.clicked.connect(lambda: self._step_seconds(-5.0))
        self.preview.btn_fwd_5s.clicked.connect(lambda: self._step_seconds(5.0))

        self._player.frame_ready.connect(self.preview.set_image)
        self._player.position_changed.connect(self._on_position)
        self._player.playback_finished.connect(self._on_playback_finished)
        self._player.error.connect(lambda message: QMessageBox.warning(self, "播放失败", message))

    # ---- 刷新 ----------------------------------------------------------

    def refresh(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None:
            self.episode_table.setRowCount(0)
            self.preview.clear()
            return

        self._decoder.set_source(media.path)
        self._player.set_source(media.path)
        self._player.set_fps(media.video.nominal_fps if media.video else Fraction(25))

        episodes = plan.episodes()
        self.episode_table.setRowCount(len(episodes))
        for row, episode in enumerate(episodes):
            status = (
                "例外"
                if episode.index in plan.exceptions
                else ("已锁定" if episode.end_ticks in plan.locked_ticks else "未锁定")
            )
            review_text = {
                "pending": "未审核",
                "confirmed": "已确认",
            }.get(plan.semantic_review_status, "草案未审核")
            cells = [
                f"第{episode.index:02d}集",
                f"{format_timecode(episode.start_seconds)} → {format_timecode(episode.end_seconds)}",
                format_seconds_brief(episode.duration_seconds),
                status,
                review_text,
            ]
            for column, text in enumerate(cells):
                self.episode_table.setItem(row, column, QTableWidgetItem(text))

        if episodes:
            row = min(self._current_episode - 1, len(episodes) - 1)
            self.episode_table.selectRow(row)

        self._refresh_problems()
        self._goto(episodes[0].start_seconds)

    def _refresh_problems(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            self.problem_box.setPlainText("")
            self._problem_pill.set_text("无方案")
            self._problem_pill.set_tone("muted")
            return
        problems = plan.validate(self.state.settings, None)
        if not problems:
            self.problem_box.setPlainText("方案层校验：无问题。")
            self._problem_pill.set_text("无问题")
            self._problem_pill.set_tone("success")
            return
        blocking = sum(1 for p in problems if p.is_blocking)
        warning = len(problems) - blocking
        if blocking:
            self._problem_pill.set_text(f"{blocking} 阻断")
            self._problem_pill.set_tone("danger")
        elif warning:
            self._problem_pill.set_text(f"{warning} 警告")
            self._problem_pill.set_tone("warn")
        lines = []
        for problem in problems:
            tag = "阻断" if problem.is_blocking else "警告"
            lines.append(f"[{tag}] {problem.describe()}")
        self.problem_box.setPlainText("\n".join(lines))

    # ---- 导航与定位 ----------------------------------------------------

    def _episode_selected(self) -> None:
        rows = self.episode_table.selectionModel().selectedRows()
        if not rows:
            return
        self._current_episode = rows[0].row() + 1
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if 1 <= self._current_episode <= len(episodes):
            episode = episodes[self._current_episode - 1]
            self._goto(episode.start_seconds)

    def _goto(self, seconds) -> None:
        media = self.state.media
        if media is None:
            return
        seconds = Fraction(seconds)
        seconds = max(Fraction(0), min(seconds, media.timeline_duration))
        _, snapped = media.snap_to_frame(seconds)
        self._position = snapped
        self._player.set_range(float(snapped))
        self.preview.timecode.setText(
            f"{format_timecode(self._position)}　（{float(self._position):.3f}s）"
        )
        try:
            image = self._decoder.frame_at(snapped)
        except DecodeError as exc:
            self.preview.clear(f"解码失败：{exc}")
            return
        if image is not None:
            # 把帧号注入 MediaPreview 的 HUD
            frame_index = media.frame_index_at(snapped)
            self.preview.set_frame_info(frame_index=frame_index)
            self.preview.set_image(image)
        self._update_card()

    def _on_position(self, seconds: float) -> None:
        self._position = Fraction(seconds).limit_denominator(100000)
        self.preview.timecode.setText(
            f"{format_timecode(self._position)}　（{float(self._position):.3f}s）"
        )

    # ---- 播放 ----------------------------------------------------------

    def _play_episode(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if not 1 <= self._current_episode <= len(episodes):
            return
        episode = episodes[self._current_episode - 1]
        self._player.set_range(float(episode.start_seconds), float(episode.end_seconds))
        self._player.play()

    def _play_boundary(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if not 1 <= self._current_episode <= len(episodes):
            return
        boundary = episodes[self._current_episode - 1].end_seconds
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集终点即片尾，没有后续集可连看。")
            return
        start = max(Fraction(0), boundary - 3)
        self._player.set_range(float(start), float(boundary + 3))
        self._player.play()

    def _play_stitched(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集没有后续集。")
            return
        current, following = episodes[self._current_episode - 1], episodes[self._current_episode]

        first = self.state.exported.get(current.index)
        second = self.state.exported.get(following.index)
        if not first or not second:
            QMessageBox.information(
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
        self._player.set_source(path)
        self._player.set_range(max(0.0, start), end)
        self._player.play()

    def _on_playback_finished(self) -> None:
        if self._stitch_queue:
            self._play_next_stitch()

    # ---- 微调 ----------------------------------------------------------

    def _step_frames(self, delta: int) -> None:
        media = self.state.media
        if media is None:
            return
        index = media.frame_index_at(self._position) + delta
        index = max(0, index)
        self._goto(media.frame_start_seconds(index))

    def _step_seconds(self, delta: float) -> None:
        self._goto(self._position + Fraction(str(delta)))

    def _set_boundary(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None:
            return
        episodes = plan.episodes()
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集终点为片尾，不可移动。")
            return

        index = self._current_episode
        ticks = plan.time_base.seconds_to_ticks(self._position)
        problems = plan.move_boundary(index, ticks)
        blocking = [p for p in problems if p.is_blocking]

        self._refresh_problems()
        if blocking:
            QMessageBox.warning(
                self,
                "该切点不可提交",
                "；".join(p.describe() for p in blocking) + "\n\n方案未被修改。",
            )
        else:
            self.refresh()
            self.episode_table.selectRow(self._current_episode - 1)

    def _lock(self, locked: bool) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        index = self._current_episode
        plan.lock_boundary(index, locked)
        self.refresh()
        self.episode_table.selectRow(max(0, self._current_episode - 1))

    def _confirm_episode(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        plan.semantic_review_status = "confirmed"
        self._review_pill.set_text("已确认")
        self._review_pill.set_tone("success")
        self.refresh()
        self.episode_table.selectRow(max(0, self._current_episode - 1))

    def _update_card(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        episodes = plan.episodes() if plan else []
        if not episodes or not 1 <= self._current_episode <= len(episodes):
            self.card.setPlainText("")
            return
        episode = episodes[self._current_episode - 1]
        start = float(episode.start_seconds)
        index = media.frame_index_at(episode.start_seconds) if media else 0
        lines = [
            f"第{episode.index:02d}集｜{format_timecode(episode.start_seconds)}—"
            f"{format_timecode(episode.end_seconds)}｜"
            f"{format_seconds_brief(episode.duration_seconds)}",
            f"当前位置：{format_timecode(self._position)}",
            f"首帧号：{index}　帧数：{plan.frame_counts(media)[episode.index - 1] if plan and media else '—'}",
            "",
            "推荐等级：未评估（本版尚未接入剧情判断，不给出无法验证的推荐分）",
            "支持证据：仅有时长与帧边界合规；对白与动作落点未评估。",
            "风险：对白截断、动作未完成、悬念落点均未评估。",
            f"审核状态：{plan.semantic_review_status}",
            f"锁定状态：{'已锁定' if episode.end_ticks in plan.locked_ticks else '未锁定'}",
            "",
            f"起点相对秒：{start:.6f}",
        ]
        self.card.setPlainText("\n".join(lines))

        # 同步审核状态徽章
        if plan.semantic_review_status == "confirmed":
            self._review_pill.set_text("已确认")
            self._review_pill.set_tone("success")
        else:
            self._review_pill.set_text("未审核")
            self._review_pill.set_tone("muted")

    def can_continue(self) -> tuple[bool, str]:
        plan = self.state.current_plan
        if plan is None:
            return False, "尚无方案。"
        blocking = [p for p in plan.validate() if p.is_blocking]
        if blocking:
            return False, "方案存在未解决的硬约束问题：" + blocking[0].describe()
        return True, ""
