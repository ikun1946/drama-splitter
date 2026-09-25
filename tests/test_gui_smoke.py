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

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core.probe import probe_media  # noqa: E402
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
    win.review_page._player.stop()
    win.close()


class TestWindowConstruction:
    def test_all_pages_are_present(self, window):
        # 方案 §五：核心工作流只保留三页（工作台 / 审核 / 导出），
        # 设置与模型降级为顶栏图标打开的抽屉，不占 stack 位。
        assert window.stack.count() == 3
        assert window.PAGES == ["工作台", "设置", "审核", "导出", "模型"]
        assert set(window.DRAWER_INDICES.values()) == {"settings", "model"}

    def test_workbench_composes_import_and_analysis(self, window):
        # 工作台一页承担导入 + 分析：两个子视图都在，且别名指向同一对象
        wb = window.workbench_page
        assert window.import_page is wb.import_view
        assert window.analysis_page is wb.analysis_view
        assert wb.import_view.parent() is not None
        assert wb.analysis_view.parent() is not None

    def test_workbench_splitter_is_actually_resizable(self, window, qapp):
        """分割条必须能被程序与用户调整。

        回归用例：导入区四张卡片叠起来最小高度接近 700px，两个子控件的最小
        高度之和超过可视高度时，Qt 会静默忽略 setSizes() 与 moveSplitter()——
        实测表现为 sizes() 恒等于均分、moveSplitter() 直接返回 None。
        修法是给两半各套一层滚动容器。这里断言"能设得动"，而不是断言某个
        具体比例（比例随窗口尺寸变化）。
        """
        wb = window.workbench_page
        window.resize(1440, 900)
        window.show()          # 布局只在 show 之后才真正分配高度
        qapp.processEvents()
        try:
            total = sum(wb.splitter.sizes())
            assert total > 0, "分割器未获得可用高度"

            upper = max(1, int(total * 0.25))
            wb.splitter.setSizes([upper, total - upper])
            qapp.processEvents()
            got_upper, got_lower = wb.splitter.sizes()
            assert abs(got_upper - upper) <= 2, (
                f"分割器忽略了 setSizes：请求上半 {upper}px，实际 {got_upper}px"
                f"（{wb.splitter.sizes()}）——子控件最小高度可能又超过了可用高度"
            )
            assert got_lower > got_upper, "分析区应拿到更多高度"
        finally:
            window.hide()      # 恢复 fixture 原状，不影响同模块后续用例

    def test_page_rows_map_to_stack(self, window):
        # 抽屉行（1/4）不切 stack；工作区行（0/2/3）依次对应 stack 0/1/2
        for row, expected in ((0, 0), (2, 1), (3, 2)):
            window.nav.setCurrentRow(row)
            assert window.stack.currentIndex() == expected

    def test_drawer_rows_do_not_switch_stack(self, window):
        window.nav.setCurrentRow(0)
        before = window.stack.currentIndex()
        for row in (1, 4):
            window.nav.setCurrentRow(row)
            assert window.stack.currentIndex() == before

    def test_navigation_starts_at_import(self, window):
        window.nav.setCurrentRow(0)
        assert window.stack.currentIndex() == 0
        assert not window.btn_prev.isEnabled()

    def test_next_is_blocked_without_media(self, window):
        window.nav.setCurrentRow(0)
        window.state.reset_media()
        ok, message = window.import_page.can_continue()
        assert not ok and "导入" in message


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
            assert window.app_bar.btn_theme.text() != ""

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
            window.app_bar.set_theme_label(current_mode())

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

    def test_model_button_targets_drawer_row(self, window):
        """回归：🤖 按钮曾指向不存在的第 6 行，抽屉永远打不开。"""
        window.app_bar.model_clicked.emit()
        assert window.nav.currentRow() == 4
        window.nav.setCurrentRow(0)


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
        window.import_page._probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.import_page._worker and not window.import_page._worker.isRunning(),
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
        window.import_page._probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.import_page._worker and not window.import_page._worker.isRunning(),
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
        window.settings_page.apply_settings(original)
        collected = window.settings_page.collect_settings()
        assert collected.split_mode == SplitMode.TARGET_EPISODE_COUNT
        assert collected.target_episode_count == 12
        assert collected.count_policy == CountPolicy.FLEXIBLE
        assert collected.allowed_count_min == 10
        assert collected.allowed_count_max == 15
        assert collected.range.tolerance == Fraction(1, 10)
        assert collected.strategy == Strategy.SUSPENSE
        assert collected.allow_episode_exceptions is True



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

        page = window.settings_page
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

        page = window.settings_page
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

        page = window.settings_page
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
        page = window.settings_page

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
        page = window.settings_page
        page.radio_by_count.setChecked(True)
        page.range_percent.setChecked(True)
        page.tolerance_spin.setValue(20.0)
        page.count_spin.setValue(4)

        settings = page.collect_settings()
        derived = settings.derive(Fraction(120))
        assert derived.min_duration == Fraction(24)
        assert derived.max_duration == Fraction(36)


class _FakeDialog:
    """录制型 QMessageBox 替身。

    类方法拦截 warning/information/critical 并记录入参；实例方法支持
    addButton/exec/clickedButton，供导出前置检查对话框（§37）的流程测试。
    """

    calls: list[tuple] = []
    ButtonRole = QMessageBox.ButtonRole

    @classmethod
    def warning(cls, *args):
        cls.calls.append(("warning",) + args)

    @classmethod
    def information(cls, *args):
        cls.calls.append(("information",) + args)

    @classmethod
    def critical(cls, *args):
        cls.calls.append(("critical",) + args)

    def __init__(self, *args, **kwargs):
        self.text_value = ""
        self.informative = ""
        self.buttons: list[tuple] = []
        self.clicked = None

    def setWindowTitle(self, title):
        pass

    def setText(self, text):
        self.text_value = text

    def setInformativeText(self, text):
        self.informative = text

    def addButton(self, text, role):
        token = (text, role)
        self.buttons.append(token)
        return token

    def exec(self):
        pass

    def clickedButton(self):
        return self.clicked


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
        return media, plan

    def test_export_precheck_counts_unreviewed_cuts(self, window, binaries, assets):
        """§37：未确认方案 → 未审核切点数 = 集数 - 1（首尾是片头片尾）。"""
        from app.gui.pages.export_page import export_precheck

        _, plan = self._seed(window, binaries, assets)
        blocking, warnings, unreviewed = export_precheck(plan, window.state.settings)
        assert blocking == []
        assert warnings == []
        assert unreviewed == 3

        plan.semantic_review_status = "confirmed"
        _, _, unreviewed = export_precheck(plan, window.state.settings)
        assert unreviewed == 0

    def test_precheck_dialog_back_to_review(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：前置检查弹「继续导出 / 返回审核」，选返回 → 切审核页、不导出。"""
        from app.gui.pages import export_page as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        assert plan.semantic_review_status == "pending"

        captured = {}
        window.export_page.review_requested.connect(lambda: captured.update(fired=True))

        _FakeDialog.calls = []
        monkeypatch.setattr(ep, "QMessageBox", _FakeDialog)
        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []

        def choose_back(self_dialog):
            self_dialog.clicked = self_dialog.buttons[-1]   # 「返回审核」

        monkeypatch.setattr(_FakeDialog, "exec", choose_back)
        window.export_page._start(None)

        assert captured.get("fired"), "选择「返回审核」应发出 review_requested"
        assert window.nav.currentRow() == 2, "应切回审核页"
        assert not _FakeWorker.instances, "不应启动导出线程"

    def test_precheck_dialog_continue_exports(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：选「继续导出」→ 走到启动导出（用户有意忽略未审核项）。"""
        from app.gui.pages import export_page as ep

        self._seed(window, binaries, assets)
        window.state.output_root = tmp_path

        _FakeDialog.calls = []
        monkeypatch.setattr(ep, "QMessageBox", _FakeDialog)
        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []

        def choose_continue(self_dialog):
            self_dialog.clicked = self_dialog.buttons[0]    # 「继续导出」

        monkeypatch.setattr(_FakeDialog, "exec", choose_continue)
        window.export_page._start(None)

        assert len(_FakeWorker.instances) == 1, "继续导出应启动一次导出线程"
        worker = _FakeWorker.instances[0]
        assert worker.kwargs.get("only_episodes") is None

    def test_hard_constraints_still_block_export(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：硬约束问题仍直接拦截，不给「继续导出」的选项。"""
        from app.gui.pages import export_page as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        # 破坏硬约束：起点不再从 0 开始 → NOT_START_AT_ZERO（阻断）
        plan.boundary_ticks = [plan.boundary_ticks[0] + 1000, plan.boundary_ticks[-1]]

        _FakeDialog.calls = []
        monkeypatch.setattr(ep, "QMessageBox", _FakeDialog)
        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []

        window.export_page._start(None)

        assert any(call[0] == "warning" for call in _FakeDialog.calls), "硬约束应弹警告"
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
        """Esc 分层：全屏时 Esc 只退全屏，不关抽屉（优先级最高）。"""
        window.settings_drawer.show()
        try:
            window._toggle_fullscreen()
            window._escape_pressed()
            assert not window.isFullScreen(), "全屏 + 抽屉同开时 Esc 应先退全屏"
        finally:
            self._hide_drawer(qapp, window.settings_drawer)

    def test_esc_closes_open_drawer(self, window, qapp):
        """Esc 分层：非全屏时 Esc 关闭打开中的抽屉（与 ✕ 同一条动画路径）。"""
        import time

        window.settings_drawer.show()
        try:
            assert window.settings_drawer.isVisible(), "前置：抽屉已打开"
            window._escape_pressed()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                qapp.processEvents()
                if not window.settings_drawer.isVisible():
                    break
                time.sleep(0.02)
            assert not window.settings_drawer.isVisible(), "Esc 应关闭抽屉"
        finally:
            self._hide_drawer(qapp, window.settings_drawer)

    @staticmethod
    def _hide_drawer(qapp, drawer) -> None:
        """模块级 window 夹具是共享的：把抽屉彻底关掉，避免动画残留污染后续用例。"""
        import time

        drawer.hide()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            qapp.processEvents()
            if not drawer.isVisible():
                return
            time.sleep(0.02)

    def test_ctrl_e_navigates_and_warns_without_plan(self, window, qapp, monkeypatch):
        """Ctrl+E：无方案时切到导出页并弹「请先完成导入、分析与审核」。"""
        from app.gui.pages import export_page as ep

        window.state.reset_media()
        window.state.plans = []
        window.state.plan_cursor = 0
        window.nav.setCurrentRow(0)

        _FakeDialog.calls = []
        monkeypatch.setattr(ep, "QMessageBox", _FakeDialog)

        window._start_export()
        assert window.nav.currentRow() == 3
        assert any("请先完成导入、分析与审核" in str(call) for call in _FakeDialog.calls)

    def test_return_confirms_episode_on_review_page(self, window, qapp, binaries, assets):
        """§54：回车 = 确认当前集（挂在审核页上，仅审核页内触发）。"""
        from PySide6.QtGui import QShortcut

        _, plan = self._seed(window, binaries, assets)
        assert plan.semantic_review_status == "pending"
        window.nav.setCurrentRow(2)

        return_shortcuts = [
            sc for sc in window.findChildren(QShortcut)
            if sc.parent() is window.review_page and sc.key().toString() == "Return"
        ]
        assert len(return_shortcuts) == 1, "Return 快捷键应唯一且挂在审核页上"
        return_shortcuts[0].activated.emit()

        assert plan.semantic_review_status == "confirmed", "回车应触发确认当前集"

    def test_inspector_cycles_jumpable_issues(self, window):
        """N/P 巡航：只走有定位的问题项，N/P 方向相反且可循环。"""
        from app.gui.widgets import InspectorIssue

        inspector = window.workbench_page.inspector
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
        """检查器「查看」：切到审核页、选中所在集、定位到问题时间点。"""
        window.nav.setCurrentRow(0)
        self._seed(window, binaries, assets)

        window._jump_to_issue(45_000)
        assert window.nav.currentRow() == 2
        assert window.review_page._current_episode == 2, "45s 应落在第 2 集（30-60s）"
        assert window.review_page._position == Fraction(45)

    def test_review_ops_buttons_have_hard_minimums(self, window):
        """回归：QSS min-height 不进 minimumSizeHint，按钮曾被布局压成细条、
        文字互相叠印（docs/screenshots/04-审核.png 缺陷）。"""
        page = window.review_page
        for btn in (
            page.btn_set_boundary,
            page.btn_lock,
            page.btn_unlock,
            page.btn_confirm,
            page.btn_snap,
        ):
            assert btn.minimumHeight() >= 32, f"「{btn.text()}」缺少显式最小高度"
