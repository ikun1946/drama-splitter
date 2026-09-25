"""主窗口（方案 §13.1 推荐界面结构）。

六个工作页面的实现在 ``app.gui.pages`` 下，各占一个文件；本模块只保留
MainWindow 与装配逻辑（页面构建、页面切换、顶栏 stepper、抽屉、
活跃任务登记、快捷键）。

分层约定：
- 主题由 ``app.gui.theme`` 注入（设计令牌化，支持浅色 / 深色两套）；
- 通用控件从 ``app.gui.widgets`` 取；
- 页面的具体实现仍然挂在同名属性下（``import_page.issues``、
  ``review_page.episode_table``、``export_page.queue`` 等），冒烟测试无须改动。
"""

from __future__ import annotations

import json
import time
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..core.app_config import AppConfig
from ..core.plan import BoundaryPlan
from ..core.probe import check_compatibility
from ..core.settings import SplitSettings
from .pages import (
    ExportPage,
    ModelPage,
    ReviewPage,
    SettingsPage,
    WorkbenchPage,
    scrollable,
)
from .state import ProjectState, payload_text
from .theme import apply_theme, current_mode
from .widgets import (
    AppBar,
    Drawer,
    InspectorIssue,
    MediaPreview,
    StepNav,
    StepState,
)

# 本模块不再直接弹窗，但 QMessageBox 在本命名空间下是既有的公共接口面：
# 冒烟脚本会 monkeypatch ``main_window.QMessageBox.information`` 等来录制对话框，
# 页类也大量用它。显式重导出，避免成为一个「看起来没用」的导入被清掉。
__all__ = ["MainWindow", "PreviewPanel", "QMessageBox"]


class PreviewPanel(MediaPreview):
    """旧代码里部分脚本可能仍引用 ``PreviewPanel``；保留同名派生类。"""


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    # 三页工作区 + 两个抽屉（设置 / 模型不进核心工作流，见方案 §五）
    PAGES = ["工作台", "设置", "审核", "导出", "模型"]

    # 抽屉在 PAGES 中的位置（不占 stack 位）
    DRAWER_INDICES = {1: "settings", 4: "model"}

    # stepper 仍是四步：工作台承担前两步（导入 + 分析）
    STEPPER_NAMES = ["导入", "分析", "审核", "导出"]
    # PAGES 0 对应 stepper 0/1（工作台覆盖导入与分析）；PAGES 2/3 对应 stepper 2/3
    _STEPPER_INDEX = {0: 0, 2: 2, 3: 3}
    # PAGES 索引 → stack 索引（抽屉不占 stack 位）
    _STACK_INDEX = {0: 0, 2: 1, 3: 2}

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 短剧智能分集器")
        self.resize(1440, 900)
        # 显式设最小尺寸，避免各子控件的 minimumSizeHint 累加成 1325+
        self.setMinimumSize(960, 600)
        self.setMaximumSize(4096, 2160)

        # 主题取自应用配置（记住上次选择）；非法值在 AppConfig.load 已兜底
        self.config = AppConfig.load()
        apply_theme(mode=self.config.theme)
        self.state = ProjectState()

        # 活跃任务登记：名 → BaseWorker。用于统一判断「现在忙什么」、
        # 阻止并发启动互相干扰的任务，以及退出时等它们收尾。
        self._workers: dict[str, object] = {}

        # 顶栏（stepper + 主题切换 + 更换视频 + 设置/模型）
        self.app_bar = AppBar("🎬  AI 短剧分集器", self.STEPPER_NAMES)
        self.app_bar.set_theme_label(current_mode())
        self.app_bar.step_clicked.connect(self._on_stepper_clicked)
        self.app_bar.settings_clicked.connect(lambda: self.nav.setCurrentRow(1))
        self.app_bar.model_clicked.connect(lambda: self.nav.setCurrentRow(4))
        self.app_bar.theme_clicked.connect(self._on_theme_toggled)
        self.app_bar.change_source_clicked.connect(self._on_change_source)

        # StepNav 实例保留（兼容测试 + 作为导航数据模型），但不可见
        self.nav = StepNav(self.PAGES)
        self.nav.setVisible(False)
        self.nav.currentRowChanged.connect(self._nav_changed)

        # 状态条（极简）：颜色全部走 QSS 角色，深浅主题自动跟随
        self.status_strip = QFrame()
        self.status_strip.setProperty("role", "status-strip")
        self.status_strip.setFixedHeight(32)
        self.status_left = QLabel("未导入源视频")
        self.status_left.setProperty("role", "status-text")
        self.status_progress = QLabel("")
        self.status_progress.setProperty("role", "status-text")
        self.status_right = QLabel(f"第 1 / {len(self.STEPPER_NAMES)} 步")
        self.status_right.setProperty("role", "status-text")

        strip_layout = QHBoxLayout(self.status_strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(0)
        strip_layout.addWidget(self.status_left, 1)
        strip_layout.addWidget(self.status_progress, 1)
        strip_layout.addWidget(self.status_right)

        # 主工作区 stack：三页（工作台 / 审核 / 导出）
        #
        # 设置与模型不占 stack 位置，走顶栏图标打开的抽屉（方案 §五：只保留
        # 三个核心页面，模型与程序配置属系统设置，不进核心工作流）。
        self.stack = QStackedWidget()

        self.workbench_page = WorkbenchPage(self.state)
        self.settings_page = SettingsPage(self.state)
        self.review_page = ReviewPage(self.state)
        self.export_page = ExportPage(self.state)
        self.model_page = ModelPage(self.state)

        # 兼容既有引用：导入与分析已并入工作台，这两个名字指向其中的对应视图。
        # import_page 指向真正的 ImportPage（探针线程、兼容性清单在其中），
        # analysis_page 指向真正的 AnalysisPage，二者都挂在工作台内部。
        self.import_page = self.workbench_page.import_view
        self.analysis_page = self.workbench_page.analysis_view

        # 检查器「查看」→ 切到审核页并定位到问题时间点（方案 §二十四 / §54）
        self.workbench_page.inspector.jump_requested.connect(self._jump_to_issue)
        # 导出前置检查中选「返回审核」→ 切回审核页（方案 §37）
        self.export_page.review_requested.connect(lambda: self.nav.setCurrentRow(2))

        for page in (self.workbench_page, self.review_page, self.export_page):
            self.stack.addWidget(page)

        # 底部 action bar：提示 + 继续按钮
        self.hint = QLabel("")
        self.hint.setProperty("role", "hint")

        self.btn_continue = QPushButton("继续  →")
        self.btn_continue.setProperty("role", "primary")
        self.btn_continue.style().unpolish(self.btn_continue)
        self.btn_continue.style().polish(self.btn_continue)
        self.btn_continue.setMinimumWidth(140)
        self.btn_continue.clicked.connect(self._on_continue_clicked)

        # 兼容性占位（测试断言 window.btn_prev.isEnabled() == False）
        self.btn_prev = QPushButton("←  上一步")
        self.btn_prev.setVisible(False)
        self.btn_prev.setEnabled(False)
        self.btn_next = QPushButton("下一步  →")
        self.btn_next.setVisible(False)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.hint, 1)
        footer.addWidget(self.btn_continue)
        footer_widget = QFrame()
        footer_widget.setProperty("role", "footer-bar")
        footer_widget.setLayout(footer)
        footer_widget.setFixedHeight(56)

        # 主内容区（占满 stack）
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.stack, 1)

        body_widget = QWidget()
        body_widget.setLayout(body)

        # 总布局
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.app_bar)
        root.addWidget(self.status_strip)
        root.addWidget(body_widget, 1)
        root.addWidget(footer_widget)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # 设置 / 模型抽屉（绑在 central 之上）
        # 抽屉固定 420px，而两张页面的最小宽度是 640px：直接塞进去会被挤变形，
        # 因此套一层横向滚动区，窄抽屉里也能完整读到内容。
        self.settings_drawer = Drawer("设置", parent=central)
        self.settings_drawer.set_body(scrollable(self.settings_page))
        self.settings_drawer.closed.connect(lambda: self._on_drawer_closed(1))

        self.model_drawer = Drawer("模型管理", parent=central)
        self.model_drawer.set_body(scrollable(self.model_page))
        self.model_drawer.closed.connect(lambda: self._on_drawer_closed(4))

        # 状态条已经把「导入状态 + 第 N / M 步」显示了一遍，QMainWindow 的原生
        # 状态栏会再显示一遍同样内容。因此隐藏原生状态栏，只保留对象本身
        # 以兼容既有测试的取值。
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.addWidget(self.status_left, 1)
        self.status_bar.hide()

        self.nav.setCurrentRow(0)
        self._install_shortcuts()

        # ---- 项目保存 / 自动保存（方案 §五十五 / §五十六） ----
        #
        # 脏检测不做事件钩子（改参数/挪切点/确认审核/开始导出的入口太多），
        # 而是每 30 秒把快照序列化一遍和上次比对：字节不同才算脏。
        # 序列化一个方案链是毫秒级开销，比给每个事件源加钩子可靠得多。
        self._last_saved_text: str | None = None
        self._pending_restore: dict | None = None
        self._autosave = QTimer(self)
        self._autosave.setInterval(30_000)
        self._autosave.timeout.connect(self._autosave_tick)
        self._autosave.start()

    # ---- 项目保存 / 打开 / 自动保存 ------------------------------------

    def _autosave_tick(self, force: bool = False) -> None:
        """定期保存：内容没变不写盘，后台任务运行中不打扰。

        没有显式项目路径时存到媒体文件旁（``<媒体名>.drama-project``），
        崩溃/误退后重开即恢复——这正是 §五十六 要防的「两小时分析后丢失」。
        """
        if not force and self.is_busy():
            return
        if self.state.media is None and not self.state.plans:
            return
        text = payload_text(self.state.snapshot_payload())
        if not force and text == self._last_saved_text:
            return
        target = self.state.project_path
        if target is None:
            if self.state.media is None:
                return
            target = Path(self.state.media.path).with_suffix(".drama-project")
        try:
            self.state.save_snapshot(target)
        except OSError as exc:
            self.hint.setText(f"自动保存失败：{exc}")
            return
        self.state.project_path = target
        self._last_saved_text = text
        self.app_bar.set_save_status(f"自动保存 {time.strftime('%H:%M')}")

    def save_project(self, path: str | Path | None = None) -> Path | None:
        """另存/保存项目文件。返回实际写入路径；用户取消返回 None。"""
        if path is None and self.state.project_path is None:
            stem = Path(self.state.media.path).stem if self.state.media else "未命名项目"
            suggested = str(Path.home() / f"{stem}.drama-project")
            path, _ = QFileDialog.getSaveFileName(
                self, "保存项目", suggested, "项目文件 (*.drama-project)"
            )
            if not path:
                return None
            if not path.endswith(".drama-project"):
                path += ".drama-project"
        target = Path(path) if path is not None else self.state.project_path
        assert target is not None
        try:
            self.state.save_snapshot(target)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return None
        self.state.project_path = target
        self._last_saved_text = payload_text(self.state.snapshot_payload())
        self.app_bar.set_save_status(f"已保存 {time.strftime('%H:%M')}")
        return target

    def open_project(self, path: str | Path | None = None) -> bool:
        """打开项目文件：设置立即恢复，媒体异步探针，方案在探针完成后重建。

        返回是否开始了恢复（媒体缺失/文件损坏时给明确提示并返回 False）。
        """
        if self.is_busy():
            self.hint.setText("后台任务进行中，结束后再打开项目。")
            return False
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "打开项目", "", "项目文件 (*.drama-project *.json)"
            )
            if not path:
                return False
        try:
            payload = ProjectState.load_snapshot(Path(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "打开失败", f"项目文件无法读取：\n{exc}")
            return False

        media_path = payload.get("media_path")
        if not media_path or not Path(media_path).exists():
            QMessageBox.warning(
                self, "源视频缺失",
                f"项目引用的源视频不存在：\n{media_path}\n\n"
                "请先把源文件放回原位置再打开项目。",
            )
            return False

        self.state.project_path = Path(path)
        self.state.reset_media()
        settings = (
            SplitSettings.from_json(payload["settings"])
            if payload.get("settings") else SplitSettings()
        )
        self.settings_page.apply_settings(settings)

        # 方案重建需要 time_base / total_ticks，只能等探针完成（_maybe_finish_restore）
        self._pending_restore = payload
        self._last_saved_text = None
        self.import_page._probe(str(media_path))
        self.nav.setCurrentRow(0)
        return True

    def _maybe_finish_restore(self) -> None:
        """探针成功后重建方案版本链与导出记录（由 _sync_top_bar 触发）。"""
        payload = self._pending_restore
        if payload is None or self.state.media is None:
            return
        self._pending_restore = None

        time_base = self.state.media.video_time_base
        total_ticks = self.state.media.duration_ticks()
        self.state.plans = [
            BoundaryPlan.from_json(item, time_base, total_ticks)
            for item in payload.get("plans", [])
        ]
        cursor = max(0, len(self.state.plans) - 1)
        wanted = payload.get("current_plan_version")
        if wanted is not None:
            for index, plan in enumerate(self.state.plans):
                if plan.version == wanted:
                    cursor = index
                    break
        self.state.plan_cursor = cursor
        self.state.exported = {
            int(episode): (Path(entry["path"]), int(entry["version"]))
            for episode, entry in (payload.get("exported") or {}).items()
        }
        # 以刚打开的文件为「干净」基线，避免自动保存立刻重写一遍
        self._last_saved_text = payload_text(self.state.snapshot_payload())
        self.app_bar.set_save_status(f"已打开 {time.strftime('%H:%M')}")
        if self.state.plans:
            self.review_page.refresh()
            self._refresh_inspector()


    # ---- 活跃任务登记 ----
    #
    # 五个后台任务分散在四个页面类里，各自持有 self._worker。这里做一层全局登记，
    # 好处是：能统一回答「现在忙不忙」、能阻止并发启动互相干扰的任务、
    # 退出时能一次性等它们收尾。页面类自身的 _worker 引用保持不变（测试依赖它）。

    WORKER_NAMES = ("probe", "analysis", "export", "download")

    def register_worker(self, name: str, worker) -> None:
        """登记一个刚开始运行的后台任务，并在结束时自动注销。

        必须在 ``worker.start()`` 之前调用，否则可能错过立即发出的结束信号。
        """
        if name not in self.WORKER_NAMES:
            raise ValueError(f"未知任务名：{name}")
        previous = self._workers.get(name)
        if previous is not None and previous.isRunning():
            # 同一类任务不允许并发：先请求取消旧的那个，把冲突暴露出来而不是静默覆盖
            previous.cancel()
        self._workers[name] = worker
        worker.finished_ok.connect(lambda: self._workers.pop(name, None))

    def active_workers(self) -> dict[str, object]:
        """当前仍在运行的任务快照。"""
        return {
            name: worker
            for name, worker in self._workers.items()
            if worker.isRunning()
        }

    def is_busy(self) -> bool:
        return bool(self.active_workers())

    def cancel_all_workers(self, wait_ms: int = 3000) -> None:
        """请求取消全部任务并等待收尾。供 closeEvent 使用。"""
        for worker in self.active_workers().values():
            worker.cancel()
        for worker in self.active_workers().values():
            worker.wait(wait_ms)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """退出前把后台线程停干净，再补一次收尾保存，否则 ffmpeg 子进程会
        变成孤儿、两小时的编辑成果可能丢失。"""
        self.cancel_all_workers()
        self._autosave_tick(force=True)
        super().closeEvent(event)

    # ---- 顶栏 stepper 点击 → 跳对应 PAGES ----

    def _on_stepper_clicked(self, index: int) -> None:
        # index 是 stepper 中的 0..3，对应 PAGES 0/2/3
        inv = {v: k for k, v in self._STEPPER_INDEX.items()}
        if index in inv:
            self.nav.setCurrentRow(inv[index])

    # ---- 顶栏：主题切换 / 更换视频 / 标题同步 -------------------------

    def _on_theme_toggled(self) -> None:
        """深浅主题互切：重设全局 QSS、更新按钮文案、写回配置。"""
        next_mode = "dark" if current_mode() == "light" else "light"
        apply_theme(mode=next_mode)
        self.app_bar.set_theme_label(next_mode)
        self.config.theme = next_mode
        try:
            self.config.save()
        except OSError:
            # 配置写失败只影响「下次记住」，不阻断本次切换
            pass
        # DropZone 等自绘控件要重绘一次才能拿到新令牌
        self.update()

    def _on_change_source(self) -> None:
        """顶栏「更换视频」：与导入区的选择文件入口等价。"""
        if self.is_busy():
            self.hint.setText("后台任务进行中，等它结束或取消后再更换视频。")
            return
        self.import_page._choose_file()

    def _sync_top_bar(self) -> None:
        """按当前状态刷新顶栏标题（方案 §七：显示当前文件名与路径）。

        在页面展示与探针完成两处调用；未导入时回到应用标题。
        """
        media = self.state.media
        if media is not None:
            self.app_bar.set_source(Path(media.path).name, str(media.path))
        else:
            self.app_bar.set_source(None)
        # 打开项目后探针刚完成 → 在这里续上方案链重建
        self._maybe_finish_restore()

    def _on_drawer_closed(self, pages_index: int) -> None:
        # 抽屉关闭时把 nav 跳回工作区，否则 nav 停在抽屉项上，
        # 会让底部按钮的「继续」判定落空（抽屉不在工作流里）。
        if self.nav.currentRow() == pages_index:
            self.nav.setCurrentRow(0)

    def _on_continue_clicked(self) -> None:
        """底部按钮：根据当前阶段做不同动作。

        工作台一页承担导入与分析两职，因此这里的 0 分支要按「媒体是否就绪」
        再分流一次：未导入时先检查导入前置，已导入时启动分析或进入审核。
        """
        cur = self.nav.currentRow()
        if cur == 0:                          # 工作台
            if self.state.media is None:      # 还没导入 → 只检查导入前置
                ok, msg = self.import_page.can_continue()
                if not ok:
                    self.hint.setText(msg)
                    return
                return
            if self.analysis_page._worker and self.analysis_page._worker.isRunning():
                self.hint.setText("分析进行中，请先取消或等待")
                return
            if self.state.current_plan is None:
                self.analysis_page._start()
                return
            ok, msg = self.workbench_page.can_continue()
            if not ok:
                self.hint.setText(msg)
                return
            self.nav.setCurrentRow(2)
        elif cur == 2:                        # 审核 → 导出
            ok, msg = self.review_page.can_continue()
            if not ok:
                self.hint.setText(msg)
                return
            self.nav.setCurrentRow(3)
        elif cur == 3:                        # 导出
            self.export_page._start(None)

    # ---- 键盘快捷键 --------------------------------------------------

    def _install_shortcuts(self) -> None:
        # 上一步 / 下一步
        QShortcut(QKeySequence("Left"), self, activated=lambda: self._navigate(-1))
        QShortcut(QKeySequence("Right"), self, activated=lambda: self._navigate(1))
        # 项目保存 / 打开（方案 §五十五）
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_project)
        # 跳到指定页：Ctrl+1..N（按 PAGES 长度，抽屉也在内）
        for i in range(1, len(self.PAGES) + 1):
            QShortcut(
                QKeySequence(f"Ctrl+{i}"),
                self,
                activated=lambda idx=i - 1: self.nav.setCurrentRow(idx),
            )
        # 审核页播放/暂停、上下帧
        QShortcut(QKeySequence("Space"), self, activated=self._space_pressed)
        QShortcut(QKeySequence("K"), self, activated=lambda: self.review_page.preview.btn_next_frame.click())
        QShortcut(QKeySequence("J"), self, activated=lambda: self.review_page.preview.btn_prev_frame.click())
        # 导出（方案 §54）：切到导出页并直接走前置检查
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._start_export)
        # 审核页回车确认当前集（方案 §54）；挂在审核页上，其他页不受影响
        QShortcut(QKeySequence("Return"), self.review_page, activated=self.review_page._confirm_episode)
        QShortcut(QKeySequence("Enter"), self.review_page, activated=self.review_page._confirm_episode)
        # 检查器问题巡航（方案 §54）：N 下一个问题 / P 上一个问题
        # 跳转会切到审核页，因此不限定当前页，保证连续按 N 能逐条走完
        QShortcut(QKeySequence("N"), self, activated=lambda: self._cycle_issue(1))
        QShortcut(QKeySequence("P"), self, activated=lambda: self._cycle_issue(-1))

    def _space_pressed(self) -> None:
        if self.stack.currentWidget() is self.review_page:
            if self.review_page._player.is_playing() if hasattr(self.review_page._player, 'is_playing') else False:
                self.review_page.preview.btn_stop.click()
            else:
                self.review_page.preview.btn_play.click()

    def _start_export(self) -> None:
        """Ctrl+E：切到导出页再启动（前置检查的提示框需要导出页可见才不突兀）。"""
        self.nav.setCurrentRow(3)
        self.export_page._start(None)

    def _cycle_issue(self, delta: int) -> None:
        """N/P：在检查器问题清单里按顺序巡航跳转（方案 §54）。"""
        self.workbench_page.inspector.cycle_issue(delta)

    def _jump_to_issue(self, target_ms: int) -> None:
        """检查器「查看」：切到审核页，选中问题所在集并定位到该时间点。"""
        self.nav.setCurrentRow(2)
        seconds = Fraction(int(target_ms), 1000)
        plan = self.state.current_plan
        if plan is not None:
            for row, episode in enumerate(plan.episodes()):
                if episode.start_seconds <= seconds < episode.end_seconds:
                    self.review_page.episode_table.selectRow(row)
                    break
        self.review_page._goto(seconds)

    # ---- 导航 ---------------------------------------------------------

    def _nav_changed(self, index: int) -> None:
        if index < 0:
            return
        drawer = self.DRAWER_INDICES.get(index)
        if drawer == "settings":
            # 抽屉是浮层，不切 stack：底下仍停在用户原来的工作页
            self.settings_drawer.show()
            self.settings_page.refresh_from_state()
        elif drawer == "model":
            self.model_drawer.show()
        else:
            # 关闭所有抽屉
            if self.settings_drawer.isVisible():
                self.settings_drawer.hide()
            if self.model_drawer.isVisible():
                self.model_drawer.hide()
            stack_index = self._STACK_INDEX.get(index)
            if stack_index is not None:
                self.stack.setCurrentIndex(stack_index)

        self._refresh_nav_states()

        if index in self._STEPPER_INDEX:
            self.status_right.setText(
                f"第 {self._STEPPER_INDEX[index] + 1} / {len(self.STEPPER_NAMES)} 步"
            )
        else:
            self.status_right.setText("⚙ 设置" if drawer == "settings" else "🤖 模型")

        self._on_page_shown(index)

    def _update_stepper_for_drawer(self, open_: str) -> None:
        """抽屉打开时，stepper 不显示 current 高亮（因为不在工作流中）。"""
        # 直接由 _refresh_nav_states 统一处理
        pass

    def _refresh_nav_states(self) -> None:
        """更新 stepper 的 4 个状态 + nav 内部 5 项状态（3 工作区 + 2 抽屉）。"""
        cur = self.nav.currentRow()

        # ---- nav 内部状态 ----
        # 0 工作台：导入与分析合并，媒体就绪即算完成开头的导入环节
        # 1 设置（抽屉）、4 模型（抽屉）：不在工作流上，只反映选中
        # 2 审核、3 导出：沿用原来的阶段判定
        has_plan = self.state.current_plan is not None
        confirmed = bool(
            has_plan and self.state.current_plan.semantic_review_status == "confirmed"
        )

        nav_states: list[StepState] = [StepState.FUTURE] * len(self.PAGES)
        # 0 工作台
        if has_plan:
            nav_states[0] = StepState.DONE
        else:
            nav_states[0] = StepState.CURRENT if cur == 0 else StepState.FUTURE
        # 1 设置（抽屉）
        nav_states[1] = StepState.CURRENT if cur == 1 else StepState.FUTURE
        # 2 审核
        if has_plan:
            nav_states[2] = StepState.DONE if confirmed else StepState.CURRENT
        else:
            nav_states[2] = StepState.FUTURE
        # 3 导出
        if self.state.exported:
            nav_states[3] = StepState.DONE
        elif has_plan:
            nav_states[3] = StepState.CURRENT if cur == 3 else StepState.FUTURE
        else:
            nav_states[3] = StepState.FUTURE
        # 4 模型（抽屉）
        nav_states[4] = StepState.CURRENT if cur == 4 else StepState.FUTURE
        if 0 <= cur < len(nav_states) and cur not in self.DRAWER_INDICES:
            nav_states[cur] = StepState.CURRENT
        self.nav.set_states(nav_states)

        # stepper 4 项
        step_states: list[StepState] = [StepState.FUTURE] * len(self.STEPPER_NAMES)
        # 0: 导入
        if self.state.media is not None:
            step_states[0] = StepState.DONE
        else:
            step_states[0] = StepState.CURRENT
        # 1: 分析
        if self.state.current_plan is not None:
            step_states[1] = StepState.DONE
        elif self.state.media is not None:
            step_states[1] = StepState.CURRENT
        # 2: 审核
        if self.state.current_plan is not None:
            step_states[2] = (
                StepState.DONE
                if self.state.current_plan.semantic_review_status == "confirmed"
                else StepState.CURRENT
            )
        # 3: 导出
        if self.state.exported:
            step_states[3] = StepState.DONE
        elif self.state.current_plan is not None:
            step_states[3] = StepState.CURRENT

        # 抽屉打开时强制 stepper 不高亮（抽屉不在工作流上）
        if cur in self.DRAWER_INDICES:
            for i in range(len(step_states)):
                if step_states[i] == StepState.CURRENT:
                    step_states[i] = StepState.DONE
        # 工作台同时承担「导入 + 分析」两步：停在 0 时按实际进度补一个高亮
        if cur == 0 and not any(s == StepState.CURRENT for s in step_states):
            if self.state.media is None:
                step_states[0] = StepState.CURRENT
            elif self.state.current_plan is None:
                step_states[1] = StepState.CURRENT
        # 当前 stepper index（若在工作区）
        cur_step = self._STEPPER_INDEX.get(cur, -1)
        if cur_step >= 0 and step_states[cur_step] != StepState.DONE:
            step_states[cur_step] = StepState.CURRENT

        done_indices = {i for i, s in enumerate(step_states) if s == StepState.DONE}
        current_idx = next(
            (i for i, s in enumerate(step_states) if s == StepState.CURRENT), 0
        )
        self.app_bar.set_step_state(current_idx, done_indices)

        # 「继续」按钮文案
        if cur == 0:
            # 工作台：媒体没导进来先让用户导入；导进来后再按分析进度给文案
            if self.state.media is None:
                self.btn_continue.setText("继续  →")
            elif self.analysis_page._worker and self.analysis_page._worker.isRunning():
                self.btn_continue.setText("分析进行中…")
                self.btn_continue.setEnabled(False)
                return
            else:
                self.btn_continue.setText(
                    "下一步：审核  →" if self.state.current_plan is not None
                    else "▶  开始分析"
                )
        elif cur == 2:
            self.btn_continue.setText("下一步：导出  →")
        elif cur == 3:
            self.btn_continue.setText("▶  开始导出")
        else:
            self.btn_continue.setText("继续  →")
        self.btn_continue.setEnabled(cur in (0, 2, 3))

        # 模型通知徽章：若全部模型未安装 → 提示
        try:
            from app.core.app_config import default_models_root
            from app.core.model_manager import MODEL_CATALOG

            root = default_models_root()
            any_installed = any(
                spec.target_dir(root).exists()
                and any(spec.target_dir(root).glob("*"))
                for spec in MODEL_CATALOG
            )
            self.app_bar.set_model_notify(not any_installed)
        except Exception:
            self.app_bar.set_model_notify(False)

    def _on_page_shown(self, index: int) -> None:
        self.hint.setText("")
        if index == 0:
            # 工作台：导入区与审核页共用切点数据，进入时刷新一次
            self.workbench_page.inspector.set_tab(0)
            self._refresh_inspector()
        elif index == 2:
            self.review_page.refresh()
        # 状态条左
        if self.state.media is not None:
            self.status_left.setText(f"源文件：{self.state.media.path}")
        else:
            self.status_left.setText("未导入源视频")
        # 顶栏标题跟随源文件状态
        self._sync_top_bar()

    def _refresh_inspector(self) -> None:
        """把当前状态里能看出来的风险汇总到右栏检查器（方案 §二十四）。"""
        issues: list[InspectorIssue] = []
        state = self.state

        # 兼容性阻断
        if state.media is not None and state.binaries is not None:
            for issue in check_compatibility(state.media, state.binaries):
                if not issue.is_blocking:
                    continue
                issues.append(
                    InspectorIssue(
                        title=issue.message,
                        detail=issue.detail or "",
                        priority="P0",
                        key=f"compat:{issue.code}",
                    )
                )

        # 逐集复核风险（Phase 4 的 grade / risks）
        for review in state.episode_reviews or []:
            for risk in review.risks:
                priority = "P0" if review.grade == "block" else "P1"
                issues.append(
                    InspectorIssue(
                        title=risk,
                        priority=priority,
                        episode=review.episode,
                        key=f"review:{review.episode}",
                    )
                )

        self.workbench_page.inspector.set_issues(issues)

    def _current_page(self):
        """返回对应阶段的页面（仅工作区 3 个）。"""
        mapping = {0: self.workbench_page, 2: self.review_page, 3: self.export_page}
        return mapping.get(self.nav.currentRow())

    def _navigate(self, delta: int) -> None:
        """按 delta 在三个工作区之间移动（抽屉不参与）。"""
        cur = self.nav.currentRow()
        order = [0, 2, 3]
        if cur not in order:
            return
        i = order.index(cur)
        if delta > 0:
            ok, message = self._current_page().can_continue()
            if not ok:
                self.hint.setText(message)
                return
            if i + 1 < len(order):
                self.nav.setCurrentRow(order[i + 1])
        elif delta < 0:
            if i - 1 >= 0:
                self.nav.setCurrentRow(order[i - 1])

    # ---- 旧 nav 仍要保留 ----

    def _legacy_nav_state(self) -> None:
        """保留旧方法作为占位，避免破坏继承。"""
        pass