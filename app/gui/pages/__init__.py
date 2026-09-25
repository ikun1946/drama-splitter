"""页面层。

原先把六个页类都塞在 ``main_window.py`` 里（2262 行），拆成每页一个文件后
``main_window.py`` 只保留 MainWindow 与装配逻辑。这是纯搬运，页类逻辑未改动。

约定
----
- 每个页类一个文件，文件名用 ``<名字>_page.py``
- 工作台（``workbench_page.py``）组合 ImportPage 与 AnalysisPage，
  通过 ``__getattr__`` 转发两者接口，因此两者仍可单独使用
- 跨页共享的符号放 ``_common.py``，不要互引页文件
- 页类通过构造函数接收 ``ProjectState``，不直接持有 MainWindow
- 需要登记后台任务时用 ``_common.register_worker``（拿不到窗口就静默跳过）
"""

from __future__ import annotations

from ._common import (
    LEVEL_COLOR,
    LEVEL_TEXT,
    FormSection,
    hint_label,
    register_worker,
    scrollable,
    section_label,
)
from .analysis_page import AnalysisPage
from .export_page import ExportPage
from .import_page import ImportPage
from .model_page import ModelPage
from .review_page import ReviewPage
from .settings_page import SettingsPage
from .workbench_page import WorkbenchPage

__all__ = [
    "AnalysisPage",
    "ExportPage",
    "ImportPage",
    "ModelPage",
    "ReviewPage",
    "SettingsPage",
    "WorkbenchPage",
    "FormSection",
    "LEVEL_COLOR",
    "LEVEL_TEXT",
    "hint_label",
    "register_worker",
    "scrollable",
    "section_label",
]
