"""分集设置对话框。

承载 ``SettingsForm``（参数控件 + 可行性实时提示），并补上原先缺的语义：
**「取消」即回滚**。表单是实时写回 ``state.settings`` 的（``collect_settings``
每次 ``_refresh`` 都会调），用户试着调参时状态已被改动，所以必须显式回滚，
否则"取消"其实是"保留试出来的值"。
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QWidget

from ...core.settings import SplitSettings
from ..panes import SettingsForm, scrollable
from ..state import ProjectState
from .base import DialogShell

__all__ = ["SettingsDialog"]


class SettingsDialog(DialogShell):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__("分集设置", parent, width=980, height=720)
        self.state = state

        self.form = SettingsForm(state, self)
        # 控件是参数的唯一录入面：打开时必须把控件同步到当前参数。不同步的话
        # 控件停在默认值，而任何一次信号触发都会用控件值覆盖 state（实测：
        # 打开项目恢复的参数会被"开一次设置对话框"悄悄改回默认）。
        self.form.apply_settings(state.settings)
        self._snapshot = state.settings.to_json()

        self.add_body(scrollable(self.form))

        self.footer_hint.setText("参数立即参与可行性检查；「取消」会恢复打开时的参数。")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setMinimumHeight(32)
        self.btn_cancel.clicked.connect(self.cancel_and_close)
        self.btn_apply = QPushButton("保存并关闭")
        self.btn_apply.setProperty("role", "primary")
        self.btn_apply.setMinimumHeight(32)
        self.btn_apply.clicked.connect(self.apply_and_close)
        self.add_footer(self.btn_cancel)
        self.add_footer(self.btn_apply)
        self.install_footer()

    # ---- 提交 / 回滚 ---------------------------------------------------

    def apply_and_close(self) -> None:
        """提交当前参数并关闭（「保存并关闭」按钮与自动化脚本都走这里）。"""
        self.form.collect_settings()
        self._snapshot = self.state.settings.to_json()
        self.accept()

    def cancel_and_close(self) -> None:
        self._rollback()
        self.close()

    def _rollback(self) -> None:
        """把参数恢复成打开对话框时的快照。重复调用是安全的。"""
        self.form.apply_settings(SplitSettings.from_json(self._snapshot))
        self._snapshot = self.state.settings.to_json()

    def reject(self) -> None:  # noqa: N802 - Qt 命名
        """Esc 与窗口关闭按钮也走回滚——"取消"的语义要一致。"""
        self._rollback()
        super().reject()