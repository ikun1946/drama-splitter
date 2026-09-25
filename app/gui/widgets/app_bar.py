"""AppBar —— 顶栏（标题 + stepper + 主题切换 + 更换视频 + 设置/模型）。

方案 §七：顶栏只放「当前文件名 / 路径 / 更换视频」这类与源文件直接相关的
信息，模型状态、GPU、token 一律不放。stepper 是本应用的工作流主轴（四步
向导），保留在中央；标题在导入后让位给源文件名。
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

from .icon_button import IconButton
from .step_indicator import StepIndicator

__all__ = ["AppBar"]

_APP_TITLE = "🎬  AI 短剧分集器"
# 标题区最多显示的字符数：长文件名截断，完整路径放 tooltip
_MAX_TITLE_CHARS = 36


class AppBar(QFrame):
    """顶栏：左侧标题/源文件名 + 中央 stepper + 右侧操作按钮。"""

    step_clicked = Signal(int)        # 用户点击了 stepper 的第 N 项
    settings_clicked = Signal()
    model_clicked = Signal()
    theme_clicked = Signal()          # 用户点了深浅主题切换
    change_source_clicked = Signal()  # 用户点了「更换视频」

    def __init__(
        self,
        title: str,
        steps: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "app-bar")
        # 背景 / 边框 / 标题颜色全部走全局 QSS（role="app-bar" 等），
        # 深浅主题切换时自动跟随，不再 inline 硬编码。
        self.setFixedHeight(56)
        self._app_title = title

        # 左：标题（导入后由 set_source() 换成源文件名）
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "app-bar-title")
        f = QFont()
        f.setBold(True)
        f.setPointSize(11)
        self.title_label.setFont(f)

        # 中：stepper
        self.stepper = StepIndicator(steps)
        self.stepper.clicked.connect(self.step_clicked)

        # 右：主题切换 + 更换视频 + 设置/模型
        self.btn_theme = QPushButton("🌙 深色")
        self.btn_theme.setProperty("role", "ghost")
        self.btn_theme.setToolTip("切换深色 / 浅色主题（选择会记住）")
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_theme.clicked.connect(self.theme_clicked)

        self.btn_change_source = QPushButton("更换视频")
        self.btn_change_source.setProperty("role", "ghost")
        self.btn_change_source.setToolTip("重新选择源视频（也可直接拖入新文件）")
        self.btn_change_source.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_change_source.clicked.connect(self.change_source_clicked)

        self.btn_settings = IconButton("⚙")
        self.btn_settings.setToolTip("设置（参数与可行性）")
        self.btn_settings.clicked.connect(self.settings_clicked)

        self.btn_model = IconButton("🤖")
        self.btn_model.setToolTip("模型管理")
        self.btn_model.clicked.connect(self.model_clicked)

        right_holder = QHBoxLayout()
        right_holder.setContentsMargins(8, 0, 16, 0)
        right_holder.setSpacing(4)
        right_holder.addWidget(self.btn_theme)
        right_holder.addWidget(self.btn_change_source)
        right_holder.addWidget(self.btn_settings)
        right_holder.addWidget(self.btn_model)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.title_label)
        layout.addSpacing(20)
        layout.addWidget(self.stepper, 1)
        layout.addLayout(right_holder)

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

    def set_theme_label(self, mode: str) -> None:
        """按当前主题更新切换按钮文案：显示的是「将要切过去的」那一档。"""
        self.btn_theme.setText("☀ 浅色" if mode == "dark" else "🌙 深色")

    def set_steps(self, steps: list[str]) -> None:
        self.stepper.set_steps(steps)

    def set_step_state(self, current: int, done: set[int]) -> None:
        self.stepper.set_current(current)
        self.stepper.set_done(*done)

    def set_model_notify(self, on: bool) -> None:
        self.btn_model.set_notify(on)
