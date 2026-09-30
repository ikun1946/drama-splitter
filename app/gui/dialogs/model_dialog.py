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
        # 1320 而不是 1000：云端厂商页是「左列表 240 + 右编辑区」两栏，编辑区里
        # 一行要并排放 LabeledField + 复选框 + 按钮，量出来的最小宽度约 975px。
        # 对话框只有 1000 时编辑区只剩 698，**保存并测试按钮、允许外发素材复选框、
        # 模型表格右半边全部被裁掉**。这个宽度和主窗口的最小宽度对齐。
        super().__init__("模型管理", parent, width=1320, height=760)
        # 最小宽度跟住初始宽度：``QDialog`` 默认可缩到外壳的 720px，那样左列表和
        # 编辑区都会被压扁、控件被裁。与其让用户拖出一个坏界面，不如不让拖。
        self.setMinimumSize(1320, 640)
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