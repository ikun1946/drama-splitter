"""主窗口：菜单 + 顶栏 + 单页工作台 + 底栏。

工作区只有一个工作台（``app/gui/workbench.py``），四个阶段在同一页里换屏，
不再有页面栈、抽屉与步骤条。本模块只负责"窗口级"的事情：

- 菜单与三个对话框（设置 / 模型 / 导出）的构造与打开；
- 项目保存 / 打开 / 自动保存；
- 活跃后台任务登记（统一判断"忙不忙"、退出时收尾）；
- 快捷键；
- 把工作台的阶段变化翻译成底栏文案与主动作。

自动化约定：测试、冒烟与截图只走 ``build_*_dialog() + show()``。实例级
``exec()`` 在离屏环境会永久阻塞（实测挂过 25 分钟），所以菜单处理器里才 exec。
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
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from ..core.app_config import AppConfig
from ..core.plan import BoundaryPlan
from ..core.settings import SplitSettings
from .dialogs import (
    ExportDialog,
    ModelDialog,
    ModelSetupDialog,
    SettingsDialog,
)
from .stage import Stage, primary_action, stage_caption
from .state import ProjectState, payload_text
from .theme import apply_theme, current_mode
from .widgets import AppBar, BottomBar
from .widgets import message
from .workbench import Workbench

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 短剧智能分集器")
        self.resize(1440, 900)
        # 三栏最小宽度预算：左 360 + 中 645（视频预览与控制条的最小宽度）
        # + 右 280（检查器 MIN_WIDTH）≈ 1285，取 1320 留余量。
        #
        # 这个数字不是随手写的：低于它，``QSplitter.setSizes`` 会被**静默忽略**，
        # 左栏被压到 170px 上下，而 ``scrollable()`` 已经关掉了横向滚动条 ——
        # 结果是内容被裁掉且没有任何报错，表现为"左侧总是看不全"。
        self.setMinimumSize(1320, 700)

        # 主题取自应用配置（记住上次选择）；非法值在 AppConfig.load 已兜底
        self.config = AppConfig.load()
        apply_theme(mode=self.config.theme)
        self.state = ProjectState()

        # 活跃任务登记：名 → BaseWorker。用于统一判断「现在忙什么」、
        # 阻止并发启动互相干扰的任务，以及退出时等它们收尾。
        self._workers: dict[str, object] = {}

        self.app_bar = AppBar()
        self.app_bar.change_source_clicked.connect(self._on_change_source)

        self.workbench = Workbench(self.state)
        self.bottom = BottomBar()
        self.bottom.action_clicked.connect(self._on_bottom_action)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.app_bar)
        body.addWidget(self.workbench, 1)
        body.addWidget(self.bottom)
        central = QWidget()
        central.setLayout(body)
        self.setCentralWidget(central)

        # 四个对话框按需构造（见 build_*_dialog）；厂商与本地模型合并在「模型管理」里
        self.settings_dialog: SettingsDialog | None = None
        self.model_dialog: ModelDialog | None = None
        self.export_dialog: ExportDialog | None = None
        self.model_setup_dialog: ModelSetupDialog | None = None

        self._connect()
        self._build_menu()
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

        self.workbench.refresh()

    # ---- 接线 ----------------------------------------------------------

    def _connect(self) -> None:
        self.workbench.stage_changed.connect(lambda _stage: self._sync_chrome())
        self.workbench.state_changed.connect(self._sync_chrome)
        self.workbench.hint_changed.connect(self.bottom.set_hint)
        self.workbench.analysis_pane.progress_changed.connect(
            lambda _text, ratio: self.bottom.set_progress(int(ratio * 100))
        )

    def _sync_chrome(self) -> None:
        """把当前阶段翻译成底栏与顶栏（唯一一处读 stage 的地方）。"""
        stage = self.workbench.stage
        self.bottom.set_caption(
            stage_caption(stage, self.state, self.workbench.selected_episode)
        )
        text, role = primary_action(stage)
        self.bottom.set_action(text, role=role)
        if stage is not Stage.ANALYZING:
            self.bottom.set_progress(None)

        media = self.state.media
        if media is not None:
            self.app_bar.set_source(Path(media.path).name, str(media.path))
        else:
            self.app_bar.set_source(None)

        if hasattr(self, "action_dark"):
            self.action_dark.setChecked(current_mode() == "dark")
        # 打开项目后探针刚完成 → 在这里续上方案链重建
        self._maybe_finish_restore()

    def _on_bottom_action(self) -> None:
        """底栏主动作：按阶段分流（文案由 stage.primary_action 提供）。"""
        stage = self.workbench.stage
        if stage is Stage.EMPTY:
            self.workbench.import_pane.choose_file()
        elif stage is Stage.READY:
            ok, reason = self.workbench.can_start_analysis()
            if not ok:
                self.bottom.set_hint(reason)
                return
            self.bottom.set_hint("")
            self.workbench.analysis_pane.start()
        elif stage is Stage.ANALYZING:
            self.workbench.analysis_pane.cancel()
        else:
            self._start_export()

    # ---- 菜单与对话框 --------------------------------------------------

    def _build_menu(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("文件")
        self._add_action(file_menu, "打开项目…", self.open_project, "Ctrl+O")
        self._add_action(file_menu, "保存项目", self.save_project, "Ctrl+S")
        self._add_action(file_menu, "另存为…", lambda: self.save_project(None))
        file_menu.addSeparator()
        self._add_action(file_menu, "更换源视频…", self._on_change_source)
        file_menu.addSeparator()
        self._add_action(file_menu, "退出", self.close)

        tools_menu = bar.addMenu("工具")
        self._add_action(tools_menu, "分集设置…", self.open_settings_dialog)
        self._add_action(tools_menu, "模型管理…", self.open_model_dialog)
        self._add_action(tools_menu, "模型设置…", self.open_model_setup_dialog)
        self._add_action(tools_menu, "导出…", self._start_export, "Ctrl+E")

        view_menu = bar.addMenu("视图")
        self.action_dark = self._add_action(
            view_menu, "深色主题", self._on_theme_toggled, checkable=True
        )
        self.action_dark.setChecked(current_mode() == "dark")

        help_menu = bar.addMenu("帮助")
        self._add_action(help_menu, "环境自检…", self.open_env_check_dialog)
        self._add_action(help_menu, "检查更新…", self.open_update_dialog)
        self._add_action(help_menu, "关于", self._show_about)

    def _add_action(self, menu, text: str, handler, shortcut: str = "",
                    checkable: bool = False):
        action = menu.addAction(text)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(handler)
        return action

    def build_settings_dialog(self) -> SettingsDialog:
        """构建设置对话框（不 exec，供测试 / 冒烟 / 截图使用）。"""
        self.settings_dialog = SettingsDialog(self.state, self)
        return self.settings_dialog

    def build_model_dialog(
        self, *, config_path=None, providers_tab: bool = False
    ) -> ModelDialog:
        """构建模型管理对话框（不 exec，供测试 / 冒烟 / 截图使用）。

        ``providers_tab=True`` 时直接停在「云端厂商」页签（合并前的厂商管理）。
        """
        self.model_dialog = ModelDialog(
            self.state, self, config_path=config_path, providers_tab=providers_tab
        )
        return self.model_dialog

    def build_model_setup_dialog(self, *, config_path=None) -> ModelSetupDialog:
        """构建「模型设置」对话框（不 exec，供测试 / 冒烟 / 截图使用）。"""
        self.model_setup_dialog = ModelSetupDialog(
            self.state, self, config_path=config_path
        )
        # 「去配置 Key…」从这里直接跳到「模型管理 → 云端厂商」：填 Key 是选云端模型
        # 的前置条件，让用户在两个窗口之间自己找是白跑一趟。
        self.model_setup_dialog.form.configure_providers_requested.connect(
            self._open_providers_tab
        )
        # 「去下载」跳到「模型管理 → 本地模型」：本机没装模型时下拉里只有云端可选，
        # 不给直达就等于让用户自己猜该去哪儿装。
        self.model_setup_dialog.form.download_models_requested.connect(
            self._open_local_models_tab
        )
        return self.model_setup_dialog

    def open_model_setup_dialog(self, *, config_path=None) -> None:
        """菜单「工具 → 模型设置…」：两条能力各选一个模型（本地 / 云端可混用）。"""
        self.build_model_setup_dialog(config_path=config_path).exec()
        # 改过的模型名落在厂商档案里，厂商页要重新读盘才看得到
        if self.model_dialog is not None:
            self.model_dialog.providers.reload()

    def _open_providers_tab(self) -> None:
        """打开「模型管理」并直接停在「云端厂商」页签。"""
        if self.model_setup_dialog is not None:
            self.model_setup_dialog.close()
        self.build_model_dialog(providers_tab=True).exec()

    def _open_local_models_tab(self) -> None:
        """打开「模型管理」并停在「本地模型」页签（默认页签）。"""
        if self.model_setup_dialog is not None:
            self.model_setup_dialog.close()
        self.build_model_dialog().exec()

    def build_export_dialog(self) -> ExportDialog:
        dialog = ExportDialog(self.state, self)
        dialog.review_requested.connect(self._on_export_review_requested)
        self.export_dialog = dialog
        return dialog

    def open_settings_dialog(self) -> None:
        self.build_settings_dialog().exec()

    def open_model_dialog(self, *, providers_tab: bool = False) -> None:
        self.build_model_dialog(providers_tab=providers_tab).exec()

    def _start_export(self) -> None:
        """打开导出对话框并直接开始（前置检查由对话框负责，Ctrl+E 同路）。"""
        dialog = self.build_export_dialog()
        dialog.show()
        dialog.start_export()

    def _on_export_review_requested(self, episode: int, ms: int) -> None:
        """§37 前置检查选「返回审核」：回到审核态并定位到问题切点。"""
        if episode:
            self.workbench.select_episode(episode, seek=False)
        if ms:
            self.workbench.stage_view.goto(Fraction(ms, 1000))
        self.workbench.inspector.set_tab(0)

    def build_env_check_dialog(self) -> "EnvCheckDialog":
        """构建「环境自检」对话框（不 exec，供测试 / 冒烟 / 截图使用）。

        构造时不发起检测——测试可先 `dialog.apply_report(...)` 注入结果，
        无界面环境下也能量到界面。真正检测走 `open_env_check_dialog`。
        """
        from .dialogs import EnvCheckDialog

        self.env_check_dialog = EnvCheckDialog(self)
        self.env_check_dialog.recheck_requested.connect(self._start_env_check)
        self.env_check_dialog.download_requested.connect(self._on_env_fix_download)
        self.env_check_dialog.open_url_requested.connect(self._on_env_fix_open_url)
        self.env_check_dialog.navigate_requested.connect(self._on_env_fix_navigate)
        return self.env_check_dialog

    def open_env_check_dialog(self) -> None:
        dialog = self.build_env_check_dialog()
        dialog.show()
        self._start_env_check()

    def _start_env_check(self) -> None:
        """后台跑一遍自检（含 ffmpeg 探测，可达数十秒，不能占主线程）。"""
        from .workers import EnvCheckWorker

        worker = EnvCheckWorker(parent=self)
        worker.checked.connect(self._on_env_report)
        self.register_worker("env_check", worker)
        worker.start()

    def _on_env_report(self, report) -> None:
        dialog = getattr(self, "env_check_dialog", None)
        if dialog is not None:
            dialog.apply_report(report)

    def _on_env_fix_download(self, model_key: str) -> None:
        """「下载模型」类修复：复用模型管理对话框的现成下载链路。

        不自己造下载流程——``model_manager.download_model`` 已处理断点续传、
        大小校验与取消，模型管理页也已接好进度与日志。这里只负责把用户送到
        那一页并说明下一步。
        """
        dialog = self.build_model_dialog()
        dialog.show()
        self.bottom.set_hint(
            f"请在「本地模型」里下载 {model_key}（已为你打开模型管理）。"
        )

    def _on_env_fix_open_url(self, url: str) -> None:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不该炸界面
            from .widgets import message

            message.warning(self, "打不开", f"请手动访问：\n{url}")

    def _on_env_fix_navigate(self, target: str) -> None:
        if target == "models":
            self.build_model_dialog().show()

    def build_update_dialog(self):
        """构建「检查更新」对话框（不 exec，供测试 / 冒烟 / 截图使用）。"""
        from run import VERSION

        from .dialogs import UpdateDialog

        self.update_dialog = UpdateDialog(self, current_version=VERSION)
        return self.update_dialog

    def open_update_dialog(self) -> None:
        dialog = self.build_update_dialog()
        dialog.show()
        # 手动打开时自动发起一次检查：用户点开就是想看结果，不该再点一次「检查」
        dialog.start_check()

    def _startup_update_check(self) -> None:
        """启动后后台静默检查更新。

        两条纪律：
        1. **只有确有新版才提示**。失败（断网、限流、404）一律静默——启动时弹
           「检查更新失败」是纯打扰，而且用户什么也做不了。
        2. **每天最多一次**。GitHub 未鉴权配额是 60 次/小时/IP，用
           ``extra["update_checked_at"]`` 记日期，别把配额耗在重复启动上。
        """
        import datetime

        from .workers import UpdateCheckWorker

        try:
            from app.core.app_config import AppConfig

            config = AppConfig.load()
            today = datetime.date.today().isoformat()
            if str(config.extra.get("update_checked_at") or "") == today:
                return
            config.extra["update_checked_at"] = today
            config.save()
        except Exception:  # noqa: BLE001 - 配置读写失败不该挡住启动
            pass

        from run import VERSION

        worker = UpdateCheckWorker(VERSION, parent=self)
        worker.checked.connect(lambda r: self._on_startup_update_checked(r))
        self.register_worker("update_check", worker)
        worker.start()

    def _on_startup_update_checked(self, result) -> None:
        if not result.ok or not result.has_update:
            return
        from app.core.app_config import AppConfig

        try:
            dismissed = str(AppConfig.load().extra.get("update_dismissed") or "")
        except Exception:  # noqa: BLE001
            dismissed = ""
        if dismissed and result.info is not None and result.info.version == dismissed:
            return  # 用户跳过了这一版
        self.build_update_dialog().show()

    def _show_about(self) -> None:
        from run import VERSION

        message.information(
            self,
            "关于",
            f"AI 短剧智能分集器 {VERSION}\n\n"
            "按《AI短剧智能分集器 V1.2.0 可实施修订方案》落地。\n"
            "AI 只给建议，用户审核，程序按帧精确导出；默认不重排、不补拍。",
        )

    # ---- 顶栏 ----------------------------------------------------------

    def _on_change_source(self) -> None:
        """顶栏「更换视频」：与空态拖放、菜单项等价。"""
        if self.is_busy():
            self.bottom.set_hint("后台任务进行中，等它结束或取消后再更换视频。")
            return
        self.workbench.import_pane.choose_file()

    def _on_theme_toggled(self) -> None:
        next_mode = "dark" if current_mode() == "light" else "light"
        apply_theme(mode=next_mode)
        self.workbench.stage_view.refresh_theme()
        if hasattr(self, "action_dark"):
            self.action_dark.setChecked(next_mode == "dark")
        self.config.theme = next_mode
        try:
            self.config.save()
        except OSError:
            # 配置写失败只影响「下次记住」，不阻断本次切换
            pass

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
            self.bottom.set_hint(f"自动保存失败：{exc}")
            return
        self.state.project_path = target
        self._last_saved_text = text
        self.app_bar.set_save_status(f"自动保存 {time.strftime('%H:%M')}")

    def save_project(self, path: str | Path | None = None) -> Path | None:
        """另存/保存项目文件。返回实际写入路径；用户取消返回 None。

        ``path`` 用 ``not path`` 判空而不是 ``path is None``：本方法既被菜单
        ``triggered`` 又被 ``QShortcut.activated`` 直接连接，而这两个信号都带一个
        ``bool checked``，PySide6 会把它当第一个实参传进来（键盘触发时恒为
        ``False``）。用 ``is None`` 拦不住这个哨兵值，会一路走到 ``Path(False)``
        抛 TypeError。
        """
        if not path and self.state.project_path is None:
            stem = Path(self.state.media.path).stem if self.state.media else "未命名项目"
            suggested = str(Path.home() / f"{stem}.drama-project")
            path, _ = QFileDialog.getSaveFileName(
                self, "保存项目", suggested, "项目文件 (*.drama-project)"
            )
            if not path:
                return None
            if not path.endswith(".drama-project"):
                path += ".drama-project"
        target = Path(path) if path else self.state.project_path
        assert target is not None
        try:
            self.state.save_snapshot(target)
        except OSError as exc:
            message.warning(self, "保存失败", str(exc))
            return None
        self.state.project_path = target
        self._last_saved_text = payload_text(self.state.snapshot_payload())
        self.app_bar.set_save_status(f"已保存 {time.strftime('%H:%M')}")
        return target

    def open_project(self, path: str | Path | None = None) -> bool:
        """打开项目文件：设置立即恢复，媒体异步探针，方案在探针完成后重建。

        返回是否开始了恢复（媒体缺失/文件损坏时给明确提示并返回 False）。

        ``path`` 的判空理由与 :meth:`save_project` 相同：菜单项的 ``triggered``
        与快捷键的 ``activated`` 都会把 ``bool checked`` 当成路径传进来，
        ``path is None`` 拦不住这个哨兵值。
        """
        if self.is_busy():
            self.bottom.set_hint("后台任务进行中，结束后再打开项目。")
            return False
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "打开项目", "", "项目文件 (*.drama-project *.json)"
            )
            if not path:
                return False
        try:
            payload = ProjectState.load_snapshot(Path(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            message.warning(self, "打开失败", f"项目文件无法读取：\n{exc}")
            return False

        media_path = payload.get("media_path")
        if not media_path or not Path(media_path).exists():
            message.warning(
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
        # 设置对话框是按需构造的（打开时才建），因此这里直接写状态；
        # 对话框构造时会用当前参数回填控件（见 SettingsDialog.__init__）。
        self.state.settings = settings

        # 方案重建需要 time_base / total_ticks，只能等探针完成（_maybe_finish_restore）
        self._pending_restore = payload
        self._last_saved_text = None
        self.workbench.import_pane.probe(str(media_path))
        return True

    def _maybe_finish_restore(self) -> None:
        """探针成功后重建方案版本链与导出记录（由 _sync_chrome 触发）。"""
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
        self.workbench.refresh()

    # ---- 活跃任务登记 --------------------------------------------------

    WORKER_NAMES = (
        "probe", "analysis", "export", "download", "judge_probe", "model_list", "model_scan",
        "update_check", "update_download", "env_check",
    )

    #: 允许"一类多实例"的任务前缀。模型设置页会同时给多家厂商拉 ``/models``，
    #: 单键登记会让它们互相顶掉——后一家把前一家 cancel 掉，清单永远拉不齐。
    #: 前缀白名单而不是放开任意后缀：并发许可只给真正需要的那一类。
    WORKER_PREFIXES = ("model_list::",)

    def register_worker(self, name: str, worker) -> None:
        """登记一个刚开始运行的后台任务，并在结束时自动注销。

        必须在 ``worker.start()`` 之前调用，否则可能错过立即发出的结束信号。
        """
        allowed = name in self.WORKER_NAMES or name.startswith(self.WORKER_PREFIXES)
        if not allowed:
            raise ValueError(f"未知任务名：{name}")
        previous = self._workers.get(name)
        if previous is not None and previous.isRunning():
            # 同一类任务不允许并发：先请求取消旧的那个，把冲突暴露出来而不是静默覆盖
            previous.cancel()
        self._workers[name] = worker
        worker.finished_ok.connect(lambda: self._workers.pop(name, None))

    def active_workers(self) -> dict[str, object]:
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
        """退出前把后台线程与播放器停干净，再补一次收尾保存。

        否则 ffmpeg 子进程会变成孤儿、两小时的编辑成果可能丢失。
        """
        self.cancel_all_workers()
        self.workbench.stage_view.player.stop()
        self._autosave_tick(force=True)
        super().closeEvent(event)

    # ---- 快捷键（方案 §54） --------------------------------------------

    def _install_shortcuts(self) -> None:
        stage_view = self.workbench.stage_view
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_project)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._start_export)
        # 播放控制：空格播放/暂停（与预览区按钮同一条路径），J/K 逐帧，←/→ 逐秒
        QShortcut(QKeySequence("Space"), self, activated=self._space_pressed)
        QShortcut(QKeySequence("K"), self, activated=lambda: stage_view.step_frames(-1))
        QShortcut(QKeySequence("J"), self, activated=lambda: stage_view.step_frames(1))
        QShortcut(QKeySequence("Left"), self, activated=lambda: stage_view.step_seconds(-1.0))
        QShortcut(QKeySequence("Right"), self, activated=lambda: stage_view.step_seconds(1.0))
        # 审核：回车确认当前集（挂在工作台上，不限定具体焦点控件）
        QShortcut(QKeySequence("Return"), self.workbench,
                  activated=self.workbench.confirm_current)
        QShortcut(QKeySequence("Enter"), self.workbench,
                  activated=self.workbench.confirm_current)
        # 检查器问题巡航：N 下一个 / P 上一个
        QShortcut(QKeySequence("N"), self, activated=lambda: self._cycle_issue(1))
        QShortcut(QKeySequence("P"), self, activated=lambda: self._cycle_issue(-1))
        # F11 全屏 ↔ 常规；Esc 先退全屏，再关对话框
        QShortcut(QKeySequence("F11"), self, activated=self._toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, activated=self._escape_pressed)

    def _space_pressed(self) -> None:
        """空格与预览区的「播放/暂停」键完全等价（一个键两态）。"""
        self.workbench.stage_view.toggle_playback()

    def _cycle_issue(self, delta: int) -> None:
        self.workbench.inspector.cycle_issue(delta)

    def _toggle_fullscreen(self) -> None:
        """F11：全屏 ↔ 常规。showNormal 会恢复进入全屏前的窗口几何。"""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _escape_pressed(self) -> None:
        """Esc 的语义分层：先退全屏，再关已打开的对话框。

        对话框关闭走 ``close()``——与 ✕ 按钮同一条路径（设置对话框因此也会
        走「取消即回滚」）。
        """
        if self.isFullScreen():
            self.showNormal()
            return
        for dialog in (self.settings_dialog, self.model_dialog, self.export_dialog):
            if dialog is not None and dialog.isVisible():
                dialog.close()
                return