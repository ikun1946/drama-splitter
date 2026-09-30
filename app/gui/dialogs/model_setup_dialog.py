"""「模型设置」对话框。

承载 ``ModelSetupForm``：语音转写与语义判断各选一个模型，本地 / 云端可混用。
改动落在 ``config.json`` 的 ``asr_model_tier`` / ``llm_model_key`` /
``backend_prefs`` / ``providers`` 上（与「模型管理」读写同一份），因此关掉回到
厂商页时那边必须重新读盘。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QWidget

from ..panes import ModelSetupForm
from ..state import ProjectState
from .base import DialogShell

__all__ = ["ModelSetupDialog"]


class ModelSetupDialog(DialogShell):
    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        # 改版后每张卡只剩一个下拉 + 一句状态，720 高会空掉大半屏
        super().__init__("模型设置", parent, width=880, height=460)
        self.form = ModelSetupForm(state, self, config_path=config_path)
        self.add_body(self.form)
        self.footer_hint.setText(
            "选中即生效：每个下拉里，本机已装的模型排在最上面，云端按厂商分组，"
            "点框可直接输入筛选，也能手输清单外的名字（如方舟 ep-xxx）。\n"
            "本页只选模型：本地模型在「模型管理 → 本地模型」下载，厂商的 Key 与接口地址"
            "在「模型管理 → 云端厂商」填写。云端要真正生效，还需要那份档案测通过并勾选"
            "「允许外发素材」。"
        )
        self.add_close_button()
        self.install_footer()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 拉模型清单是网络请求（每家最长 10 秒）：关窗就取消，别让线程在后台等超时
        self.form.shutdown()
        super().closeEvent(event)
