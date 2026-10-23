"""模型管理对话框：本地模型 / 云端模型，一个界面两个页签。

为什么合并
----------
云端厂商的 Key、外发许可与连接参数原本分散在「模型管理」的云端卡片和
「厂商管理」对话框里——同一份档案两处都能改，用户实测就撞上了（"为什么模型管理
里要填 Key，厂商管理那里不就重复了"）。合并之后：

- **本地模型**页签：whisper 档位 / GGUF 的下载、删除、「使用中」（原样搬过来）；
- **云端模型**页签：选厂商、填 Key、保存并测试连接。地址、协议、模型清单与
  参数都在「进阶设置」里；**具体用途与模型不在这里选**，统一去「模型设置」。

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
        # 首屏只保留连接配置，不需要 1320 的双栏宽度；1120 在 1366×768
        # 笔记本上也留得下标题栏与完整动作区。
        super().__init__("模型管理", parent, width=1120, height=760)
        self.setMinimumSize(1120, 640)
        self.tabs = QTabWidget(self)
        self.form = ModelForm(state, self.tabs, config_path=config_path)
        self.tabs.addTab(scrollable(self.form), "本地模型")
        self.providers = ProviderForm(state, self.tabs, config_path=config_path)
        self.tabs.addTab(self.providers, "云端模型")
        if providers_tab:
            self.tabs.setCurrentIndex(1)
        self.add_body(self.tabs)
        self.footer_hint.setText(
            "本地模型按需下载。云端模型页负责厂商连接：内置厂商填 Key；"
            "自建或聚合服务点「自定义接入」，填写 API 地址与密钥并选择协议。"
            "具体用途与模型请在「工具 → 模型设置…」中选择。"
        )
        self.add_close_button()
        self.install_footer()
