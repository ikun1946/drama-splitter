"""Inspector —— 常驻右栏检查器（方案 §二十四）。

作用只有一个：集中告诉用户「哪些地方可能有问题」。

与 Drawer 的区别（不能互相替代）
--------------------------------
- Drawer 是**浮层**：720px 固定宽、QPropertyAnimation 滑入、挂在布局之外、
  自己带遮罩，打开时遮住主工作区。用于「设置 / 模型」这类临时任务。
- Inspector 是**常驻布局成员**：280–320px、无动画、无遮罩，和主内容区并排，
  随窗口缩放。用于「问题 / 建议 / 日志」这类需要一直看得到的信息。

因此这里复用的是 Card / LogPanel / IssueList 这些叶子控件，而不是 Drawer。

三个标签页
----------
问题（默认）  高风险问题清单，见 set_issues()
建议          中性提示，如「这段切点附近有对白跨边界」
日志          后台任务输出，见 append_log()

优先级
------
P0 阻断 / P1 需处理 / P2 建议，与方案 §二十六 一致。条目按优先级排序，
同类按时间先后。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .log_panel import LogPanel

__all__ = ["Inspector", "InspectorIssue", "PRIORITY_LABEL", "PRIORITY_ORDER"]

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
PRIORITY_LABEL = {"P0": "阻断", "P1": "需处理", "P2": "建议"}
# 与 theme 的 tone 对应：P0 红、P1 黄、P2 蓝
PRIORITY_TONE = {"P0": "danger", "P1": "warn", "P2": "primary"}

MIN_WIDTH = 280
MAX_WIDTH = 320


@dataclass
class InspectorIssue:
    """一条问题。

    episode 为 None 表示不属于某一集（如整片层面的问题）。
    target_ms 用于「跳到该位置」，None 表示无法定位（按钮置灰）。
    """

    title: str
    detail: str = ""
    priority: str = "P2"
    episode: int | None = None
    target_ms: int | None = None
    hint: str = ""            # 行动建议，如「建议向后移动 1.2～2.4 秒」
    key: str = ""             # 去重键；为空则用 (priority, title, episode) 推导

    def sort_key(self) -> tuple:
        return (
            PRIORITY_ORDER.get(self.priority, 9),
            self.episode if self.episode is not None else 10**6,
            self.title,
        )

    def dedupe_key(self) -> tuple:
        if self.key:
            return (self.key, self.priority)
        return (self.title, self.priority, self.episode)


class _IssueRow(QFrame):
    """问题卡片里的一行。

    布局：色块 + 标题（可换行）+ 定位行（集号 / 时间码 / 跳转按钮）
    """

    jump_requested = Signal(int)   # target_ms

    def __init__(self, issue: InspectorIssue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "issue-row")

        # 左侧优先级色条
        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setProperty("role", "issue-bar")
        bar.setProperty("tone", PRIORITY_TONE.get(issue.priority, "primary"))

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(2)

        title = QLabel(issue.title)
        title.setWordWrap(True)
        title.setProperty("role", "issue-title")
        body.addWidget(title)

        meta_bits: list[str] = []
        if issue.episode is not None:
            meta_bits.append(f"第{issue.episode:02d}集")
        if issue.target_ms is not None:
            meta_bits.append(_format_ms(issue.target_ms))
        if meta_bits:
            meta = QLabel("　".join(meta_bits))
            meta.setProperty("role", "issue-meta")
            body.addWidget(meta)

        if issue.detail:
            detail = QLabel(issue.detail)
            detail.setWordWrap(True)
            detail.setProperty("role", "issue-detail")
            body.addWidget(detail)

        if issue.hint:
            hint = QLabel(issue.hint)
            hint.setWordWrap(True)
            hint.setProperty("role", "issue-hint")
            body.addWidget(hint)

        if issue.target_ms is not None:
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 0)
            row.addStretch(1)
            btn_jump = QPushButton("查看")
            btn_jump.setProperty("role", "ghost")
            btn_jump.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_jump.clicked.connect(
                lambda _=False, ms=issue.target_ms: self.jump_requested.emit(ms)
            )
            row.addWidget(btn_jump)
            body.addLayout(row)

        wrap = QHBoxLayout(self)
        wrap.setContentsMargins(8, 6, 8, 6)
        wrap.setSpacing(8)
        wrap.addWidget(bar)
        wrap.addLayout(body, 1)


def _format_ms(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class Inspector(QFrame):
    """右栏检查器：问题 / 建议 / 日志 三个标签页。"""

    jump_requested = Signal(int)   # 用户点了某条问题的「查看」，带毫秒位置

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "inspector")
        self.setMinimumWidth(MIN_WIDTH)
        self.setMaximumWidth(MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._issues: list[InspectorIssue] = []
        self._rows: list[_IssueRow] = []

        # ---- 标签切换 ----
        self._tabs: list[QPushButton] = []
        self._tab_row = QHBoxLayout()
        self._tab_row.setContentsMargins(0, 0, 0, 0)
        self._tab_row.setSpacing(0)
        for index, name in enumerate(("问题", "建议", "日志")):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("role", "inspector-tab")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, i=index: self.set_tab(i))
            self._tabs.append(btn)
            self._tab_row.addWidget(btn, 1)
        self._tabs[0].setChecked(True)

        # ---- 页 0：问题 ----
        self._problem_list = QWidget()
        self._problem_layout = QVBoxLayout(self._problem_list)
        self._problem_layout.setContentsMargins(6, 6, 6, 6)
        self._problem_layout.setSpacing(6)
        self._problem_layout.addStretch(1)

        self._empty = QLabel("暂无问题。")
        self._empty.setProperty("role", "muted")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)

        problem_scroll = _scrolled(self._problem_list)
        problem_page = QWidget()
        problem_wrap = QVBoxLayout(problem_page)
        problem_wrap.setContentsMargins(0, 0, 0, 0)
        problem_wrap.setSpacing(6)
        problem_wrap.addWidget(self._empty)
        problem_wrap.addWidget(problem_scroll, 1)

        # 页 1：建议 —— 复用问题列表的渲染逻辑，但用独立数据源
        self._advice_list = QWidget()
        self._advice_layout = QVBoxLayout(self._advice_list)
        self._advice_layout.setContentsMargins(6, 6, 6, 6)
        self._advice_layout.setSpacing(6)
        self._advice_layout.addStretch(1)
        self._advice_empty = QLabel("暂无建议。")
        self._advice_empty.setProperty("role", "muted")
        self._advice_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._advice_empty.setWordWrap(True)
        advice_page = QWidget()
        advice_wrap = QVBoxLayout(advice_page)
        advice_wrap.setContentsMargins(0, 0, 0, 0)
        advice_wrap.setSpacing(6)
        advice_wrap.addWidget(self._advice_empty)
        advice_wrap.addWidget(_scrolled(self._advice_list), 1)

        # 页 2：日志
        self._log = LogPanel()

        self._pages = QStackedWidget()
        self._pages.addWidget(problem_page)
        self._pages.addWidget(advice_page)
        self._pages.addWidget(self._log)

        # ---- 装配 ----
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        tab_holder = QFrame()
        tab_holder.setProperty("role", "inspector-tabs")
        tab_holder.setLayout(self._tab_row)
        tab_holder.setFixedHeight(34)
        outer.addWidget(tab_holder)

        self._count_label = QLabel("")
        self._count_label.setProperty("role", "muted")
        self._count_label.setContentsMargins(10, 4, 10, 0)
        outer.addWidget(self._count_label)

        outer.addWidget(self._pages, 1)

    # ---- 对外 API ------------------------------------------------------

    def set_tab(self, index: int) -> None:
        """0 问题 / 1 建议 / 2 日志。"""
        index = max(0, min(index, len(self._tabs) - 1))
        for i, btn in enumerate(self._tabs):
            btn.setChecked(i == index)
        self._pages.setCurrentIndex(index)

    def current_tab(self) -> int:
        return self._pages.currentIndex()

    def set_issues(self, issues: list[InspectorIssue]) -> None:
        """整体替换问题清单，按优先级与集号排序，重复项自动去重。"""
        seen: set[tuple] = set()
        unique: list[InspectorIssue] = []
        for issue in sorted(issues, key=lambda i: i.sort_key()):
            key = issue.dedupe_key()
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)

        self._issues = unique

        # 清空旧行（保留末尾的 stretch）
        while self._problem_layout.count() > 1:
            item = self._problem_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._rows = []
        for issue in unique:
            row = _IssueRow(issue)
            row.jump_requested.connect(self.jump_requested.emit)
            self._problem_layout.insertWidget(self._problem_layout.count() - 1, row)
            self._rows.append(row)

        self._empty.setVisible(not unique)
        self._refresh_count()

    def issues(self) -> list[InspectorIssue]:
        return list(self._issues)

    def problem_count(self) -> int:
        return len(self._issues)

    def blocking_count(self) -> int:
        return sum(1 for i in self._issues if i.priority == "P0")

    def set_advice(self, advice: list[InspectorIssue]) -> None:
        """中性提示，结构与问题相同但不参与阻断计数。"""
        while self._advice_layout.count() > 1:
            item = self._advice_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        for issue in sorted(advice, key=lambda i: i.sort_key()):
            row = _IssueRow(issue)
            row.jump_requested.connect(self.jump_requested.emit)
            self._advice_layout.insertWidget(self._advice_layout.count() - 1, row)
        self._advice_empty.setVisible(not advice)

    def append_log(self, text: str, level: str = "info") -> None:
        self._log.append(text, level)

    def clear_log(self) -> None:
        self._log.clear()

    def log_text(self) -> str:
        return self._log.toPlainText()

    # ---- 内部 ----------------------------------------------------------

    def _refresh_count(self) -> None:
        if not self._issues:
            self._count_label.setText("")
            self._tabs[0].setText("问题")
            return
        blocking = self.blocking_count()
        if blocking:
            self._tabs[0].setText(f"问题 {len(self._issues)}")
            self._count_label.setText(f"其中 {blocking} 项阻断，需先处理。")
        else:
            self._tabs[0].setText(f"问题 {len(self._issues)}")
            self._count_label.setText("")


def _scrolled(widget: QWidget) -> QScrollArea:
    """无边框纵向滚动容器（右栏内容不固定高度）。"""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area
