"""AppBar —— 顶栏（标题 + stepper + 右上图标按钮）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from .icon_button import IconButton
from .step_indicator import StepIndicator
from ..theme import FONT_FAMILY, FONT_SYMBOL


class AppBar(QFrame):
    """顶栏：左侧标题 + 中央 stepper + 右侧图标按钮。"""

    step_clicked = Signal(int)        # 用户点击了 stepper 的第 N 项
    settings_clicked = Signal()
    model_clicked = Signal()

    def __init__(
        self,
        title: str,
        steps: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "app-bar")
        self.setStyleSheet(
            "QFrame[role='app-bar'] { background: #ffffff; border-bottom: 1px solid #d9dce4; }"
        )
        self.setFixedHeight(56)

        # 左：标题
        self.title_label = QLabel(title)
        f = QFont()
        f.setBold(True)
        f.setPointSize(11)
        self.title_label.setFont(f)
        self.title_label.setStyleSheet(
            f"font-family: {FONT_SYMBOL}, {FONT_FAMILY};"
            " color: #20232a; padding: 0 16px;"
        )

        # 中：stepper
        self.stepper = StepIndicator(steps)
        self.stepper.clicked.connect(self.step_clicked)

        # 右：图标按钮
        self.btn_settings = IconButton("⚙")
        self.btn_settings.setToolTip("设置（参数与可行性）")
        self.btn_settings.clicked.connect(self.settings_clicked)

        self.btn_model = IconButton("🤖")
        self.btn_model.setToolTip("模型管理")
        self.btn_model.clicked.connect(self.model_clicked)

        right_holder = QHBoxLayout()
        right_holder.setContentsMargins(8, 0, 16, 0)
        right_holder.setSpacing(4)
        right_holder.addWidget(self.btn_settings)
        right_holder.addWidget(self.btn_model)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.title_label)
        layout.addSpacing(20)
        layout.addWidget(self.stepper, 1)
        layout.addLayout(right_holder)

    def set_steps(self, steps: list[str]) -> None:
        self.stepper.set_steps(steps)

    def set_step_state(self, current: int, done: set[int]) -> None:
        self.stepper.set_current(current)
        self.stepper.set_done(*done)

    def set_model_notify(self, on: bool) -> None:
        self.btn_model.set_notify(on)