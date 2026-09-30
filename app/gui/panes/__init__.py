"""界面分区（pane）与表单（form）。

单页工作台之后不再有"页面"概念：整窗由三栏组成，每一栏在四个阶段各显示一屏。
本包放下这些可独立使用的分区与表单：

- ``import_pane.py``   左栏·素材（源文件 / 媒体摘要 / 主对白音轨）
- ``review_pane.py``   左栏·分集审核（集列表 / 当前集 / 切点操作）
- ``analysis_pane.py`` 中栏·分析进度（阶段 / 进度 / 最近输出）
- ``settings_form.py`` / ``model_form.py`` / ``export_form.py`` / ``provider_form.py``
  / ``model_setup_form.py`` 五个表单，分别由 ``app/gui/dialogs/`` 下的对话框承载

三栏装配与阶段驱动在 ``app/gui/workbench.py``；舞台（预览 + 时间轴 + 播放）
是 ``app/gui/widgets/stage_view.py``。

约定
----
- 分区通过构造函数接收 ``ProjectState``，不直接持有 MainWindow
- 分区只通过**公开信号**向上汇报，不直接改方案模型（改动集中在工作台，
  以便统一翻译成检查器问题与时间轴标红）
- 需要登记后台任务时用 ``_common.register_worker``（沿 parent 链上溯）
"""

from __future__ import annotations

from ._common import (
    LEVEL_COLOR,
    LEVEL_TEXT,
    register_worker,
    scrollable,
)
from .analysis_pane import AnalysisPane
from .export_form import ExportForm, export_precheck
from .import_pane import ImportPane
from .model_form import ModelForm
from .model_setup_form import ModelSetupForm
from .provider_form import ProviderForm
from .review_pane import ReviewPane
from .settings_form import SettingsForm

__all__ = [
    "AnalysisPane",
    "ExportForm",
    "ImportPane",
    "ModelForm",
    "ModelSetupForm",
    "ProviderForm",
    "ReviewPane",
    "SettingsForm",
    "export_precheck",
    "LEVEL_COLOR",
    "LEVEL_TEXT",
    "register_worker",
    "scrollable",
]