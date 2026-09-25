"""页 1 分集工作台（方案 §八）。

导入与分析合并成一页：用户 80% 的操作都发生在这里。

版式
----
::

    ┌────────────────────────────┬──────────┐
    │ 导入区（源文件/媒体信息/音轨/兼容性）│ 检查器   │
    ├────────────────────────────┤ 问题/建议 │
    │ 分析区（阶段进度 + 日志）            │ /日志    │
    └────────────────────────────┴──────────┘

上半放 ImportPage，下半放 AnalysisPage，右侧常驻 Inspector。
导入是分析的前置条件，两者本就顺序衔接，分开反而让用户来回切页。

属性转发
--------
ImportPage / AnalysisPage 的所有公共属性与方法都通过 ``__getattr__`` 转发，
因此下列两种写法等价，既有测试与脚本无须改动：

- ``window.workbench_page.issues``
- ``window.import_page.issues``          （``import_page`` 指向本页）

转发顺序：先查本页自身 → 再查 analysis → 最后查 import。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget
from PySide6.QtCore import Qt

from ..state import ProjectState
from ..widgets import Inspector
from ._common import scrollable
from .analysis_page import AnalysisPage
from .import_page import ImportPage

__all__ = ["WorkbenchPage"]


class WorkbenchPage(QWidget):
    """导入 + 分析合并页，右侧常驻检查器。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state

        # 两个既有页面原样复用，不复制任何逻辑
        self.import_view = ImportPage(state, self)
        self.analysis_view = AnalysisPage(state, self)

        # ---- 左：导入（上）+ 分析（下） ----
        #
        # 两半都套一层滚动容器：QSplitter 的约束来自子控件的最小高度，
        # 而导入区的四张卡片叠起来最小高度接近 700px —— 直接放进分割器时
        # 两个子控件的最小高度之和超过可视高度，Qt 会把 setSizes / 拖动手柄
        # 全部忽略（实测 sizes() 恒等于均分，moveSplitter() 直接返回 None）。
        # 装上滚动容器后最小高度不再由内容撑开，分割器才真正可调。
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(scrollable(self.import_view))
        self.splitter.addWidget(scrollable(self.analysis_view))
        # 导入是短流程、分析区日志要长，给后者更多空间
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)

        # ---- 右：检查器 ----
        self.inspector = Inspector(self)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.splitter, 1)
        body.addWidget(self.inspector)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(body)

        # 分析阶段的日志同时写进检查器的日志页，用户不必来回看。
        # 走 _append 的公开出口而不是信号：_append 是本页所有日志的唯一落点，
        # 补一个镜像调用即可，不动 AnalysisPage 内部。
        self._original_append = self.analysis_view._append
        self.analysis_view._append = self._append_with_mirror

        # 上下两区在一个 QSplitter 里，默认同屏可见；但导入区内容较高时下半会被
        # 压到折叠线以下，用户点完导入要先往下拉才看得到分析进度。这里在分析
        # 启动 / 进入新阶段时把分隔条推到分析区，让视线自动跟上下一个阶段。
        self._original_stage = self.analysis_view._on_stage
        self.analysis_view._on_stage = self._on_stage_with_scroll
        self._original_start = self.analysis_view._start
        self.analysis_view._start = self._start_with_scroll

    # ---- 自动跟随 ------------------------------------------------------

    def _focus_analysis(self) -> None:
        """把分隔条压到分析区顶部，确保下半区完整可见。"""
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        import_top = max(0, total - int(total * 0.62))
        self.splitter.setSizes([import_top, total - import_top])

    def _start_with_scroll(self) -> None:
        self._original_start()
        self._focus_analysis()

    def _on_stage_with_scroll(self, name: str) -> None:
        self._original_stage(name)
        self._focus_analysis()

    # ---- 属性转发 ------------------------------------------------------

    @property
    def import_worker(self):
        """探针线程（等价于原 ImportPage._worker）。"""
        return self.import_view._worker

    @property
    def analysis_worker(self):
        """分析线程（等价于原 AnalysisPage._worker）。

        注意：``_worker`` 转发给的是分析线程，因为既有代码（主窗口的「继续」
        按钮逻辑）用 ``analysis_page._worker`` 判断分析是否在跑。探针线程请用
        ``import_worker``。
        """
        return self.analysis_view._worker

    def __getattr__(self, name: str):
        """把未知属性转发给内部两个视图。

        只在 ``__init__`` 完成后生效；构造期间访问自身属性不会被劫持。
        analysis 先于 import：两者若有同名成员，分析区的是更常被外部使用的那批
        （``_worker`` / ``_start`` / ``log`` / ``btn_start``）。
        """
        for attr in ("analysis_view", "import_view"):
            view = self.__dict__.get(attr)
            if view is not None and hasattr(view, name):
                return getattr(view, name)
        raise AttributeError(f"{type(self).__name__!r} 对象没有属性 {name!r}")

    # ---- 行为 ----------------------------------------------------------

    def _append_with_mirror(self, text: str, level: str = "info") -> None:
        """分析页日志落两处：本页 LogPanel + 检查器日志页。"""
        self._original_append(text, level)
        self.inspector.append_log(text, level)

    # ---- 兼容既有导航 ------------------------------------------------

    def can_continue(self) -> tuple[bool, str]:
        """进入下一阶段前的前置检查。

        工作台同时承担导入与分析两职：先要求导入可继续（媒体已就绪、无阻断），
        再要求分析已产出方案。任一不满足即返回对应提示。
        """
        ok, message = self.import_view.can_continue()
        if not ok:
            return ok, message
        return self.analysis_view.can_continue()
