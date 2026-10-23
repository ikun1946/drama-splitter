"""导出对话框。

承载 ``ExportForm``（导出概览 / 输出目录 / 进阶编码 / 队列 / 进度 / 重试），并承接
§37 导出前置检查的**决策点**：``request_precheck`` 是唯一可覆写的方法，
测试与离屏冒烟覆写它来模拟用户选择，而不是去弹一个会永久阻塞的模态框。

选「返回审核」时：关闭对话框并发出 ``review_requested(集号, 毫秒)``，
由主窗口负责把界面带回审核态并定位到问题切点。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ..panes import ExportForm
from ..panes.export_form import precheck_prompt
from ..state import ProjectState
from ..widgets import message
from .base import DialogShell

__all__ = ["ExportDialog"]


class ExportDialog(DialogShell):
    review_requested = Signal(int, int)   # (集号, 毫秒位置)；集号 0 表示不定位

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__("导出", parent, width=1120, height=900)
        self.state = state

        self.form = ExportForm(state, self, precheck=self.request_precheck)
        self.form.review_requested.connect(self._on_review_requested)

        self.add_body(self.form)
        self.form.refresh()
        self.footer_hint.setText(
            "默认原生编码：视频与音频不重新编码，画质、音质和码率保持源片不变；"
            "事务式落盘，失败可单独重试。"
        )
        # 关闭按钮由表单底部的操作行承担：「取消」在空闲时关闭对话框，
        # 避免同一屏出现两个语义相近的“取消/关闭”。
        self.form.close_requested.connect(self.close)
        self.install_footer()

    # ---- §37 前置检查 --------------------------------------------------

    def request_precheck(self, warnings: list, unreviewed: int) -> str:
        """返回 ``"continue"``（继续导出）或 ``"review"``（返回审核）。

        覆写这个方法即可在自动化环境里跳过模态框——实例级 ``exec()`` 拦不到，
        离屏跑链路时会永久阻塞（实测挂过 25 分钟）。
        """
        agreed = message.confirm_continue(
            self,
            "导出前置检查",
            precheck_prompt(warnings, unreviewed),
            "继续导出",
            "返回审核",
        )
        return "continue" if agreed else "review"

    def _on_review_requested(self, episode: int, ms: int) -> None:
        self.close()
        self.review_requested.emit(episode, ms)

    # ---- 执行 ----------------------------------------------------------

    def start_export(self, only: list[int] | None = None) -> None:
        """开始导出（供主窗口的菜单项与 Ctrl+E 调用）。"""
        self.form.start_export(only)
