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
