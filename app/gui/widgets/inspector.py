"""Inspector —— 常驻右栏检查器（方案 §二十四）。

作用只有一个：集中告诉用户「哪些地方可能有问题」。

与对话框的区别（不能互相替代）
-------------------------------
- 对话框（``app/gui/dialogs/``）是**独立窗口**：用户主动打开、用完关掉，
  用于设置 / 模型 / 导出这类"不是工作流一步"的事；
- 检查器是**常驻右栏**：把"当前工程的健康状态"一直摆在眼前，用户不必去
  某个页面翻问题清单。

因此这里复用的是 EmptyState / LogPanel 这些叶子控件，而不是对话框。

两个标签页
----------
问题（默认）  一条清单：阻断 / 需处理 / 建议三类同列，靠色点区分，
              见 set_issues() 与 set_advice()。原先「建议」独占一个页签，
              同一个工程的毛病分两屏列着，用户得来回翻才知道看全了没有。
日志          后台任务输出，见 append_log()

一行只占四件东西：色条 + 标题（单行省略）+ 定位（等宽）+ 详情开关；
说明 / 行动建议 / 「查看」按需展开——四行字的卡片一屏只放得下三四条，
收起来之后同样的高度能看七八条。

优先级
------
P0 阻断 / P1 需处理 / P2 建议，与方案 §二十六 一致。条目按优先级排序，
同类按时间先后。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
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

from . import message
from .empty_state import EmptyState
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
            self.episode if self.episode is not None else 99,
            self.title,
        )

    def dedupe_key(self) -> tuple:
        if self.key:
            return (self.key, self.priority)
        return (self.title, self.priority, self.episode)


def _format_ms(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _locator(issue: InspectorIssue) -> str:
    """定位串：``第 03 集 · 00:12:34``；两样都没有时返回空串。"""
    bits: list[str] = []
    if issue.episode is not None:
        bits.append(f"第 {issue.episode:02d} 集")
    if issue.target_ms is not None:
        bits.append(_format_ms(issue.target_ms))
    return " · ".join(bits)


class _ElidedLabel(QLabel):
    """单行省略标签（标题专用）。

    QLabel 的默认 minimumSizeHint 等于整行文字的宽度——长标题会把右栏
    的最小宽度顶过 320px。这里把最小宽度钉在 40px，显示时按当前宽度省略，
    全文留在 tooltip 里。
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._elide()

    def _elide(self) -> None:
        fm = self.fontMetrics()
        QLabel.setText(
            self,
            fm.elidedText(self._full, Qt.TextElideMode.ElideRight, max(40, self.width())),
        )

    def resizeEvent(self, event) -> None:  # noqa: N802  (Qt 命名)
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, self.fontMetrics().height())


class _IssueRow(QFrame):
    """清单里的一行：色条 + 标题 + 定位 + 详情开关。

    详情（说明 / 行动建议 / 「查看」）默认收起，点这一行或右侧开关展开；
    展开只影响这一行，别的行不受牵连。
    """

    jump_requested = Signal(int)   # target_ms

    def __init__(self, issue: InspectorIssue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "issue-row")

        # 左侧优先级色条（阻断红 / 需处理黄 / 建议蓝）
        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setProperty("role", "issue-bar")
        bar.setProperty("tone", PRIORITY_TONE.get(issue.priority, "primary"))

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        title = _ElidedLabel(issue.title)
        title.setProperty("role", "issue-title")
        title.setToolTip(issue.title)   # 省略之后，全文还能悬停看到
        text.addWidget(title)

        locator = _locator(issue)
        if locator:
            meta = QLabel(locator)
            meta.setProperty("role", "issue-meta")   # QSS 里就是等宽：时间码对齐才好读
            text.addWidget(meta)
        head.addLayout(text, 1)

        # ---- 详情（默认收起）----
        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 4, 0, 0)
        body.setSpacing(4)
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
            jump_row = QHBoxLayout()
            jump_row.setContentsMargins(0, 0, 0, 0)
            jump_row.addStretch(1)
            btn_jump = QPushButton("查看")
            btn_jump.setProperty("role", "ghost")
            btn_jump.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_jump.clicked.connect(
                lambda _=False, ms=issue.target_ms: self.jump_requested.emit(ms)
            )
            jump_row.addWidget(btn_jump)
            body.addLayout(jump_row)
        self._body.setVisible(False)

        has_body = bool(issue.detail or issue.hint or issue.target_ms is not None)

        self.btn_toggle = QPushButton("详情")
        self.btn_toggle.setProperty("role", "ghost")
        self.btn_toggle.setCheckable(True)
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.setToolTip("展开这条问题的说明与动作")
        self.btn_toggle.setVisible(has_body)
        self.btn_toggle.toggled.connect(self._on_toggled)
        head.addWidget(self.btn_toggle)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(head)
        column.addWidget(self._body)

        wrap = QHBoxLayout(self)
        wrap.setContentsMargins(8, 6, 8, 6)
        wrap.setSpacing(8)
        wrap.addWidget(bar)
        wrap.addLayout(column, 1)

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.btn_toggle.setText("收起" if checked else "详情")

    def mousePressEvent(self, event) -> None:  # noqa: N802  (Qt 命名)
        """整行可点：右栏里 3px 的开关不好瞄，标题本身也该能展开。"""
        if event.button() == Qt.MouseButton.LeftButton and self.btn_toggle.isVisible():
            self.btn_toggle.toggle()
        super().mousePressEvent(event)


class Inspector(QFrame):
    """右栏检查器：问题（含建议）/ 日志 两个标签页。"""

    jump_requested = Signal(int)   # 用户点了某条问题的「查看」，带毫秒位置

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "inspector")
        self.setMinimumWidth(MIN_WIDTH)
        self.setMaximumWidth(MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._issues: list[InspectorIssue] = []
        self._advice: list[InspectorIssue] = []
        self._rows: list[_IssueRow] = []
        self._issue_cursor = -1   # N/P 巡航位置：-1 表示尚未开始，首次 N 落到第一条

        # ---- 标签切换 ----
        self._tabs: list[QPushButton] = []
        self._tab_row = QHBoxLayout()
        self._tab_row.setContentsMargins(0, 0, 0, 0)
        self._tab_row.setSpacing(0)
        for index, name in enumerate(("问题", "日志")):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("role", "inspector-tab")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, i=index: self.set_tab(i))
            self._tabs.append(btn)
            self._tab_row.addWidget(btn, 1)
        self._tabs[0].setChecked(True)

        # ---- 页 0：问题（阻断 / 需处理 / 建议同列）----
        self._problem_list = QWidget()
        self._problem_layout = QVBoxLayout(self._problem_list)
        self._problem_layout.setContentsMargins(6, 6, 6, 6)
        self._problem_layout.setSpacing(6)
        self._problem_layout.addStretch(1)

        # 空态只有一张卡：标题 + 一行说明 + 一行指向底栏动作的提示。
        # 原先「没有问题」与「下一步」两张卡上下叠着，两句话各占右栏一半。
        self._empty = EmptyState(
            "没有问题",
            detail=(
                "兼容性问题、方案校验与整集复核建议都会列在这里。\n"
                "下一步：点底栏的「开始分析」，按对白停顿给出切点后再回来逐条核对。"
            ),
        )
        # 旧名保留：空态与「下一步」已是同一张卡，测试仍按此名取用
        self._next_step = self._empty

        # 没有条目时把空列表藏起来：留着一个空滚动区，用户看到的就是「一片空白」
        self._problem_scroll = _scrolled(self._problem_list)
        self._problem_scroll.setVisible(False)

        problem_page = QWidget()
        problem_wrap = QVBoxLayout(problem_page)
        problem_wrap.setContentsMargins(0, 0, 0, 0)
        problem_wrap.setSpacing(6)
        problem_wrap.addWidget(self._empty, 1)
        problem_wrap.addWidget(self._problem_scroll, 1)

        # 页 1：日志
        self._log = LogPanel()
        log_page = QWidget()
        log_wrap = QVBoxLayout(log_page)
        log_wrap.setContentsMargins(0, 0, 0, 0)
        log_wrap.setSpacing(0)
        log_wrap.addWidget(self._build_log_toolbar())
        log_wrap.addWidget(self._log, 1)

        self._pages = QStackedWidget()
        self._pages.addWidget(problem_page)
        self._pages.addWidget(log_page)

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
        """0 问题（含建议） / 1 日志。

        旧调用点里 ``2`` 表示原「建议」页——建议已并入问题列表，这里把 2
        落到 0，免得刷新时把用户甩去日志页（``workbench.py`` 里
        ``refresh(inspector_tab=2)``）。workbench 改掉之后这段可以删。
        """
        if index == 2:
            index = 0
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
        self._issue_cursor = -1   # 清单整体换过，巡航从头开始
        self._render()

    def issues(self) -> list[InspectorIssue]:
        return list(self._issues)

    def problem_count(self) -> int:
        return len(self._issues)

    def blocking_count(self) -> int:
        return sum(1 for i in self._issues if i.priority == "P0")

    def set_advice(self, advice: list[InspectorIssue]) -> None:
        """中性提示：落在问题同一个列表里，色点为 primary，不参与阻断计数。"""
        self._advice = sorted(advice, key=lambda i: i.sort_key())
        self._render()

    def cycle_issue(self, delta: int) -> InspectorIssue | None:
        """在可定位的条目之间循环巡航（N=下一个 / P=上一个，方案 §54）。

        没有目标位置（target_ms 为 None）的条目无法跳转，直接跳过；
        清单为空或全部不可定位时返回 None、不发信号。返回发出的条目。
        问题与建议同列，巡航也走同一串。
        """
        jumpable = [issue for issue in self._entries() if issue.target_ms is not None]
        if not jumpable:
            return None
        self._issue_cursor = (self._issue_cursor + (1 if delta >= 0 else -1)) % len(jumpable)
        issue = jumpable[self._issue_cursor]
        self.set_tab(0)   # 让用户回头能对上「刚跳的是哪条」
        self.jump_requested.emit(issue.target_ms)
        return issue

    def append_log(self, text: str, level: str = "info") -> None:
        self._log.append(text, level)

    def clear_log(self) -> None:
        self._log.clear()

    def log_text(self) -> str:
        return self._log.toPlainText()

    # ---- 日志工具条 ----------------------------------------------------

    def _build_log_toolbar(self) -> QWidget:
        """日志页顶部的小工具条：导出与清空。

        为什么要有：分析日志是排障的第一手证据，但原先只能靠"全选复制"——
        长日志在只读面板里选不全，用户想把它发出来问都难。这里给一个明确的
        落盘入口（另存为 .txt/.log），并留一个清空按钮给"重跑前眼不见为净"。
        """
        self.btn_export_log = QPushButton("导出日志…")
        self.btn_export_log.setProperty("role", "mini")
        self.btn_export_log.setToolTip("把当前日志另存为文本文件（便于反馈问题）")
        # 不能直接连 export_log：clicked 带 checked(bool)，会被当成 path 收下
        # （见 _on_export_clicked 的说明）。
        self.btn_export_log.clicked.connect(self._on_export_clicked)

        self.btn_clear_log = QPushButton("清空")
        self.btn_clear_log.setProperty("role", "mini")
        self.btn_clear_log.setToolTip("清空当前日志（不影响已导出的文件）")
        self.btn_clear_log.clicked.connect(self.clear_log)

        holder = QFrame()
        holder.setProperty("role", "log-toolbar")
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)
        row.addWidget(self.btn_export_log)
        row.addWidget(self.btn_clear_log)
        row.addStretch(1)
        return holder

    def _on_export_clicked(self) -> None:
        """按钮点击入口——**必须把 clicked 带的 checked 参数丢掉**。

        直接 ``clicked.connect(self.export_log)`` 会出问题：``QPushButton.clicked``
        带一个 checked(bool)，PySide 会把它塞进 ``export_log(path=None)`` 的 path。
        它不是 None，于是跳过文件对话框，拿 False 去 ``Path()`` 抛 TypeError；
        异常发生在 Qt 槽里被直接吞掉，用户看到的就是「点了没反应」。
        （此前的测试全是直接调 export_log，所以一直没抓到。）
        """
        self.export_log()

    def export_log(self, path: str | None = None) -> str | None:
        """把日志写到文件；返回落盘路径（用户取消时返回 None）。

        ``path`` 可用于测试/自动化直接指定落点，跳过文件对话框——与项目里
        其它"可覆写决策点"同一条约定（离屏环境不弹模态框）。
        """
        text = self._log.toPlainText()
        if not text.strip():
            message.information(self, "没有日志", "当前还没有可导出的日志。")
            return None

        if path is None:
            from PySide6.QtWidgets import QFileDialog

            suggested = f"drama-splitter-日志-{datetime.now():%Y%m%d-%H%M%S}.txt"
            chosen, _ = QFileDialog.getSaveFileName(
                self, "导出日志", suggested, "文本文件 (*.txt);;日志文件 (*.log);;所有文件 (*)"
            )
            if not chosen:
                return None
            path = chosen

        target = Path(path)
        header = (
            f"AI 短剧智能分集器 日志\n"
            f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"共 {self._log.count()} 条\n"
            + "-" * 40 + "\n"
        )
        try:
            target.write_text(header + text + "\n", encoding="utf-8")
        except OSError as exc:
            message.warning(self, "导出失败", f"无法写入 {target}：{exc}")
            return None
        message.information(self, "日志已导出", f"已写入：\n{target}")
        return str(target)

    # ---- 内部 ----------------------------------------------------------

    def _entries(self) -> list[InspectorIssue]:
        """问题在前、建议在后，各自按优先级与集号排序。"""
        return sorted(self._issues, key=lambda i: i.sort_key()) + sorted(
            self._advice, key=lambda i: i.sort_key()
        )

    def _render(self) -> None:
        """按「问题 + 建议」重建整列（保留末尾的 stretch）。"""
        while self._problem_layout.count() > 1:
            item = self._problem_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._rows = []
        entries = self._entries()
        for issue in entries:
            row = _IssueRow(issue)
            row.jump_requested.connect(self.jump_requested.emit)
            self._problem_layout.insertWidget(self._problem_layout.count() - 1, row)
            self._rows.append(row)
        self._empty.setVisible(not entries)
        self._problem_scroll.setVisible(bool(entries))
        self._refresh_count()

    def _refresh_count(self) -> None:
        total = len(self._issues) + len(self._advice)
        self._tabs[0].setText(f"问题 {total}" if total else "问题")
        blocking = self.blocking_count()
        self._count_label.setText(f"其中 {blocking} 项阻断，需先处理。" if blocking else "")


def _scrolled(widget: QWidget) -> QScrollArea:
    """无边框纵向滚动容器（右栏内容不固定高度）。"""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area
