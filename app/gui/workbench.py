"""单页工作台：一页完成 导入 → 分析 → 审核，右侧常驻检查器。

版式：:

    ┌────────────┬──────────────────────────────┬────────────┐
    │ 左栏 320    │ 中栏（自适应）                 │ 右栏 300    │
    │ 素材        │ 空态 hero / 预览+时间轴 / 分析  │ 检查器      │
    │ 或 集列表    │ 进度（三屏随阶段切换）          │ 问题/建议/日志│
    └────────────┴──────────────────────────────┴────────────┘

为什么没有"页面切换"
--------------------
四个阶段（未导入 / 就绪 / 分析中 / 审核）由 :mod:`app.gui.stage` 依据项目
状态推导，界面只是跟着状态换屏。原先那套"页面 + 抽屉 + 步骤条 + 状态条"
要用户自己判断该去哪一页，四层 chrome 还各报一遍进度。

职责边界
--------
- 面板（``ImportPane`` / ``AnalysisPane`` / ``ReviewPane`` / ``StageView``）
  只管显示与发信号，**不直接改方案模型**；
- 改模型（移动切点、锁定、确认）集中在本类：它们要把返回值翻译成检查器问题、
  时间轴标红与底栏提示，这是编排，放在面板里会让同一动作有三份实现。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.probe import check_compatibility
from .panes import AnalysisPane, ImportPane, ReviewPane, scrollable
from .stage import Stage, current_stage
from .state import ProjectState
from .widgets import DropZone, Inspector, InspectorIssue, StageView
from .widgets import message

__all__ = ["Workbench"]


class _EmptyView(QWidget):
    """未导入源视频时的中栏：拖放 hero。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drop_zone = DropZone()
        hint = QLabel("拖入视频文件，或点右下角「选择源视频…」")
        hint.setProperty("role", "subtitle")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.setSpacing(12)
        outer.addStretch(1)
        outer.addWidget(self.drop_zone)
        outer.addWidget(hint)
        outer.addStretch(2)


class Workbench(QWidget):
    stage_changed = Signal(object)     # Stage：底栏文案与主动作据此刷新
    state_changed = Signal()           # 方案或选择被改动（自动保存的脏检测读快照）
    hint_changed = Signal(str)         # 底栏提示（校验失败、非法操作原因）

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.stage = Stage.EMPTY
        self.selected_episode = 1
        # 上一次被阻止的操作留下的问题（例如非法拖拽切点）。它会一直挂在
        # 检查器里直到出现一次成功操作——用户需要看到"为什么没生效"。
        self._action_issues: list[InspectorIssue] = []

        self.import_pane = ImportPane(state, self)
        self.review_pane = ReviewPane(state, self)
        self.analysis_pane = AnalysisPane(state, self)
        self.stage_view = StageView(state, self)
        self.inspector = Inspector(self)
        self.empty_view = _EmptyView(self)

        # 三栏各自一个 stack：换的是"屏"，不是"页"
        self._left_import = scrollable(self.import_pane)
        self._left_review = scrollable(self.review_pane)
        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(self._left_import)
        self.left_stack.addWidget(self._left_review)

        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(self.empty_view)
        self.center_stack.addWidget(self.stage_view)
        self.center_stack.addWidget(self.analysis_pane)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.left_stack)
        self.splitter.addWidget(self.center_stack)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([320, 820, 300])

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.splitter)

        self._connect()

    # ---- 接线 ----------------------------------------------------------

    def _connect(self) -> None:
        # 素材：探针完成 → 重判阶段；拖放 hero 与「更换视频」都走 probe()
        self.import_pane.probed.connect(lambda *_: self.refresh())
        self.import_pane.probe_failed.connect(lambda *_: self.refresh())
        self.empty_view.drop_zone.clicked.connect(self.import_pane.choose_file)
        self.empty_view.drop_zone.file_dropped.connect(self.import_pane.probe)

        # 分析：开始/结束/阶段变化都要重判阶段；日志与进度广播给右栏与中栏
        self.analysis_pane.started.connect(lambda: self.refresh(inspector_tab=2))
        self.analysis_pane.finished.connect(lambda: self.refresh())
        self.analysis_pane.stage_changed.connect(lambda *_: self.refresh())
        self.analysis_pane.log_appended.connect(self.inspector.append_log)

        # 左栏：选集、切点操作、锁定、确认
        self.review_pane.episode_selected.connect(self._on_episode_selected)
        self.review_pane.boundary_set_requested.connect(self._on_set_boundary)
        self.review_pane.lock_requested.connect(self._on_lock)
        self.review_pane.confirm_requested.connect(self._on_confirm)
        self.review_pane.snap_requested.connect(
            lambda: self.stage_view.goto(self.stage_view.position)
        )

        # 中栏：时间轴拖拽 / 点选 / 播放头
        self.stage_view.boundary_move_requested.connect(self._on_boundary_move)
        self.stage_view.episode_activated.connect(self._on_timeline_episode)
        self.stage_view.position_changed.connect(self.review_pane.set_position)

        # 检查器「查看」→ 定位（由主窗口负责把工作台带到前台）
        self.inspector.jump_requested.connect(self._on_jump_requested)

    # ---- 阶段驱动 ------------------------------------------------------

    def current_stage(self) -> Stage:
        return current_stage(self.state, self.analysis_pane.is_active())

    def refresh(self, *, inspector_tab: int | None = None) -> None:
        """重判阶段并让三栏显示对应内容。所有状态变化的唯一刷新入口。"""
        self.stage = self.current_stage()

        if self.stage is Stage.REVIEW:
            self.left_stack.setCurrentWidget(self._left_review)
            self.review_pane.refresh()
        else:
            self.left_stack.setCurrentWidget(self._left_import)
        self.left_stack.setVisible(self.stage is not Stage.EMPTY)

        if self.stage is Stage.EMPTY:
            self.center_stack.setCurrentWidget(self.empty_view)
        elif self.stage is Stage.ANALYZING:
            self.center_stack.setCurrentWidget(self.analysis_pane)
        else:
            self.center_stack.setCurrentWidget(self.stage_view)
            self.stage_view.set_transport_enabled(True)
            self.stage_view.set_project(
                self.selected_episode if self.stage is Stage.REVIEW else None
            )

        self.refresh_inspector()
        if inspector_tab is not None:
            self.inspector.set_tab(inspector_tab)
        self.stage_changed.emit(self.stage)

    def refresh_inspector(self) -> None:
        """兼容性问题、方案层校验与整集复核风险，统一进右栏（不再各处重复列）。"""
        issues: list[InspectorIssue] = []
        state = self.state

        if state.media is not None and state.binaries is not None:
            for issue in check_compatibility(state.media, state.binaries):
                issues.append(
                    InspectorIssue(
                        title=issue.message,
                        detail=issue.detail or "",
                        priority="P0" if issue.is_blocking else "P1",
                        key=f"compat:{issue.code}",
                    )
                )

        plan = state.current_plan
        if plan is not None:
            for problem in plan.validate(state.settings, None):
                issues.append(
                    InspectorIssue(
                        title=problem.describe(),
                        priority="P0" if problem.is_blocking else "P1",
                        episode=problem.episode,
                        target_ms=self._episode_end_ms(plan, problem.episode),
                        key=f"plan:{problem.code}:{problem.episode}",
                    )
                )

        for review in state.episode_reviews or []:
            for risk in review.risks:
                issues.append(
                    InspectorIssue(
                        title=risk,
                        priority="P0" if review.grade == "block" else "P1",
                        episode=review.episode,
                        target_ms=self._episode_end_ms(plan, review.episode),
                        key=f"review:{review.episode}",
                    )
                )

        issues.extend(self._action_issues)
        self.inspector.set_issues(issues)

    @staticmethod
    def _episode_end_ms(plan, episode: int | None) -> int | None:
        if plan is None or not episode:
            return None
        episodes = plan.episodes()
        if not 1 <= episode <= len(episodes):
            return None
        return int(float(episodes[episode - 1].end_seconds) * 1000)

    # ---- 交互编排 ------------------------------------------------------

    def _on_episode_selected(self, index: int) -> None:
        self.selected_episode = index
        self.stage_view.set_selected_episode(index)
        plan = self.state.current_plan
        if plan is not None and 1 <= index <= plan.episode_count:
            self.stage_view.goto(plan.episodes()[index - 1].start_seconds)
        self.state_changed.emit()

    def _on_timeline_episode(self, index: int) -> None:
        if index:
            self.selected_episode = index
            self.review_pane.set_current(index)
            self.state_changed.emit()

    def _on_set_boundary(self, index: int) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None:
            return
        if index >= plan.episode_count:
            message.information(self, "末集", "末集终点为片尾，不可移动。")
            return
        ticks = plan.time_base.seconds_to_ticks(self.stage_view.position)
        self._apply_boundary_move(index, ticks)

    def _on_boundary_move(self, index: int, ticks: int) -> None:
        """时间轴拖拽松手（已吸附到帧起点、已换算成 ticks）。"""
        self._apply_boundary_move(index, ticks)

    def _apply_boundary_move(self, index: int, ticks: int) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        problems = plan.move_boundary(index, ticks)
        blocking = [p for p in problems if p.is_blocking]
        if blocking:
            # move_boundary 越界时提前返回且不写模型，因此这里无需回滚
            reason = blocking[0].describe()
            self._action_issues = [
                InspectorIssue(
                    title=problem.describe(),
                    detail="该移动被阻止，方案未被修改。",
                    priority="P0",
                    episode=problem.episode,
                    key=f"action:{problem.code}",
                )
                for problem in blocking
            ]
            self.stage_view.timeline.set_error(index, reason)
            self.refresh_inspector()
            self.inspector.set_tab(0)
            self.hint_changed.emit(reason + "（方案未被修改）")
            message.warning(
                self,
                "该切点不可提交",
                "；".join(p.describe() for p in blocking) + "\n\n方案未被修改。",
            )
            return

        plan.source = "manual"
        self._action_issues = []
        self.stage_view.timeline.clear_error()
        self.stage_view.set_project(self.selected_episode)
        self.review_pane.refresh()
        self.refresh_inspector()
        self.hint_changed.emit("")
        self.state_changed.emit()

    def _on_lock(self, index: int, locked: bool) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        plan.lock_boundary(index, locked)
        self.stage_view.set_project(self.selected_episode)
        self.review_pane.refresh()
        self.state_changed.emit()

    def _on_confirm(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        plan.semantic_review_status = "confirmed"
        self._refresh_after_plan_change()

    def confirm_current(self) -> None:
        """确认当前集（回车快捷键与左栏按钮的同一条路径）。"""
        self._on_confirm()

    def mark_review_pending(self) -> None:
        """方案状态被外部改动后重刷（导出前置检查复位审核状态时用）。"""
        self._refresh_after_plan_change()

    def _refresh_after_plan_change(self) -> None:
        self.stage_view.set_project(self.selected_episode)
        self.review_pane.refresh()
        self.refresh_inspector()
        self.state_changed.emit()

    def _on_jump_requested(self, target_ms: int) -> None:
        """检查器「查看」：选中问题所在集并定位到该时间点。"""
        from fractions import Fraction

        seconds = Fraction(int(target_ms), 1000)
        plan = self.state.current_plan
        if plan is not None:
            for index, episode in enumerate(plan.episodes(), start=1):
                if episode.start_seconds <= seconds < episode.end_seconds:
                    self.select_episode(index, seek=False)
                    break
        self.stage_view.goto(seconds)

    def select_episode(self, index: int, *, seek: bool = True) -> None:
        self.selected_episode = index
        self.review_pane.set_current(index)
        self.stage_view.set_selected_episode(index)
        plan = self.state.current_plan
        if seek and plan is not None and 1 <= index <= plan.episode_count:
            self.stage_view.goto(plan.episodes()[index - 1].start_seconds)

    # ---- 阶段性前置检查 ------------------------------------------------

    def can_start_analysis(self) -> tuple[bool, str]:
        ok, reason = self.import_pane.can_continue()
        if not ok:
            return ok, reason
        return True, ""

    def can_continue(self) -> tuple[bool, str]:
        ok, reason = self.import_pane.can_continue()
        if not ok:
            return ok, reason
        return self.review_pane.can_continue()