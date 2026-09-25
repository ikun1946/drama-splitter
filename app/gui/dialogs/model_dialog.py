"""模型管理对话框。

承载 ``ModelForm``（清单 / 实测体积 / 磁盘预检 / 下载与删除）。下载线程由
表单自己持有，``register_worker`` 会沿 parent 链上溯到主窗口登记，因此
"任务进行中不允许换片/退出"这些既有保护照旧生效。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..panes import ModelForm, scrollable
from ..state import ProjectState
from .base import DialogShell

__all__ = ["ModelDialog"]


class ModelDialog(DialogShell):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__("模型管理", parent, width=980, height=700)
        self.form = ModelForm(state, self)
        self.add_body(scrollable(self.form))
        self.footer_hint.setText("模型不随程序分发，按需下载；目录与删除只影响所选模型。")
        self.add_close_button()
        self.install_footer()