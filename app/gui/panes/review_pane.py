"""左栏「分集审核」pane：集列表、当前集、切点操作。

只负责"看与点"：列表、信息、按钮。播放与定位在中栏（``StageView``），方案的
改动由工作台统一执行（``plan.move_boundary`` / ``lock_boundary`` 等的返回值需要
被翻译成检查器问题与时间轴标红，那是编排职责，不属面板）。

按钮发出请求信号而不是直接改模型，好处是"同一动作"无论来自按钮、时间轴拖拽
还是快捷键，都走工作台里同一条校验与反馈路径。

版式：
- 锁定与解除是**同一个开关**的两面（二者互斥，并排摆两个常驻按钮等于让用户
  每次先判断题）。只留 ``btn_lock``，文案随当前集状态切换；
- 四个动作排 2×2（原先 1+2+2 挤成三行，右列总有一个空位）；
- 「当前集」用四行结构化文本（集号 / 区间 / 时长+来源 / 审核胶囊），原先是一个
  7 行只读 ``QTextEdit``：推荐等级恒为常量、锁定态与集列表重复，当前位置中栏已有。
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.timebase import format_seconds_brief, format_timecode
from ..state import ProjectState
from ..theme import SPACE_SM, current_mode, qcolor
from ..widgets import Card, StatusPill

__all__ = ["ReviewPane"]

#: 集列表「切点」列的窄状态点：锁定实心 / 正常空心 / 例外三角。
#: 用字符而不是新增 QSS role：一列只放得下一个点，颜色由 item 前景色承担，
#: 深浅两套主题都从 ``theme.qcolor`` 取，不引入第二份色值。
_DOT_TEXT = {"locked": "●", "unlocked": "○", "exception": "▲"}
_DOT_COLOR = {"locked": "PRIMARY", "unlocked": "TEXT_MUTED", "exception": "WARN"}
_DOT_HINT = {
    "locked": "切点已锁定，不可拖动",
    "unlocked": "切点未锁定",
    "exception": "该集有例外记录",
}

#: 切点来源（``BoundaryPlan.source``）的中文说法。
_SOURCE_TEXT = {"auto": "自动候选", "manual": "手动调整"}


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
        # 320px 的左栏里，三列按内容宽度排会超出栏宽 → 出横向滚动条 → 整表被裁。
        # 全部列按比例拉伸，并关掉横向滚动条：宁可列窄一点，也不能让用户横向拖。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.episode_table.horizontalHeader().setMinimumSectionSize(48)
        self.episode_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.episode_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.episode_table.verticalHeader().setVisible(False)
        self.episode_table.verticalHeader().setDefaultSectionSize(30)
        self.episode_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.episode_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.episode_table.setAlternatingRowColors(True)
        self.episode_table.setMinimumHeight(220)
        self.episode_table.itemSelectionChanged.connect(self._episode_selected)
        list_card = Card("集列表", parent=self)
        list_card.body_layout.addWidget(self.episode_table, 1)

        # ---- 当前集：四行结构化文本 ----
        self.episode_title = QLabel("—", self)
        self.episode_title.setProperty("role", "title")
        self.episode_range = QLabel("—", self)
        self.episode_range.setProperty("role", "mono")
        # 等宽区间串 255px 宽，而卡片正文只有 276px：允许换行是为了把**最小宽度**
        # 压下来（不换行的 QLabel 最小宽度就是整串文本宽度，栏窄一点就顶宽被裁）。
        self.episode_range.setWordWrap(True)
        self.episode_meta = QLabel("—", self)
        self.episode_meta.setProperty("role", "muted")
        self.episode_meta.setWordWrap(True)
        self._review_pill = StatusPill("未审核", tone="muted")

        # 集号与区间同属"这是哪一集"，但**不能并排**：等宽字体下
        # 「00:12:34–00:18:56」就占 255px，加 15pt 的集号是 343px，
        # 超过 340 栏宽扣掉卡片内边距后的 276px —— 并排会顶宽、被静默裁切。
        # 上下两行仍是同一块头部信息，只是排成两行。
        head_box = QVBoxLayout()
        head_box.setContentsMargins(0, 0, 0, 0)
        head_box.setSpacing(0)
        head_box.addWidget(self.episode_title)
        head_box.addWidget(self.episode_range)

        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.addWidget(self._review_pill)
        pill_row.addStretch(1)

        card_card = Card("当前集", parent=self)
        card_card.body_layout.addLayout(head_box)
        card_card.body_layout.addWidget(self.episode_meta)
        card_card.body_layout.addLayout(pill_row)

        # ---- 切点操作（2×2）----
        # 文案按左栏 320px 的实际预算裁过：两列并排的那一行放不下原来的
        # 「🔒 锁定该切点 / 🔓 解除锁定」（最小宽度合计 331 > 视口 310），
        # 整张卡会被撑到 397 然后**静默裁切**——用户看到的是按钮被切掉半截。
        # 完整说法放进 tooltip（下面统一设置），短文案只保证排得下。
        #
        # 锁定 / 解除是互斥的两个方向，合成一个开关：文案随当前集切换，
        # 点击时按**当前**状态取反后照原样发 ``lock_requested``。
        self.btn_set_boundary = QPushButton("设为切点")
        self.btn_set_boundary.setProperty("role", "primary")
        self.btn_lock = QPushButton("锁定")
        self.btn_confirm = QPushButton("确认该集")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_snap = QPushButton("吸附到帧")

        #: 短文案 → 完整说法。窄栏里放不下全称，tooltip 补上。
        _hints = {
            "btn_set_boundary": "把当前位置设为该集结束切点",
            "btn_confirm": "确认该集已审核通过",
            "btn_snap": "把播放头吸附到最近帧",
        }
        for name, button in (
            ("btn_set_boundary", self.btn_set_boundary),
            ("btn_lock", self.btn_lock),
            ("btn_confirm", self.btn_confirm),
            ("btn_snap", self.btn_snap),
        ):
            if name in _hints:
                button.setToolTip(_hints[name])
            # QSS 的 min-height 不进 minimumSizeHint：可用高度不足时按钮会被
            # 布局压成一条细线、文字互相叠印（04-审核 截图缺陷）。显式最小高度
            # 是硬保险。
            button.setMinimumHeight(32)
            button.style().unpolish(button)
            button.style().polish(button)

        self.btn_set_boundary.clicked.connect(
            lambda: self.boundary_set_requested.emit(self.current_episode)
        )
        self.btn_lock.clicked.connect(self._toggle_lock)
        self.btn_confirm.clicked.connect(self.confirm_requested)
        self.btn_snap.clicked.connect(self.snap_requested)

        ops_card = Card("切点操作", parent=self, accent="warn")
        ops_grid = QGridLayout()
        ops_grid.setContentsMargins(0, 0, 0, 0)
        ops_grid.setHorizontalSpacing(SPACE_SM)
        ops_grid.setVerticalSpacing(SPACE_SM)
        ops_grid.addWidget(self.btn_set_boundary, 0, 0)
        ops_grid.addWidget(self.btn_lock, 0, 1)
        ops_grid.addWidget(self.btn_snap, 1, 0)
        ops_grid.addWidget(self.btn_confirm, 1, 1)
        ops_grid.setColumnStretch(0, 1)
        ops_grid.setColumnStretch(1, 1)
        ops_card.body_layout.addLayout(ops_grid)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(list_card, 1)
        outer.addWidget(card_card)
        outer.addWidget(ops_card)
        outer.addStretch(0)

        self._sync_lock_button()

    # ---- 刷新 ----------------------------------------------------------

    def refresh(self, *, keep_row: bool = True) -> None:
        plan = self.state.current_plan
        if plan is None:
            self.episode_table.setRowCount(0)
            self._clear_card()
            return

        episodes = plan.episodes()
        self.episode_table.blockSignals(True)
        self.episode_table.setRowCount(len(episodes))
        for row, episode in enumerate(episodes):
            self.episode_table.setItem(
                row, 0, QTableWidgetItem(f"第{episode.index:02d}集")
            )
            self.episode_table.setItem(
                row,
                1,
                QTableWidgetItem(format_seconds_brief(episode.duration_seconds)),
            )
            dot = QTableWidgetItem("")
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setData(Qt.ItemDataRole.UserRole, self._dot_kind(plan, episode))
            self.episode_table.setItem(row, 2, dot)
        self.episode_table.blockSignals(False)
        self._recolor_dots()

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
        """当前位置来自中栏舞台（本面板不再自己持有播放器）。

        卡片不再逐行重排当前位置——它在中栏时间行里已经有了。这里只留值，
        供外部（或将来）读取。
        """
        self.position = position

    # ---- 内部 ----------------------------------------------------------

    @staticmethod
    def _dot_kind(plan, episode) -> str:
        if episode.index in plan.exceptions:
            return "exception"
        if episode.end_ticks in plan.locked_ticks:
            return "locked"
        return "unlocked"

    def _recolor_dots(self) -> None:
        """按当前主题重上色：QTableWidgetItem 的前景色不走 QSS。"""
        mode = current_mode()
        for row in range(self.episode_table.rowCount()):
            item = self.episode_table.item(row, 2)
            if item is None:
                continue
            kind = item.data(Qt.ItemDataRole.UserRole) or "unlocked"
            item.setText(_DOT_TEXT[kind])
            item.setForeground(QColor(qcolor(mode, _DOT_COLOR[kind])))
            item.setToolTip(_DOT_HINT[kind])

    def event(self, event) -> bool:  # noqa: N802 - Qt 命名
        # 切主题时 QSS 会重设全局样式（StyleChange），但表格项的前景色是
        # item 数据而不是样式表，必须自己重刷一遍，否则深色下点会"消失"。
        # 必须延到下一轮事件循环：``apply_theme`` 是先 setStyleSheet（此刻派发
        # StyleChange）再更新 ``_CURRENT_MODE``，当场重刷取到的还是旧主题色。
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "episode_table"):
            QTimer.singleShot(0, self._recolor_dots)
        return super().event(event)

    def _episode_selected(self) -> None:
        rows = self.episode_table.selectionModel().selectedRows()
        if not rows:
            return
        self.current_episode = rows[0].row() + 1
        self._update_card()
        self.episode_selected.emit(self.current_episode)

    def _current_episode_obj(self):
        plan = self.state.current_plan
        if plan is None:
            return None
        episodes = plan.episodes()
        if not 1 <= self.current_episode <= len(episodes):
            return None
        return episodes[self.current_episode - 1]

    def _is_current_locked(self) -> bool:
        plan = self.state.current_plan
        episode = self._current_episode_obj()
        if plan is None or episode is None:
            return False
        return episode.end_ticks in plan.locked_ticks

    def _sync_lock_button(self) -> None:
        locked = self._is_current_locked()
        self.btn_lock.setText("解除" if locked else "锁定")
        self.btn_lock.setToolTip(
            "解除锁定，恢复可拖动" if locked else "锁定该切点，锁定后不能再拖动"
        )

    def _toggle_lock(self) -> None:
        self.lock_requested.emit(self.current_episode, not self._is_current_locked())

    @staticmethod
    def _short_time(seconds) -> str:
        """``HH:MM:SS``，小时不补零（窄栏里省下的两位正好是一行的余量）。"""
        text = format_timecode(seconds).split(".")[0]
        return text[1:] if text.startswith("0") else text

    def _clear_card(self) -> None:
        self.episode_title.setText("—")
        self.episode_range.setText("—")
        self.episode_meta.setText("—")
        self._review_pill.set_text("未审核")
        self._review_pill.set_tone("muted")
        self._sync_lock_button()

    def _update_card(self) -> None:
        plan = self.state.current_plan
        episode = self._current_episode_obj()
        if plan is None or episode is None:
            self._clear_card()
            return

        source = _SOURCE_TEXT.get(plan.source, "自动候选")

        self.episode_title.setText(f"第{episode.index:02d}集")
        # 区间显示到秒。小时不补零（"0:12:34" 而不是 "00:12:34"）：补零的
        # 26 字符等宽行是 255px，而卡片正文可用宽度只有 240px（栏 340 − 外边距 −
        # 卡片两层内边距），会折成两行。去掉两位零后 225px，稳定一行。
        start = self._short_time(episode.start_seconds)
        end = self._short_time(episode.end_seconds)
        # 零宽断点：整串没有可断处，留一个断点让它在更窄的栏里换行而不是被裁。
        self.episode_range.setText(f"{start}\u200b–\u200b{end}")
        self.episode_range.setToolTip(f"{start}–{end}")
        self.episode_meta.setText(
            f"时长 {format_seconds_brief(episode.duration_seconds)} · 切点{source}"
        )

        if plan.semantic_review_status == "confirmed":
            self._review_pill.set_text("已确认")
            self._review_pill.set_tone("success")
        else:
            self._review_pill.set_text("未审核")
            self._review_pill.set_tone("muted")

        self._sync_lock_button()

    def can_continue(self) -> tuple[bool, str]:
        plan = self.state.current_plan
        if plan is None:
            return False, "尚无方案。"
        blocking = [p for p in plan.validate() if p.is_blocking]
        if blocking:
            return False, "方案存在未解决的硬约束问题：" + blocking[0].describe()
        return True, ""