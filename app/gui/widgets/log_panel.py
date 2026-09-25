"""LogPanel —— 风格化的日志面板。

基于 ``QTextEdit``，增加：
- 等宽字体
- 时间码灰色前缀（自动补 ``[HH:MM:SS]``）
- append() / append_html() 入口
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextEdit, QWidget


class LogPanel(QTextEdit):
    """只读日志面板，自动给追加的文本加时间码。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setProperty("role", "log")

    def append(self, text: str, level: str = "info") -> None:
        """追加一行日志，自动加时间码前缀。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        # 调色板已禁用（mono 配色），level 仅在 message 前缀展示
        prefix = {
            "info": "INFO",
            "warn": "WARN",
            "error": "ERR ",
        }.get(level, "INFO")
        line = f"[{stamp}] [{prefix}] {text}"
        super().append(line)
        # 自动滚到底
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def clear(self) -> None:  # noqa: A003 - Qt 命名
        super().clear()