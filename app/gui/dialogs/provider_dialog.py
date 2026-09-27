"""厂商管理对话框。

承载 ``ProviderForm``（新增 / 编辑 / 复制内置 / 删除 / 恢复默认）。改动一律写进
``config.json`` 的 ``providers`` 字段——与「模型管理」里的云端卡片读写的是同一份，
因此关掉本对话框回到模型页时，那边必须重新读盘（``ModelForm.reload_after_provider_edit``）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QWidget

from ..panes import ProviderForm
from ..state import ProjectState
from .base import DialogShell

__all__ = ["ProviderDialog"]


class ProviderDialog(DialogShell):
    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__("厂商管理", parent, width=960, height=680)
        self.form = ProviderForm(state, self, config_path=config_path)
        # 表单自己就是左右分栏（左列表 / 右编辑区带滚动），因此不再套一层滚动容器
        self.add_body(self.form)
        self.footer_hint.setText(
            "内置厂商可改可恢复默认、不可删；删除或恢复会把它承担的能力回落本地。"
            "API Key 明文存 config.json，建议用环境变量。"
        )
        self.add_close_button()
        self.install_footer()