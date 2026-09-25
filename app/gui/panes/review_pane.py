"""左栏「分集审核」pane：集列表、当前集、切点操作。

只负责"看与点"：列表、信息、按钮。播放与定位在中栏（``StageView``），方案的
改动由工作台统一执行（``plan.move_boundary`` / ``lock_boundary`` 等的返回值需要
被翻译成检查器问题与时间轴标红，那是编排职责，不属面板）。

按钮发出请求信号而不是直接改模型，好处是"同一动作"无论来自按钮、时间轴拖拽
还是快捷键，都走工作台里同一条校验与反馈路径。
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.timebase import format_seconds_brief, format_timecode
from ..state import ProjectState
from ..widgets import Card, StatusPill

__all__ = ["ReviewPane"]


class ReviewPane(QWidget):
    episode_selected = Signal(int)         # 选中某集（1 起）
    boundary_set_requested = Signal(int)   # 把当前位置设为该集结束切点
    lock_requested = Signal(int, bool)     # (集号, 是否锁定)
    confirm_requested = Signal()
    snap_requested = Signal()

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.current_episode = 1
        self.position = Fraction(0)

        # ---- 集列表 ----
        # 列只放"窄栏里放得下"的信息：区间与审核状态在「当前集」卡片里都有，
        # 塞进 320px 的左栏只会被截断成半截时间码（实测 1440 宽下就被截了）。
        self.episode_table = QTableWidget(0, 3)
        self.episode_table.setHorizontalHeaderLabels(["集号", "时长", "切点"])
        header = self.episode_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.episode_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.episode_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.episode_table.setAlternatingRowColors(True)
        self.episode_table.setMinimumHeight(220)
        self.episode_table.itemSelectionChanged.connect(self._episode_selected)
        list_card = Card("集列表", parent=self)
        list_card.body_layout.addWidget(self.episode_table, 1)

        # ---- 当前集 ----
        self.card = QTextEdit()
        self.card.setReadOnly(True)
        self.card.setMinimumHeight(110)
        self._review_pill = StatusPill("未审核", tone="muted")
        card_card = Card("当前集", parent=self)
        pill_row = QHBoxLayout()
        pill_row.addWidget(self._review_pill)
        pill_row.addStretch(1)
        card_card.body_layout.addLayout(pill_row)
        card_card.body_layout.addWidget(self.card, 1)

        # ---- 切点操作 ----
        self.btn_set_boundary = QPushButton("把当前位置设为该集结束切点")
        self.btn_set_boundary.setProperty("role", "primary")
        self.btn_lock = QPushButton("🔒  锁定该切点")
        self.btn_unlock = QPushButton("🔓  解除锁定")
        self.btn_confirm = QPushButton("✔  确认该集")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_snap = QPushButton("吸附到最近帧")
        for button in (
            self.btn_set_boundary,
            self.btn_lock,
            self.btn_unlock,
            self.btn_confirm,
            self.btn_snap,
        ):
            button.setToolTip(button.text())
            # QSS 的 min-height 不进 minimumSizeHint：可用高度不足时按钮会被
            # 布局压成一条细线、文字互相叠印（04-审核 截图缺陷）。显式最小高度
            # 是硬保险。
            button.setMinimumHeight(32)
            button.style().unpolish(button)
            button.style().polish(button)

        self.btn_set_boundary.clicked.connect(
            lambda: self.boundary_set_requested.emit(self.current_episode)
        )
        self.btn_lock.clicked.connect(
            lambda: self.lock_requested.emit(self.current_episode, True)
        )
        self.btn_unlock.clicked.connect(
            lambda: self.lock_requested.emit(self.current_episode, False)
        )
        self.btn_confirm.clicked.connect(self.confirm_requested)
        self.btn_snap.clicked.connect(self.snap_requested)

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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(list_card, 1)
        outer.addWidget(card_card)
        outer.addWidget(ops_card)
        outer.addStretch(0)

    # ---- 刷新 ----------------------------------------------------------

    def refresh(self, *, keep_row: bool = True) -> None:
        plan = self.state.current_plan
        if plan is None:
            self.episode_table.setRowCount(0)
            self.card.setPlainText("")
            return

        episodes = plan.episodes()
        self.episode_table.blockSignals(True)
        self.episode_table.setRowCount(len(episodes))
        for row, episode in enumerate(episodes):
            status = (
                "例外"
                if episode.index in plan.exceptions
                else ("已锁定" if episode.end_ticks in plan.locked_ticks else "未锁定")
            )
            cells = [
                f"第{episode.index:02d}集",
                format_seconds_brief(episode.duration_seconds),
                status,
            ]
            for column, text in enumerate(cells):
                self.episode_table.setItem(row, column, QTableWidgetItem(text))
        self.episode_table.blockSignals(False)

        if keep_row and episodes:
            row = min(self.current_episode - 1, len(episodes) - 1)
            self.episode_table.selectRow(row)
        self._update_card()

    def set_current(self, index: int, *, select_row: bool = True) -> None:
        """外部（时间轴、检查器跳转）设定当前集。"""
        self.current_episode = index
        if select_row and self.episode_table.rowCount():
            row = max(0, min(index - 1, self.episode_table.rowCount() - 1))
            self.episode_table.selectRow(row)
        self._update_card()

    def select_episode(self, index: int) -> None:
        self.set_current(index)

    def set_position(self, position: Fraction) -> None:
        """当前位置来自中栏舞台（本面板不再自己持有播放器）。"""
        self.position = position
        self._update_card()

    # ---- 内部 ----------------------------------------------------------

    def _episode_selected(self) -> None:
        rows = self.episode_table.selectionModel().selectedRows()
        if not rows:
            return
        self.current_episode = rows[0].row() + 1
        self._update_card()
        self.episode_selected.emit(self.current_episode)

    def _update_card(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        episodes = plan.episodes() if plan else []
        if not episodes or not 1 <= self.current_episode <= len(episodes):
            self.card.setPlainText("")
            return
        episode = episodes[self.current_episode - 1]
        frame_counts = plan.frame_counts(media) if media else []
        count = frame_counts[episode.index - 1] if frame_counts else "—"
        first_frame = media.frame_index_at(episode.start_seconds) if media else 0
        locked = episode.end_ticks in plan.locked_ticks
        lines = [
            f"第{episode.index:02d}集｜{format_timecode(episode.start_seconds)}—"
            f"{format_timecode(episode.end_seconds)}｜"
            f"{format_seconds_brief(episode.duration_seconds)}",
            f"当前位置：{format_timecode(self.position)}",
            f"首帧号：{first_frame}　帧数：{count}",
            "",
            f"审核状态：{plan.semantic_review_status}",
            f"锁定状态：{'已锁定' if locked else '未锁定'}",
            "推荐等级：未评估（本版未接入剧情判断，不给出无法验证的推荐分）",
        ]
        self.card.setPlainText("\n".join(lines))

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