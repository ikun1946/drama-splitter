"""GUI 复用控件。

包含：
- Card / LabeledField / StatusPill    —— 区段容器与状态展示
- IssueList                            —— 兼容性问题表（代理原 QTableWidget 行为）
- LogPanel                             —— 风格化日志面板
- StepIndicator / StepNav              —— 步骤条与侧栏导航
- MediaPreview                         —— 帧级预览（HUD + 时间码）
- Drawer                               —— 右侧滑入抽屉（浮层，用于设置 / 模型）
- Inspector                            —— 右侧常驻检查器（问题 / 建议 / 日志）
"""

from .app_bar import AppBar
from .card import Card
from .drawer import Drawer
from .drop_zone import DropZone
from .icon_button import IconButton
from .inspector import Inspector, InspectorIssue
from .issue_list import IssueList
from .labeled_field import LabeledField
from .log_panel import LogPanel
from .media_preview import MediaPreview
from .status_pill import StatusPill
from .step_indicator import StepIndicator
from .step_nav import StepNav, StepState

__all__ = [
    "AppBar",
    "Card",
    "Drawer",
    "DropZone",
    "IconButton",
    "Inspector",
    "InspectorIssue",
    "IssueList",
    "LabeledField",
    "LogPanel",
    "MediaPreview",
    "StatusPill",
    "StepIndicator",
    "StepNav",
    "StepState",
]