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
    def test_five_pages_are_present(self, window):
        assert window.stack.count() == 5
        assert window.PAGES == ["导入", "设置", "分析", "审核", "导出"]

    def test_navigation_starts_at_import(self, window):
        assert window.stack.currentIndex() == 0
        assert not window.btn_prev.isEnabled()

    def test_next_is_blocked_without_media(self, window):
        window.nav.setCurrentRow(0)
        window.state.reset_media()
        ok, message = window.import_page.can_continue()
        assert not ok and "导入" in message


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
