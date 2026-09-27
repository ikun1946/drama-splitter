"""模型管理对话框：本地模型 / 云端厂商，一个界面两个页签。

为什么合并
----------
云端厂商的 Key、外发许可、能力开关与模型名原本分散在「模型管理」的云端卡片和
「厂商管理」对话框里——同一份档案两处都能改，用户实测就撞上了（"为什么模型管理
里要填 Key，厂商管理那里不就重复了"）。合并之后：

- **本地模型**页签：whisper 档位 / GGUF 的下载、删除、「使用中」（原样搬过来）；
- **云端厂商**页签：左列表选厂商，右侧填 Key、看/选模型（`/models` 自动刷新 +
  手填可记住）、勾两条能力并「保存并测试连通性」。地址、协议、模型名只在这里改。

``form`` 仍是本地模型表单（老代码与测试照旧），厂商表单在 ``providers``。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QTabWidget, QWidget

from ..panes import ModelForm, ProviderForm, scrollable
from ..state import ProjectState
from .base import DialogShell

__all__ = ["ModelDialog"]


class ModelDialog(DialogShell):
    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path: Path | None = None,
        providers_tab: bool = False,
    ) -> None:
        super().__init__("模型管理", parent, width=1000, height=720)
        self.tabs = QTabWidget(self)
        self.form = ModelForm(state, self.tabs, config_path=config_path)
        self.tabs.addTab(scrollable(self.form), "本地模型")
        self.providers = ProviderForm(state, self.tabs, config_path=config_path)
        self.tabs.addTab(self.providers, "云端厂商")
        if providers_tab:
            self.tabs.setCurrentIndex(1)
        self.add_body(self.tabs)
        self.footer_hint.setText(
            "本地模型不随程序分发，按需下载；云端厂商的 Key 明文存 config.json，"
            "建议用环境变量。勾了「云端能力」后必须「保存并测试连通性」通过，"
            "那条能力才会真正切到云端。"
        )
        self.add_close_button()
        self.install_footer()