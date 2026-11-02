"""时间轴缩放条测试（离屏运行）。

缩放条不持有视窗状态，因此这里只验证三件事：传给它的数值是否被正确呈现、
按钮在边界处是否置灰、点击是否发出对应请求。真正的缩放语义由时间轴测试
覆盖。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.gui.widgets.timeline_zoom import TimelineZoomBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bar(qapp):
    widget = TimelineZoomBar()
    widget.resize(600, TimelineZoomBar.HEIGHT)
    return widget


class TestInitialState:
    def test_starts_at_100_percent_with_fit_disabled(self, bar):
        assert bar.percent_label.text() == "100%"
        assert not bar.btn_fit.isEnabled()

    def test_height_is_fixed_to_24(self, bar):
        assert bar.height() == TimelineZoomBar.HEIGHT

    def test_tooltip_documents_zoom_gestures(self, bar):
        text = bar.toolTip()
        for token in (
            "Ctrl+=",
            "Ctrl+-",
            "Ctrl+0",
            "滚轮缩放",
            "Shift+滚轮",
            "中键平移",
        ):
            assert token in text


class TestSetView:
    def test_full_view_shows_total_range(self, bar):
        bar.set_view(0, 1800, 1800, False)
        assert bar.range_label.text() == "全片 00:00:00.000 – 00:30:00.000"
        assert bar.percent_label.text() == "100%"

    def test_zoomed_view_shows_visible_range(self, bar):
        bar.set_view(30, 60, 120, True)
        assert bar.range_label.text() == "可见 00:00:30.000 – 00:01:30.000"
        assert bar.percent_label.text() == "200%"

    def test_percent_rounds_half_up(self, bar):
        # 120 / 48 = 2.5 倍；银行家舍入会得到 200%，这里必须是 250%。
        bar.set_view(0, 48, 120, True)
        assert bar.percent_label.text() == "250%"

    def test_full_flag_wins_over_inconsistent_span(self, bar):
        # 调用方说未放大时，即使跨度传小了也应按全片呈现，避免状态自相矛盾。
        bar.set_view(0, 60, 120, False)
        assert bar.range_label.text().startswith("全片 ")
        assert bar.percent_label.text() == "100%"
        assert not bar.btn_fit.isEnabled()


class TestButtons:
    def test_clicks_emit_requests(self, bar):
        bar.set_view(30, 60, 120, True)
        seen: list[str] = []
        bar.zoom_out_requested.connect(lambda: seen.append("out"))
        bar.zoom_in_requested.connect(lambda: seen.append("in"))
        bar.fit_requested.connect(lambda: seen.append("fit"))

        bar.btn_zoom_out.click()
        bar.btn_zoom_in.click()
        bar.btn_fit.click()

        assert seen == ["out", "in", "fit"]

    def test_zoom_in_disabled_at_minimum_span(self, bar):
        bar.set_view(0, 2, 120, True)
        assert not bar.btn_zoom_in.isEnabled()
        assert bar.btn_zoom_out.isEnabled()
        assert bar.btn_fit.isEnabled()
        assert bar.percent_label.text() == "6000%"

    def test_zoom_out_and_fit_disabled_at_full_view(self, bar):
        bar.set_view(0, 120, 120, False)
        assert bar.btn_zoom_in.isEnabled()
        assert not bar.btn_zoom_out.isEnabled()
        assert not bar.btn_fit.isEnabled()

    def test_short_clip_has_no_zoom_range(self, bar):
        # min(total, 2s) 的跨度下限：不足 2 秒的片子没有可放大空间。
        bar.set_view(0, 1.5, 1.5, False)
        assert not bar.btn_zoom_in.isEnabled()
        assert not bar.btn_zoom_out.isEnabled()
        assert not bar.btn_fit.isEnabled()

    def test_disabled_buttons_do_not_emit(self, bar):
        bar.set_view(0, 120, 120, False)
        seen: list[str] = []
        bar.zoom_out_requested.connect(lambda: seen.append("out"))
        bar.fit_requested.connect(lambda: seen.append("fit"))

        bar.btn_zoom_out.click()
        bar.btn_fit.click()

        assert seen == []
