"""界面层回归测试（离屏运行）。

只覆盖"构造 + 参数联动 + 可行性提示"这些廉价但易碎的部分；
包含真实编码的完整链路验证在 tools/gui_smoke.py，作为发布前的手工关卡。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面测试")

from fractions import Fraction  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.probe import probe_media  # noqa: E402
from app.gui.stage import Stage  # noqa: E402
from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
)  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(scope="module")
def window(qapp):
    from app.gui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.workbench.stage_view.player.stop()
    win.close()


@pytest.fixture(autouse=True)
def message_recorder(monkeypatch):
    """把统一弹窗出口（app.gui.widgets.message）换成录制器。

    离屏环境里 QMessageBox 会永久阻塞——静态方法与实例 exec() 都一样，本
    项目实测挂过 25 分钟。因此整份界面测试都不允许真弹窗；需要断言弹窗内容
    的用例直接读这个夹具返回的列表。
    """
    from app.gui.widgets import message as message_mod

    recorded: list[tuple] = []

    def record(kind):
        def inner(*args, **kwargs):
            recorded.append((kind,) + args)
            return None

        return inner

    monkeypatch.setattr(message_mod, "information", record("information"))
    monkeypatch.setattr(message_mod, "warning", record("warning"))
    monkeypatch.setattr(message_mod, "critical", record("critical"))
    monkeypatch.setattr(message_mod, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(message_mod, "confirm_continue", lambda *a, **k: False)
    return recorded


class TestWindowConstruction:
    def test_single_page_workbench_has_three_columns(self, window):
        """单页工作台：一页三栏（左/中/右），没有页面栈与抽屉。"""
        wb = window.workbench
        assert wb.splitter.count() == 3, "工作台应由三栏组成"
        assert wb.inspector is not None, "右栏检查器常驻"
        assert not hasattr(window, "stack"), "页面栈已退场"
        assert window.menuBar().actions(), "菜单是设置/模型/导出的入口"

    def test_workbench_composes_import_and_analysis(self, window):
        wb = window.workbench
        assert wb.import_pane.parent() is not None
        assert wb.analysis_pane.parent() is not None
        assert wb.review_pane.parent() is not None
        assert wb.stage_view.parent() is not None

    def test_workbench_splitter_is_actually_resizable(self, window, qapp):
        """三栏分割条必须能被程序与用户调整。

        回归用例：子控件最小宽度之和超过可视宽度时，Qt 会静默忽略 setSizes()
        与 moveSplitter()——实测表现为 sizes() 恒等于均分。修法是左右两栏各套
        一层滚动容器。这里断言"能设得动"，而不是断言某个具体比例。
        """
        wb = window.workbench
        window.resize(1440, 900)
        window.show()          # 布局只在 show 之后才真正分配尺寸
        qapp.processEvents()
        try:
            wb.left_stack.setVisible(True)   # 空态会隐藏左栏；测分割器时显式显示
            qapp.processEvents()
            total = sum(wb.splitter.sizes())
            assert total > 0, "分割器未获得可用宽度"

            left = max(1, int(total * 0.2))
            center = int(total * 0.6)
            wb.splitter.setSizes([left, center, total - left - center])
            qapp.processEvents()
            got = wb.splitter.sizes()
            assert abs(got[0] - left) <= 4, (
                f"分割器忽略了 setSizes：请求左栏 {left}px，实际 {got[0]}px（{got}）"
                "——子控件最小宽度可能又超过了可用宽度"
            )
            assert got[1] > got[0] and got[1] > got[2], "中栏应拿到最多宽度"
        finally:
            window.hide()      # 恢复 fixture 原状，不影响同模块后续用例

    def test_dialog_does_not_replace_workbench(self, window, qapp):
        """设置 / 模型是对话框（真顶层窗口），打开时底下仍是工作台。"""
        dialog = window.build_settings_dialog()
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible(), "设置对话框应能直接显示（工厂 + show，不 exec）"
        assert window.workbench.isVisible() or not window.isVisible()
        dialog.close()

    def test_starts_in_empty_stage(self, window):
        """空态：阶段 EMPTY，底栏主动作是「选择源视频…」。"""
        window.state.reset_media()
        window.workbench.refresh()
        assert window.workbench.stage is Stage.EMPTY
        assert window.bottom.action_text().startswith("选择源视频")

    def test_analysis_requires_media(self, window, message_recorder):
        window.state.reset_media()
        ok, reason = window.workbench.can_start_analysis()
        assert not ok and "导入" in reason

        window.workbench.analysis_pane.start()
        assert any(kind == "warning" for kind, *_ in message_recorder), (
            "没有素材时应弹「尚未导入」而不是静默失败"
        )


class TestThemeAndTopBar:
    """第 3 步：运行时主题切换 + 顶栏动态标题。

    主题切换是全局 QSS 重注入，切不回去就是事故——这里两个方向各切一次。
    配置持久化用 tmp_path 替换 config_path，绝不污染开发机上的 config.json。
    """

    def test_theme_toggle_round_trip(self, window, qapp, monkeypatch, tmp_path):
        from app.gui import theme
        from app.gui.theme import apply_theme, current_mode

        # 配置写到临时目录，测试不碰真实 config.json
        monkeypatch.setattr(
            "app.core.app_config.config_path", lambda: tmp_path / "config.json"
        )
        start = current_mode()
        try:
            window._on_theme_toggled()
            qapp.processEvents()
            flipped = current_mode()
            assert flipped != start
            assert qapp.styleSheet() == theme.build_stylesheet(flipped)
            # 菜单里的勾选态要跟着走，否则用户看到的是错的当前主题
            assert window.action_dark.isChecked() == (flipped == "dark")

            # 再切回来，确认可逆
            window._on_theme_toggled()
            qapp.processEvents()
            assert current_mode() == start

            # 配置应记录最后一次的选择
            from app.core.app_config import AppConfig

            assert AppConfig.load().theme == start
        finally:
            # 无论断言成败，把主题拨回起点，不影响同模块其他用例
            apply_theme(mode="light")
            window.action_dark.setChecked(False)

    def test_theme_toggle_persists_to_config(self, monkeypatch, tmp_path):
        from app.core.app_config import AppConfig, config_path

        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("app.core.app_config.config_path", lambda: cfg_path)

        cfg = AppConfig()
        cfg.theme = "dark"
        cfg.save()
        reloaded = AppConfig.load()
        assert reloaded.theme == "dark"

        # 旧配置文件没有 theme 字段 → 回落浅色
        cfg_path.write_text('{"models_dir": null}', encoding="utf-8")
        assert AppConfig.load().theme == "light"
        # 非法值同样回落浅色
        cfg_path.write_text('{"theme": "solarized"}', encoding="utf-8")
        assert AppConfig.load().theme == "light"
        assert config_path()  # 引用存活，防误删

    def test_top_bar_shows_source_name(self, window):
        bar = window.app_bar
        app_title = bar.title_label.text()
        bar.set_source("某个很长很长的剧名_全集.mp4", "D:/drama/某个很长很长的剧名_全集.mp4")
        assert bar.title_label.text() == "某个很长很长的剧名_全集.mp4"
        assert "D:/drama" in bar.title_label.toolTip()
        bar.set_source(None)
        assert bar.title_label.text() == app_title

        # 超长文件名截断且带省略号
        long_name = "超" * 60 + ".mp4"
        bar.set_source(long_name, long_name)
        assert len(bar.title_label.text()) <= 37 and bar.title_label.text().endswith("…")
        bar.set_source(None)

    def test_model_button_targets_drawer_row(self, window, monkeypatch):
        """回归：设置/模型入口曾指向不存在的行，永远打不开。

        现在入口在「工具」菜单；这里断言菜单项存在且分别调到对应对话框工厂。
        """
        opened: list[str] = []
        monkeypatch.setattr(window, "open_settings_dialog", lambda: opened.append("settings"))
        monkeypatch.setattr(window, "open_model_dialog", lambda: opened.append("model"))

        tools_menu = next(
            a.menu() for a in window.menuBar().actions() if a.text() == "工具"
        )
        actions = {a.text(): a for a in tools_menu.actions()}
        assert "分集设置…" in actions and "模型管理…" in actions
        actions["分集设置…"].trigger()
        actions["模型管理…"].trigger()
        assert opened == ["settings", "model"]


class TestProjectPersistence:
    """第 4 步：项目保存 / 打开 / 自动保存（方案 §五十五 / §五十六）。

    open_project 的媒体恢复走真实 ffprobe（与既有 fixtures 同一素材），
    方案链重建在探针完成后异步发生——测试里用 wait_until 风格的事件泵等它。
    """

    @staticmethod
    def _wait(qapp, predicate, timeout=10.0):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            qapp.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _build_plan(media, cuts):
        from fractions import Fraction

        from app.core.plan import BoundaryPlan

        time_base = media.video_time_base
        ticks = [0] + [time_base.seconds_to_ticks(Fraction(str(c))) for c in cuts]
        ticks.append(media.duration_ticks())
        return BoundaryPlan(
            boundary_ticks=ticks, time_base=time_base, total_ticks=media.duration_ticks()
        )

    def test_save_then_open_roundtrip(self, window, qapp, binaries, assets, tmp_path):
        from fractions import Fraction

        from app.core.settings import RangeSpec, SplitSettings

        window.state.binaries = binaries
        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.workbench.import_pane.worker and not window.workbench.import_pane.worker.isRunning(),
        ), "探测超时"
        qapp.processEvents()

        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        window.state.settings = settings
        plan = self._build_plan(window.state.media, [30, 60, 90])
        window.state.add_plan(plan)
        window.state.audio_stream_index = 0

        path = tmp_path / "roundtrip.drama-project"
        assert window.save_project(path) == path
        assert path.exists()
        assert window.app_bar.save_label.text().startswith("已保存")

        # 破坏现场，再从项目文件恢复
        window.state.reset_media()
        window.state.settings = SplitSettings()
        window.open_project(path)
        assert self._wait(
            qapp,
            lambda: window.state.media is not None and bool(window.state.plans),
        ), "打开项目后未完成恢复"

        assert window.state.settings.target_duration_seconds == Fraction(30)
        assert window.state.settings.range.tolerance == Fraction(0)
        assert len(window.state.plans) == 1
        restored = window.state.plans[0]
        assert list(restored.boundary_ticks) == list(plan.boundary_ticks)
        assert window.state.project_path == path

    def test_autosave_only_writes_when_dirty(
        self, window, qapp, binaries, assets, tmp_path
    ):
        window.state.binaries = binaries
        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.workbench.import_pane.worker and not window.workbench.import_pane.worker.isRunning(),
        )
        qapp.processEvents()

        target = tmp_path / "auto.drama-project"
        window.state.project_path = target
        window._autosave_tick()
        assert target.exists(), "首次自动保存应落盘"
        first = target.read_text(encoding="utf-8")

        # 内容没变 → 不重写
        window._autosave_tick()
        assert target.read_text(encoding="utf-8") == first

        # 方案变了 → 下一个 tick 落盘新内容
        window.state.add_plan(self._build_plan(window.state.media, [30, 60, 90]))
        window._autosave_tick()
        assert target.read_text(encoding="utf-8") != first

    def test_apply_settings_roundtrip(self, window):
        from fractions import Fraction

        from app.core.settings import (
            CountPolicy,
            RangeMode,
            RangeSpec,
            SplitMode,
            SplitSettings,
            Strategy,
        )

        original = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            target_episode_count=12,
            count_policy=CountPolicy.FLEXIBLE,
            allowed_count_min=10,
            allowed_count_max=15,
            target_duration_seconds=Fraction(45),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 10)),
            strategy=Strategy.SUSPENSE,
            allow_episode_exceptions=True,
        )
        form = window.build_settings_dialog().form
        form.apply_settings(original)
        collected = form.collect_settings()
        assert collected.split_mode == SplitMode.TARGET_EPISODE_COUNT
        assert collected.target_episode_count == 12
        assert collected.count_policy == CountPolicy.FLEXIBLE
        assert collected.allowed_count_min == 10
        assert collected.allowed_count_max == 15
        assert collected.range.tolerance == Fraction(1, 10)
        assert collected.strategy == Strategy.SUSPENSE
        assert collected.allow_episode_exceptions is True



class TestImportPreview:
    """回归：导入完成后预览必须出图，不能留着占位文案。

    用户报告「选好视频之后画面还写着尚未选择源文件」：绑定媒体时没有任何地方
    解码首帧（只有用户主动 seek 才有画面），而画面区最大的那块文字正好是这句
    占位提示——导入明明成功，看起来却像视频没选上。
    """

    def test_import_decodes_first_frame(self, window, qapp, binaries, assets):
        window.state.binaries = binaries
        window.state.reset_media()
        window.workbench.refresh()
        assert window.workbench.stage is Stage.EMPTY

        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert TestProjectPersistence._wait(
            qapp,
            lambda: window.workbench.import_pane.worker
            and not window.workbench.import_pane.worker.isRunning(),
        ), "探测超时"
        qapp.processEvents()

        assert window.workbench.stage is Stage.READY
        preview = window.workbench.stage_view.preview
        assert preview.view.text() == "", (
            f"导入后画面仍显示占位文案：{preview.view.text()!r}——"
            "绑定媒体后应立即解出第一帧"
        )
        assert not preview.view.pixmap().isNull(), "导入后预览应显示真实解码帧"
        assert window.workbench.stage_view.position == 0, "新片源的播放头应归零"

        # 同一片源重刷（方案改动、锁切点都会走 set_project）不得把画面清回占位
        window.workbench.refresh()
        assert preview.view.text() == "" and not preview.view.pixmap().isNull()


class TestCloudModelCard:
    """云端模型（厂商 API）卡片：默认零外发，且"没测通不许启用"。

    连通性探测被替换成假实现——界面测试不允许真联网（也不该要求 Key）。
    """

    @staticmethod
    def _form(window, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.core.app_config.config_path", lambda: tmp_path / "config.json"
        )
        return window.build_model_dialog().form

    def test_defaults_are_closed_and_local(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        assert form.cloud_enable.isChecked() is False
        assert form.cloud_allow.isChecked() is False
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND, "默认仍走本地"
        assert "未启用" in form.cloud_hint.text()
        assert "不含画面" in form.cloud_allow.text(), "外发范围必须在勾选那一刻就能读到"

    def test_probe_failure_refuses_to_enable(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND, AppConfig
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_model.setText("ep-test")
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        monkeypatch.setattr(
            type(form),
            "_probe_cloud",
            lambda self, profile, capability: "鉴权失败（API Key 或资源权限）",
        )

        form._save_cloud()

        assert form.cloud_enable.isChecked() is False, "测不通就不该保持勾选"
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        assert "未通过" in form.log.toPlainText()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        profile = saved.profiles().get("volc")
        assert profile.enabled is False, "失败的档案不得被标记为已启用"
        assert profile.judge_model == "ep-test", "字段本身应已保存，用户不用重填"

    def test_probe_success_enables_and_persists(self, window, tmp_path, monkeypatch):
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_model.setText("ep-test")
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)

        form._save_cloud()

        assert form.cloud_enable.isChecked() is True
        assert "已启用" in form.cloud_hint.text()
        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("judge") == "volc"
        profile = saved.profiles().get("volc")
        assert profile.enabled is True and profile.allow_upload is True
        assert profile.api_key == "sk-test"

    def test_asr_capability_switches_independently(self, window, tmp_path, monkeypatch):
        """两条能力各自测通、各自切换：只勾转写时语义判断仍走本地。"""
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_asr_enable.setChecked(True)      # 只勾转写
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)

        form._save_cloud()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("asr") == "volc"
        assert saved.backend_for("judge") == "local", "没勾的能力不许被切到云端"
        assert "转写" in form.cloud_hint.text() and "语义判断" not in form.cloud_hint.text()

    def test_asr_probe_failure_keeps_local_and_judge_stays(self, window, tmp_path, monkeypatch):
        """转写测不通只影响转写：语义判断已测通的不该被连带取消。"""
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_model.setText("ep-test")
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        form.cloud_asr_enable.setChecked(True)

        def probe(self, profile, capability):
            return "识别失败（45000151）" if capability == "asr" else None

        monkeypatch.setattr(type(form), "_probe_cloud", probe)
        form._save_cloud()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("asr") == "local"
        assert saved.backend_for("judge") == "volc"
        assert form.cloud_asr_enable.isChecked() is False
        assert form.cloud_enable.isChecked() is True

    def test_project_snapshot_never_carries_the_key(self, window, tmp_path, monkeypatch):
        """§6：方案快照只记"能否复现这次分集"，不含任何凭据。"""
        from app.core.app_config import AppConfig
        from app.gui.state import payload_text

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_model.setText("ep-test")
        form.cloud_key.setText("sk-secret-value")
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)
        form.cloud_enable.setChecked(True)
        form.cloud_allow.setChecked(True)
        form._save_cloud()
        assert AppConfig.load(tmp_path / "config.json").providers, "前置：确实写进了配置"

        snapshot = payload_text(window.state.snapshot_payload())
        assert "sk-secret-value" not in snapshot


class TestWorkers:
    """工作线程的构造参数与信号契约。

    AnalysisWorker 一度漏传 settings，等到界面点「开始分析」才炸出来——
    那是用户最不想看到报错的地方。这里直接调用 run()（不经过 QThread.start），
    在同一线程内同步执行，异常不会丢失。
    """

    def test_analysis_worker_completes_and_emits_plan(self, binaries, assets):
        from fractions import Fraction

        from app.gui.workers import AnalysisWorker

        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        worker = AnalysisWorker(binaries, media, 0, settings)
        outcome: dict = {}

        worker.stage_started.connect(lambda name: outcome.setdefault("stages", []).append(name))
        worker.stage_log.connect(lambda text: outcome.setdefault("log", []).append(text))
        worker.completed.connect(
            lambda plan, report, problems, candidates, reviews: outcome.update(
                plan=plan, candidates=candidates, reviews=reviews
            )
        )
        worker.failed.connect(lambda message: outcome.setdefault("failed", message))

        worker.run()  # 同步执行，异常不会被线程吞掉

        assert "failed" not in outcome, f"分析线程失败：{outcome['failed']}"
        assert "plan" in outcome, f"未产出方案：{outcome.get('stages')}"
        assert outcome["candidates"] is not None
        assert "字幕解析" in outcome["stages"]
        assert "语音转写" in outcome["stages"]
        assert "候选点" in outcome["stages"]

    def test_analysis_worker_reports_missing_model_as_log_not_crash(
        self, binaries, assets, monkeypatch
    ):
        """模型缺失是可恢复状态：应作为日志说明跳过转写，而不是让分析失败。

        §7.2 要求"无音轨时跳过 ASR、启用画面分析和人工审核"——模型缺失同理。
        """
        import app.core.asr as asr_module
        from fractions import Fraction

        from app.gui.workers import AnalysisWorker

        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        worker = AnalysisWorker(binaries, media, 0, settings)
        outcome: dict = {}

        def refuse(*args, **kwargs):
            raise RuntimeError("模型未下载（测试模拟）")

        monkeypatch.setattr(asr_module.AsrEngine, "transcribe_cached", refuse)
        worker.stage_log.connect(lambda text: outcome.setdefault("log", []).append(text))
        worker.failed.connect(lambda message: outcome.setdefault("failed", message))
        worker.completed.connect(
            lambda plan, report, problems, candidates: outcome.update(plan=plan)
        )

        worker.run()

        assert "failed" not in outcome, "模型缺失不应让分析失败"
        assert any("跳过语音转写" in line for line in outcome.get("log", []))
        assert "plan" in outcome


class TestSettingsPageBehaviour:
    def test_feasibility_updates_live(self, window, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media
        window.state.binaries = binaries

        page = window.build_settings_dialog().form
        page.radio_by_duration.setChecked(True)
        page.range_percent.setChecked(True)
        page.duration_spin.setValue(30.0)
        page.tolerance_spin.setValue(0.0)

        text = page.feasibility.toPlainText()
        # 120 秒 / 目标是 30 秒、0% 浮动 → 只能分成 4 集
        assert "可行集数范围：4 – 4 集" in text
        assert "浮动比例为 0%" in text, "0% 浮动必须给出提示"
        assert "参数可行" in text

    def test_infeasible_parameters_block_progress(self, window, binaries, assets):
        """§5.1 参数无解必须在调用分析之前就拦住（§17.4），且提示要可操作。"""
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media

        page = window.build_settings_dialog().form
        page.radio_by_count.setChecked(True)
        page.policy_exact.setChecked(True)
        page.count_spin.setValue(60)
        page.range_manual.setChecked(True)
        page.min_spin.setValue(80.0)
        page.max_spin.setValue(100.0)

        text = page.feasibility.toPlainText()
        # 严格 60 集、每集不低于 80 秒 → 至少需要 4800 秒，而源片只有 120 秒
        assert "至少需要 4800.0 秒" in text, f"提示未给出具体的时长缺口：\n{text}"
        assert "当前只有 120.0 秒" in text
        assert "当前参数无解" in text

        ok, message = page.can_continue()
        assert not ok
        assert "至少需要 4800.0 秒" in message

    def test_empty_episode_range_message_is_meaningful(self, window, binaries, assets):
        """集长范围与总时长冲突时，不得打印 "2 – 1 集" 这类无意义区间。"""
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media

        page = window.build_settings_dialog().form
        page.radio_by_duration.setChecked(True)
        page.range_manual.setChecked(True)
        page.min_spin.setValue(200.0)
        page.max_spin.setValue(300.0)

        text = page.feasibility.toPlainText()
        assert "可行集数范围：无" in text
        assert "– 1 集" not in text and "2 – " not in text
        assert "当前参数无解" in text

    def test_mode_switch_updates_settings_object(self, window, binaries, assets):
        """界面控件是唯一事实来源，切换后设置对象必须同步。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form

        page.radio_by_count.setChecked(True)
        page.policy_flexible.setChecked(True)
        settings = page.collect_settings()
        assert settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
        assert settings.count_policy == CountPolicy.FLEXIBLE

        page.range_manual.setChecked(True)
        settings = page.collect_settings()
        assert settings.range.mode == RangeMode.MANUAL

    def test_strict_mode_ignores_manual_range_for_target(self, window, binaries, assets):
        """§4.1 两种范围来源互斥：百分比模式下手动上下限不参与计算。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form
        page.radio_by_count.setChecked(True)
        page.range_percent.setChecked(True)
        page.tolerance_spin.setValue(20.0)
        page.count_spin.setValue(4)

        settings = page.collect_settings()
        derived = settings.derive(Fraction(120))
        assert derived.min_duration == Fraction(24)
        assert derived.max_duration == Fraction(36)


class TestSettingsDialogRollback:
    """设置对话框的「取消即回滚」语义。

    表单是实时写回 ``state.settings`` 的（控件一变就 collect），所以"取消"
    必须是显式回滚，否则用户试出来的参数会被当成确认值留下。
    """

    def test_cancel_restores_snapshot(self, window):
        from fractions import Fraction

        from app.core.settings import SplitSettings

        window.state.settings = SplitSettings(target_duration_seconds=Fraction(45))
        dialog = window.build_settings_dialog()
        form = dialog.form
        assert form.duration_spin.value() == 45.0, "打开时应把当前参数回填到控件"

        form.duration_spin.setValue(12.0)     # 用户试参数（实时写回）
        assert window.state.settings.target_duration_seconds == Fraction(12)

        dialog.cancel_and_close()
        assert window.state.settings.target_duration_seconds == Fraction(45), (
            "「取消」应把参数恢复成打开时的快照"
        )

    def test_close_button_also_rolls_back(self, window):
        """✕ / Esc 与「取消」同语义（reject 路径）。"""
        from fractions import Fraction

        from app.core.settings import SplitSettings

        window.state.settings = SplitSettings(target_duration_seconds=Fraction(30))
        dialog = window.build_settings_dialog()
        dialog.form.duration_spin.setValue(8.0)
        assert window.state.settings.target_duration_seconds == Fraction(8)

        dialog.reject()
        assert window.state.settings.target_duration_seconds == Fraction(30)


class TestExportDialogContracts:
    """§37 的纯逻辑：返回审核时的定位目标、前置检查文案。"""

    def test_review_target_points_at_problem_episode(self):
        from app.core.plan import BoundaryPlan, PlanProblem
        from app.core.timebase import TimeBase
        from app.gui.panes.export_form import review_target

        plan = BoundaryPlan.uniform(4, TimeBase(1, 25), 3000)   # 0/30/60/90/120 秒
        episode, ms = review_target(
            plan, [PlanProblem(code="X", level="warn", message="超长", episode=3)]
        )
        assert episode == 3
        assert ms == 90_000, "问题落在第 3 集片尾（90 秒）"

    def test_review_target_falls_back_without_episode(self):
        from app.core.plan import BoundaryPlan, PlanProblem
        from app.core.timebase import TimeBase
        from app.gui.panes.export_form import review_target

        plan = BoundaryPlan.uniform(4, TimeBase(1, 25), 3000)
        episode, ms = review_target(
            plan, [PlanProblem(code="Y", level="warn", message="全局问题")]
        )
        assert (episode, ms) == (0, 0), "没有集号时只回审核态、不定位"

    def test_precheck_prompt_mentions_both_counts(self):
        from app.gui.panes.export_form import precheck_prompt

        text = precheck_prompt([], 3)
        assert "3 个切点未审核" in text
        assert "继续" in text and "返回审核" in text


class _FakeSignal:
    def connect(self, slot):
        pass


class _FakeWorker:
    """ExportWorker 替身：记录实例化入参，start() 不启动真线程。"""

    instances: list["_FakeWorker"] = []
    episode_done = _FakeSignal()
    completed = _FakeSignal()
    failed = _FakeSignal()
    finished_ok = _FakeSignal()

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _FakeWorker.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        pass

    def isRunning(self):
        return False

    def wait(self, ms=0):
        pass


class TestStep5Refinement:
    """第 5 步：导出前置检查（§37）、快捷键（§54）、检查器跳转、按钮压扁回归。"""

    def _seed(self, window, binaries, assets):
        """给共享的 module 级窗口塞一份干净的 4 集方案现场。"""
        window.state.reset_media()
        window.state.plans = []
        window.state.plan_cursor = 0
        window.state.exported = {}
        window.state.output_root = None
        media = probe_media(binaries, assets["cfr"])
        plan = TestProjectPersistence._build_plan(media, [30, 60, 90])
        window.state.media = media
        window.state.binaries = binaries
        window.state.add_plan(plan)
        window.workbench.refresh()
        return media, plan

    def test_export_precheck_counts_unreviewed_cuts(self, window, binaries, assets):
        """§37：未确认方案 → 未审核切点数 = 集数 - 1（首尾是片头片尾）。"""
        from app.gui.panes.export_form import export_precheck

        _, plan = self._seed(window, binaries, assets)
        blocking, warnings, unreviewed = export_precheck(plan, window.state.settings)
        assert blocking == []
        assert warnings == []
        assert unreviewed == 3

        plan.semantic_review_status = "confirmed"
        _, _, unreviewed = export_precheck(plan, window.state.settings)
        assert unreviewed == 0

    def test_precheck_dialog_back_to_review(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：前置检查选「返回审核」→ 关对话框、回审核态、不导出。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        assert plan.semantic_review_status == "pending"

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "review"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()
        qapp.processEvents()

        assert window.workbench.stage is Stage.REVIEW, "应回到审核态（左栏切回集列表）"
        assert not _FakeWorker.instances, "不应启动导出线程"
        assert not dialog.isVisible(), "选「返回审核」应关闭导出对话框"

    def test_precheck_dialog_continue_exports(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：选「继续导出」→ 走到启动导出（用户有意忽略未审核项）。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        self._seed(window, binaries, assets)
        window.state.output_root = tmp_path

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "continue"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()

        assert len(_FakeWorker.instances) == 1, "继续导出应启动一次导出线程"
        worker = _FakeWorker.instances[0]
        assert worker.kwargs.get("only_episodes") is None

    def test_hard_constraints_still_block_export(
        self, window, qapp, binaries, assets, tmp_path, monkeypatch, message_recorder
    ):
        """§37：硬约束问题仍直接拦截，连前置检查都不给。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        # 破坏硬约束：起点不再从 0 开始 → NOT_START_AT_ZERO（阻断）
        plan.boundary_ticks = [plan.boundary_ticks[0] + 1000, plan.boundary_ticks[-1]]

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "continue"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()

        assert any(kind == "warning" for kind, *_ in message_recorder), "硬约束应弹警告"
        assert not _FakeWorker.instances, "硬约束问题必须拦住导出"

    def test_shortcut_sequences_registered(self, window):
        """§54：Ctrl+E / Enter / N / P / F11 / Esc 必须注册成快捷键。"""
        from PySide6.QtGui import QShortcut

        sequences = {sc.key().toString() for sc in window.findChildren(QShortcut)}
        for expected in (
            "Ctrl+E", "N", "P", "Return", "Enter", "Space", "Ctrl+S", "F11", "Esc"
        ):
            assert expected in sequences, f"缺少快捷键 {expected}（现有：{sorted(sequences)}）"

    def test_f11_toggles_fullscreen(self, window):
        """F11：全屏 ↔ 常规真实翻转（窗口状态标志在 show 时同步置位）。"""
        assert not window.isFullScreen()
        window._toggle_fullscreen()
        assert window.isFullScreen(), "第一次 F11 应进入全屏"
        window._toggle_fullscreen()
        assert not window.isFullScreen(), "第二次 F11 应退出全屏"

    def test_esc_exits_fullscreen_first(self, window, qapp):
        """Esc 分层：全屏时 Esc 只退全屏，不关对话框（优先级最高）。"""
        dialog = window.build_settings_dialog()
        dialog.show()
        qapp.processEvents()
        try:
            window._toggle_fullscreen()
            window._escape_pressed()
            assert not window.isFullScreen(), "全屏 + 对话框同开时 Esc 应先退全屏"
            assert dialog.isVisible(), "同一次 Esc 不该顺手把对话框也关掉"
        finally:
            dialog.close()
            qapp.processEvents()

    def test_esc_closes_open_dialog(self, window, qapp):
        """Esc 分层：非全屏时 Esc 关闭打开中的对话框。"""
        dialog = window.build_model_dialog()
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible(), "前置：对话框已打开"
        window._escape_pressed()
        qapp.processEvents()
        assert not dialog.isVisible(), "Esc 应关闭对话框"

    def test_ctrl_e_opens_export_dialog_and_warns_without_plan(
        self, window, qapp, monkeypatch, message_recorder
    ):
        """Ctrl+E：无方案时打开导出对话框并给「请先完成导入、分析与审核」。"""
        window.state.reset_media()
        window.state.plans = []
        window.state.plan_cursor = 0
        window.workbench.refresh()

        window._start_export()

        assert window.export_dialog is not None, "Ctrl+E 应打开导出对话框"
        assert any(
            "请先完成导入、分析与审核" in str(call) for call in message_recorder
        ), f"应给出可操作提示，实际弹窗：{message_recorder}"
        window.export_dialog.close()

    def test_return_confirms_episode(self, window, qapp, binaries, assets):
        """§54：回车 = 确认当前集（快捷键挂在工作台上，走同一条确认路径）。"""
        from PySide6.QtGui import QShortcut

        _, plan = self._seed(window, binaries, assets)
        assert plan.semantic_review_status == "pending"

        return_shortcuts = [
            sc for sc in window.findChildren(QShortcut)
            if sc.parent() is window.workbench and sc.key().toString() == "Return"
        ]
        assert len(return_shortcuts) == 1, "Return 快捷键应唯一且挂在工作台上"
        return_shortcuts[0].activated.emit()

        assert plan.semantic_review_status == "confirmed", "回车应触发确认当前集"

    def test_inspector_cycles_jumpable_issues(self, window):
        """N/P 巡航：只走有定位的问题项，N/P 方向相反且可循环。"""
        from app.gui.widgets import InspectorIssue

        inspector = window.workbench.inspector
        got = []
        inspector.jump_requested.connect(got.append)
        inspector.set_issues([
            InspectorIssue(title="a", target_ms=1000),
            InspectorIssue(title="b", target_ms=2000),
            InspectorIssue(title="c"),   # 无定位 → 巡航跳过
        ])

        window._cycle_issue(1)
        window._cycle_issue(1)
        window._cycle_issue(-1)
        assert got == [1000, 2000, 1000], f"巡航顺序错误：{got}"

    def test_jump_requested_opens_review_page_at_position(self, window, qapp, binaries, assets):
        """检查器「查看」：选中所在集、定位到问题时间点。"""
        self._seed(window, binaries, assets)

        window.workbench._on_jump_requested(45_000)
        assert window.workbench.selected_episode == 2, "45s 应落在第 2 集（30-60s）"
        assert window.workbench.stage_view.position == Fraction(45)

    def test_review_ops_buttons_have_hard_minimums(self, window):
        """回归：QSS min-height 不进 minimumSizeHint，按钮曾被布局压成细条、
        文字互相叠印（docs/screenshots/04-审核.png 缺陷）。"""
        page = window.workbench.review_pane
        for btn in (
            page.btn_set_boundary,
            page.btn_lock,
            page.btn_unlock,
            page.btn_confirm,
            page.btn_snap,
        ):
            assert btn.minimumHeight() >= 32, f"「{btn.text()}」缺少显式最小高度"
