"""GUI 复用控件。

包含：
- Card / LabeledField / StatusPill    —— 区段容器与状态展示
- ModelTable                           —— 厂商模型清单表格（指派 / 手填 / 删除）
- LogPanel                             —— 风格化日志面板
- MediaPreview                         —— 帧级预览（HUD + 时间码）
- TimelineRuler                        —— 轻量时间轴（切点 / 播放头 / 拖拽）
- StageView                            —— 中栏舞台（预览 + 时间轴 + 播放控制）
- DropZone                             —— 拖放导入区
- BottomBar                            —— 单页工作台唯一的底部状态行
- Inspector                            —— 右侧常驻检查器（问题 / 建议 / 日志）
- NoWheel*                             —— 滚轮不改值的数值框 / 下拉框
- scrollable / h_scrollable            —— 滚动容器助手
- message                              —— 统一弹窗出口（子模块，用 message.xxx 调用）
"""

from .app_bar import AppBar
from .bottom_bar import BottomBar
from .card import Card
from .drop_zone import DropZone
from .inspector import Inspector, InspectorIssue
from .labeled_field import LabeledField
from .log_panel import LogPanel
from .media_preview import MediaPreview
from .model_table import ModelTable
from .no_wheel import NoWheelComboBox, NoWheelDoubleSpinBox, NoWheelSpinBox
from .scroll_area import h_scrollable, scrollable
from .stage_view import StageView
from .status_pill import StatusPill
from .timeline import TimelineRuler

__all__ = [
    "AppBar",
    "BottomBar",
    "Card",
    "DropZone",
    "Inspector",
    "InspectorIssue",
    "LabeledField",
    "LogPanel",
    "MediaPreview",
    "ModelTable",
    "NoWheelComboBox",
    "NoWheelDoubleSpinBox",
    "NoWheelSpinBox",
    "StageView",
    "StatusPill",
    "TimelineRuler",
    "h_scrollable",
    "scrollable",
]