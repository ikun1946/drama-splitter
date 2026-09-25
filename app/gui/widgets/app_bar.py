"""顶栏：源文件名 + 保存状态 + 更换视频。

只保留与"当前源文件"直接相关的信息（方案 §七）。原先这里还有四段 stepper、
主题按钮、设置/模型图标按钮——stepper 已被单页工作台的阶段推进取代，主题与
设置/模型入口收进菜单（见 ``MainWindow._build_menu``）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

__all__ = ["AppBar"]

_APP_TITLE = "🎬  AI 短剧分集器"
# 标题区最多显示的字符数：长文件名截断，完整路径放 tooltip
_MAX_TITLE_CHARS = 36


class AppBar(QFrame):
    """顶栏：源文件名（未导入时是应用名）+ 保存状态 + 更换视频。"""

    change_source_clicked = Signal()

    def __init__(self, title: str = _APP_TITLE, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "app-bar")
        self.setFixedHeight(44)
        self._app_title = title

        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "app-bar-title")
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        self.title_label.setFont(font)

        self.save_label = QLabel("")
        self.save_label.setProperty("role", "status-text")

        self.btn_change_source = QPushButton("更换视频")
        self.btn_change_source.setProperty("role", "ghost")
        self.btn_change_source.setToolTip("重新选择源视频（也可直接把文件拖进窗口）")
        self.btn_change_source.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_change_source.clicked.connect(self.change_source_clicked)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 16, 0)
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.save_label)
        layout.addWidget(self.btn_change_source)

    # ---- 状态 ----------------------------------------------------------

    def set_source(self, filename: str | None, path: str | None = None) -> None:
        """导入后标题让位给源文件名；清空（None）则回到应用标题。

        长文件名截断显示，完整路径放 tooltip（方案 §七 要求顶栏可看到路径）。
        """
        if filename:
            shown = filename if len(filename) <= _MAX_TITLE_CHARS else (
                filename[: _MAX_TITLE_CHARS - 1] + "…"
            )
            self.title_label.setText(shown)
            self.title_label.setToolTip(path or filename)
        else:
            self.title_label.setText(self._app_title)
            self.title_label.setToolTip("")

    def set_save_status(self, text: str) -> None:
        """保存状态文案：空 / 「未保存」/「已保存 HH:MM」/「自动保存 HH:MM」。"""
        self.save_label.setText(text)