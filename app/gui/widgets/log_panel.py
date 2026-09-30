"""结构化日志面板。

为什么重写
----------
原先是一个等宽 ``QTextEdit``，每行拼成 ``[17:32:44] [INFO] 正文``。分析一次短剧
会刷出上百行，其中「整集复核」对每一集都写一遍**除集号外逐字相同**的结论 ——
四集就是四条 60 字的重复，整屏都在读同一句话。

改成分层条目：彩色级别点 + 等宽时间戳 + 自动换行正文；**连续且仅集号不同**的
结论折叠成一条 + ``×N`` 胶囊，点开可看全。

只折叠"连续"且"仅集号不同"
------------------------
- **不跨时间折叠**：把整场分析的同类结论隔空合并，用户会以为中间漏了信息。
- **只归一化集号**（``第01集`` / ``第2集`` → 同一个键）：这是复核日志唯一的
  参数化维度，除此之外正文必须真的逐字相同才折叠。归一化范围再宽一点，
  就会把"看起来像"的不同结论误并成一条。

公开面不变
----------
``append(text, level)`` / ``clear()`` / ``toPlainText()`` 三个入口与旧版一致，
``toPlainText()`` 仍输出 **原格式**（``[HH:MM:SS] [LEVEL] 正文``，折叠时首条 + 计数），
人工复制与任何下游消费者都不会因为换了渲染方式而断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = ["LogPanel", "LogEntry", "MAX_ENTRIES"]

#: 最多保留的条数。逐行 QWidget 在千行下比 QTextEdit 明显慢；超出后从最早的开始收。
MAX_ENTRIES = 800

_LEVEL_PREFIX = {"info": "INFO", "warn": "WARN", "error": "ERR ", "success": "OK  "}
_LEVEL_TONE = {"info": "info", "success": "success", "warn": "warn", "error": "error"}

#: 折叠键的归一化：只把集号抹平，其余逐字比对。
_EPISODE_RE = re.compile(r"第\s*0?\d+\s*集")


def fold_key(text: str) -> str:
    """折叠判定用的键：把集号归一化，其余原样。"""
    return _EPISODE_RE.sub("第N集", text)


@dataclass(frozen=True)
class LogEntry:
    stamp: str
    level: str
    text: str

    @property
    def plain(self) -> str:
        return f"[{self.stamp}] [{_LEVEL_PREFIX.get(self.level, 'INFO')}] {self.text}"

    @property
    def fold(self) -> str:
        return fold_key(self.text)


class _LogRow(QFrame):
    """一行日志：级别点 + 时间戳 + 正文（+ 折叠时的 ×N 胶囊）。"""

    #: 折叠段被展开/收起（名字里带 _toggled：``expanded`` 被 is_expanded() 占用了，
    #: 同名会让后定义的方法覆盖信号，``self.expanded.emit()`` 直接抛 AttributeError）
    expand_toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "log-row")
        self._texts: list[str] = []
        self._expanded = False

        self._dot = QFrame(self)
        self._dot.setProperty("role", "log-dot")
        self._dot.setFixedSize(6, 6)

        self._time = QLabel("", self)
        self._time.setProperty("role", "log-time")
        self._time.setFixedWidth(56)
        self._time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )

        self._msg = QLabel("", self)
        self._msg.setProperty("role", "log-msg")
        self._msg.setWordWrap(True)
        self._msg.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)
        body.addWidget(self._msg)

        self._fold_row = QWidget(self)
        fold_layout = QHBoxLayout(self._fold_row)
        fold_layout.setContentsMargins(0, 0, 0, 0)
        fold_layout.setSpacing(8)
        self._fold_mark = QLabel("", self._fold_row)
        self._fold_mark.setProperty("role", "log-foldmark")
        self._fold_mark.setWordWrap(True)
        self._fold_btn = QPushButton("×1", self._fold_row)
        self._fold_btn.setProperty("role", "ghost")
        self._fold_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fold_btn.setToolTip("点开看全部")
        self._fold_btn.clicked.connect(self._on_expand)
        fold_layout.addWidget(self._fold_mark, 1)
        fold_layout.addWidget(self._fold_btn, 0, Qt.AlignmentFlag.AlignTop)
        self._fold_row.setVisible(False)
        body.addWidget(self._fold_row)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._time, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(body, 1)

    def set_entry(self, entry: LogEntry, texts: list[str]) -> None:
        """``entry`` 是这一段的第一条（时间戳与级别取自它）；``texts`` 是整段正文。"""
        self._texts = list(texts) or [entry.text]
        self._time.setText(entry.stamp)
        self._dot.setProperty("tone", _LEVEL_TONE.get(entry.level, "info"))
        self._dot.style().unpolish(self._dot)
        self._dot.style().polish(self._dot)
        self._refresh_fold()

    def _count(self) -> int:
        return len(self._texts)

    def _refresh_fold(self) -> None:
        count = len(self._texts)
        folded = count > 1
        self._fold_row.setVisible(folded)
        if self._expanded:
            # 展开 = 整段正文原样铺开，不能只改一句提示——那等于"说可以看但看不到"
            self._msg.setText("\n".join(self._texts))
        else:
            self._msg.setText(self._texts[0])
        if not folded:
            self._fold_btn.setText("")
            return
        if self._expanded:
            self._fold_mark.setText(f"已展开这一段全部 {count} 条")
            self._fold_btn.setText("收起")
        else:
            self._fold_mark.setText(f"另有 {count - 1} 条仅集号不同的结论")
            self._fold_btn.setText(f"×{count}")

    def count(self) -> int:
        return len(self._texts)

    def is_expanded(self) -> bool:
        return self._expanded

    def _on_expand(self) -> None:
        self._expanded = not self._expanded
        self._refresh_fold()
        self.expand_toggled.emit()



class _CollapsedNotice(QWidget):
    """超出上限后的"更早的 N 条已收起"。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        label = QLabel("", self)
        label.setProperty("role", "log-foldmark")
        self._label = label
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 8, 6)
        layout.addWidget(label, 1)

    def set_count(self, count: int) -> None:
        self._label.setText(f"更早的 {count} 条日志已收起")
        self.setVisible(count > 0)


class LogPanel(QWidget):
    """只读日志面板。自动给追加的文本加时间码，并折叠重复结论。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[LogEntry] = []
        self._rows: list[_LogRow] = []
        #: 每行覆盖的 ``(起始下标, 长度)``，用于追加时只重算受影响的最后一段
        self._spans: list[tuple[int, int]] = []
        #: 用户手动上滚后置 False：分析日志一刷就把人正在看的内容顶走是旧版最大的毛病
        self._follow = True

        self._notice = _CollapsedNotice(self)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addWidget(self._notice)
        self._body_layout.addStretch(1)

        self._area = QScrollArea(self)
        self._area.setWidget(self._body)
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.Shape.NoFrame)
        self._area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._area.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._area)
        self.setProperty("role", "log")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ---- 公开面 --------------------------------------------------------

    def append(self, text: str, level: str = "info") -> None:
        """追加一行日志，自动加时间码前缀。"""
        self._entries.append(
            LogEntry(datetime.now().strftime("%H:%M:%S"), level, str(text))
        )
        self._trim()
        self._rebuild_tail()
        if self._follow:
            self._scroll_to_bottom()

    def clear(self) -> None:  # noqa: A003 - Qt 命名
        self._entries.clear()
        self._forget = 0
        self._rebuild_all()
        self._follow = True

    def toPlainText(self) -> str:  # noqa: N802 - 与旧版同名
        """**原格式**全文，供人工复制。

        折叠段的**每一条**都要出现在这里——早先只输出首条加一句计数，等于把
        第 02/03/04 集的结论从复制结果里抹掉了。折叠只该影响屏幕上的排版，
        不能影响能拿走的文本。
        """
        lines: list[str] = []
        index = 0
        total = len(self._entries)
        while index < total:
            run = 1
            key = self._entries[index].fold
            level = self._entries[index].level
            while (
                index + run < total
                and self._entries[index + run].fold == key
                and self._entries[index + run].level == level
            ):
                run += 1
            for offset in range(run):
                lines.append(self._entries[index + offset].plain)
            if run > 1:
                lines.append(f"    （以上 {run} 条仅集号不同，界面上折叠为一条）")
            index += run
        return "\n".join(lines)

    def count(self) -> int:
        return len(self._entries)

    # ---- 内部 ----------------------------------------------------------

    _forget = 0

    def _trim(self) -> None:
        limit = max(1, MAX_ENTRIES)
        if len(self._entries) <= limit:
            return
        drop = len(self._entries) - limit
        self._entries = self._entries[drop:]
        self._forget += drop
        self._notice.set_count(self._forget)
        self._rebuild_all()

    def _rebuild_all(self) -> None:
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self._spans = []
        for index in range(len(self._entries)):
            self._append_row(index)
        self._notice.set_count(self._forget)

    def _rebuild_tail(self) -> None:
        """只重算最后一段。

        折叠判定是**连续**的，所以新条目只可能并进末尾那一段，不会牵动更早的行。
        逐条全量重建在长日志下是 O(n²)，这里用 ``_spans`` 记录每行覆盖的区间，
        只把与新段落重叠的行摘掉重建。
        """
        if not self._entries:
            return
        self._append_row(len(self._entries) - 1)

    def _run_start(self, index: int) -> int:
        """``index`` 所在连续段的**起始**下标（只向后走）。

        刻意不向前统计段长：``append`` 调用时新条目永远是最后一条，向前看不到
        任何还没进来的内容，统计出来恒为 1。段长改由 ``index - 段首 + 1`` 得出。
        """
        entry = self._entries[index]
        start = index
        while start > 0:
            probe = self._entries[start - 1]
            if probe.fold != entry.fold or probe.level != entry.level:
                break
            start -= 1
        return start

    def _append_row(self, index: int) -> None:
        start = self._run_start(index)
        count = index - start + 1
        if self._spans and self._spans[-1][0] == start:
            # 已经在上一行覆盖的同一段里：只把整段正文顶上去，
            # 时间戳与级别仍取自第一条（那一段本来就是同一条结论重复了 N 次）
            self._rows[-1].set_entry(
                self._entries[start], [e.text for e in self._entries[start:index + 1]]
            )
            self._spans[-1] = (start, count)
            return
        row = _LogRow(self._body)
        row.set_entry(
            self._entries[start], [e.text for e in self._entries[start:index + 1]]
        )
        # 末尾的 stretch 之前插入，保证行都在顶部堆叠
        self._body_layout.insertWidget(self._body_layout.count() - 1, row)
        self._rows.append(row)
        self._spans.append((start, count))

    def _on_scrolled(self, value: int) -> None:
        bar = self._area.verticalScrollBar()
        at_bottom = value >= bar.maximum() - 2
        if at_bottom != self._follow:
            self._follow = at_bottom

    def _scroll_to_bottom(self) -> None:
        bar = self._area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def following(self) -> bool:
        return self._follow
