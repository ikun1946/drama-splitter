"""「选择云端模型」对话框。

承载 ``ModelPickForm``：只列"已配 Key 且能连通"的厂商，悬停看模型清单、下拉选
一个、保存写回档案。改动落在 ``config.json`` 的 ``providers`` 字段（与「厂商
管理」「模型管理」读写同一份），因此关掉回到模型页时那边必须重新读盘。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QWidget

from ..panes import ModelPickForm
from ..state import ProjectState
from .base import DialogShell

__all__ = ["ModelPickDialog"]


class ModelPickDialog(DialogShell):
    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__("选择云端模型", parent, width=760, height=520)
        self.form = ModelPickForm(state, self, config_path=config_path)
        self.add_body(self.form)
        self.footer_hint.setText(
            "列表里只有已配好 API Key 且能连通的厂商（打开时实时拉一次各家 "
            "/models）；悬停厂商项可看它的全部模型。本页只选模型名——是否允许外发"
            "素材、是否切到云端，仍在「模型管理 → 云端厂商」里决定。"
        )
        self.add_close_button()
        self.install_footer()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 探测是网络请求（每家最长 10 秒）：关窗就取消，别让线程在后台等超时
        self.form.shutdown()
        super().closeEvent(event)