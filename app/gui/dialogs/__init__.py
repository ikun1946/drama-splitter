"""对话框。

设置 / 模型（本地 + 云端模型）/ 模型设置 / 导出不再是工作流里的一"页"，而是按需
打开、用完关掉的对话框。它们共享 ``base.DialogShell`` 的版式与同一条自动化约定：

    生产代码只在菜单/按钮处理器里 exec()；测试、冒烟、截图只走
    ``MainWindow.build_*_dialog() + show()``。
"""

from __future__ import annotations

from .base import DialogShell
from .env_check_dialog import EnvCheckDialog
from .export_dialog import ExportDialog
from .model_dialog import ModelDialog
from .model_setup_dialog import ModelSetupDialog
from .settings_dialog import SettingsDialog
from .update_dialog import UpdateDialog

__all__ = [
    "DialogShell",
    "EnvCheckDialog",
    "ExportDialog",
    "ModelDialog",
    "ModelSetupDialog",
    "SettingsDialog",
    "UpdateDialog",
]