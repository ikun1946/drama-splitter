"""IssueList —— 风格化的兼容性问题表。

封装 ``QTableWidget``：保留 ``rowCount()`` / ``setRowCount()`` / ``item()`` / ``setItem()`` 等公开方法，
这样冒烟测试 (``window.import_page.issues.rowCount()``) 不需要改。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)


class IssueList(QTableWidget):
    """四级兼容性问题表：级别 / 代码 / 说明 / 建议。

    颜色由主题令牌统一管理；接口与 ``QTableWidget`` 兼容。
    """

    LEVEL_TONES: dict[str, str] = {
        "block": "#b3261e",
        "warn": "#9a6700",
        "info": "#3d6b35",
    }

    LEVEL_TEXT: dict[str, str] = {
        "block": "阻断",
        "warn": "警告",
        "info": "提示",
    }

    def __init__(
        self,
        columns: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        cols = columns or ["级别", "代码", "说明", "建议"]
        super().__init__(0, len(cols), parent)
        self.setHorizontalHeaderLabels(cols)
        header_view = self.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header_view.setStretchLastSection(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)

    def add_issue(self, level: str, code: str, message: str, detail: str = "") -> None:
        """添加一行 issue。level 取值 block / warn / info。"""
        row = self.rowCount()
        self.insertRow(row)
        cells = [
            self.LEVEL_TEXT.get(level, level),
            code,
            message,
            detail,
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setForeground(QColor(self.LEVEL_TONES.get(level, "#333")))
            self.setItem(row, column, item)

    def clear_issues(self) -> None:
        self.setRowCount(0)

    # 兼容旧 API：issues 直接来自 QTableWidget 也能用 setRowCount / rowCount
    def setRowCount(self, rows: int) -> None:  # noqa: N802 - Qt 命名
        super().setRowCount(rows)

    def rowCount(self) -> int:  # noqa: N802 - Qt 命名
        return super().rowCount()

    # 提供一个 data() 兼容接口，方便旧代码遍历
    def issues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in range(self.rowCount()):
            out.append(
                {
                    "level_text": self.item(r, 0).text() if self.item(r, 0) else "",
                    "code": self.item(r, 1).text() if self.item(r, 1) else "",
                    "message": self.item(r, 2).text() if self.item(r, 2) else "",
                    "detail": self.item(r, 3).text() if self.item(r, 3) else "",
                }
            )
        return out