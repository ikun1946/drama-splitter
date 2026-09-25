"""底栏 —— 单页工作台唯一的常驻状态行。

取代原先并存的三份重复：``status_strip``（源文件路径 + 第 N/M 步）、
``footer``（提示 + 继续按钮）、各页自己的状态标签。底栏一行里放：

    阶段文案 │ 进度（可隐藏） │ 提示（伸展） │ 主动作按钮

主动作文案与角色由 :func:`app.gui.stage.primary_action` 给出，底栏只负责显示；
点击后发 ``action_clicked``，由工作台按当前阶段决定做什么——底栏自己不判断
阶段，避免"按钮文案与实际动作"两处逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

__all__ = ["BottomBar"]


class BottomBar(QFrame):
    action_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "bottom-bar")
        self.setFixedHeight(48)

        self.caption = QLabel("")
        self.caption.setProperty("role", "bottom-caption")

        self.progress = QProgressBar()
        self.progress.setProperty("role", "bottom-progress")
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)

        self.hint = QLabel("")
        self.hint.setProperty("role", "bottom-hint")
        self.hint.setWordWrap(False)

        self.btn_action = QPushButton("")
        self.btn_action.setProperty("role", "primary")
        # QSS 的 min-height 不进 minimumSizeHint：可用高度不足时按钮会被压成
        # 一条细线、文字叠印。显式最小高度是硬保险。
        self.btn_action.setMinimumHeight(32)
        self.btn_action.setMinimumWidth(150)
        self.btn_action.clicked.connect(self.action_clicked)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(12)
        row.addWidget(self.caption)
        row.addWidget(self.progress)
        row.addWidget(self.hint, 1)
        row.addWidget(self.btn_action)

    # ---- 状态 ----------------------------------------------------------

    def set_caption(self, text: str) -> None:
        self.caption.setText(text)

    def set_hint(self, text: str) -> None:
        self.hint.setText(text)

    def set_progress(self, value: int | None) -> None:
        """``None`` 隐藏进度条；否则显示 0–100。"""
        if value is None:
            self.progress.setVisible(False)
            return
        self.progress.setVisible(True)
        self.progress.setValue(max(0, min(100, int(value))))

    def set_action(self, text: str, enabled: bool = True, role: str = "primary") -> None:
        self.btn_action.setText(text)
        self.btn_action.setEnabled(enabled)
        if self.btn_action.property("role") != role:
            # 角色是动态属性，改完必须重新 polish 才生效
            self.btn_action.setProperty("role", role)
            self.btn_action.style().unpolish(self.btn_action)
            self.btn_action.style().polish(self.btn_action)

    def action_text(self) -> str:
        return self.btn_action.text()

    def action_enabled(self) -> bool:
        return self.btn_action.isEnabled()