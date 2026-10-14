"""时间轴控件测试（离屏运行）。

三条硬约束在这里固化：
1. 秒 ↔ 像素换算精确可逆（Fraction，不经 float）；
2. 连续 set_playhead 不得重建静态层（播放时每帧都会调，重建就会掉帧）；
3. 锁定切点不可拖；拖动松手必须吸附到帧起点后再换算 ticks。

另附主题守卫：凡设置 font-size 的 QSS 规则必须同时写 font-family（Qt 会因
覆盖字号而重置字体族，符号字符会变豆腐块——本项目已踩过两次）。
"""

from __future__ import annotations

import os
import re
from fractions import Fraction

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtCore import QPoint, QPointF  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.plan import BoundaryPlan  # noqa: E402
from app.core.timebase import TimeBase  # noqa: E402
from app.gui.widgets.timeline import TimelineRuler  # noqa: E402

TB = TimeBase(1, 25)          # 1 tick = 1 帧 = 40ms
FPS = 25
TOTAL_TICKS = 3000            # 120 秒
WIDTH = 800
# 测试用粗吸附网格（5 帧 = 0.2s）：这样"先吸附再换算"与"直接换算"结果不同，
# 才能真正验证控件走的是 media.snap_to_frame 而不是自己 round 一下。
SNAP_STEP = 5


class _Media:
    """时间轴只读这几个字段：时长、VFR 标记、吸附与帧号。"""

    def __init__(self, seconds: int = 120, vfr: bool = False) -> None:
        self.timeline_duration = Fraction(seconds)
        self.is_vfr = vfr

    def snap_to_frame(self, seconds: Fraction) -> tuple[int, Fraction]:
        index = max(0, round(float(seconds) * FPS / SNAP_STEP) * SNAP_STEP)
        return index, Fraction(index, FPS)

    def frame_index_at(self, seconds: Fraction) -> int:
        return max(0, int(seconds * FPS))


class _Event:
    """只实现控件用到的那两个方法，避免构造真实的 QMouseEvent 序列。"""

    def __init__(self, x: float, y: float = 30.0) -> None:
        self._point = QPointF(x, y)
        self._global = QPoint(int(x), int(y))

    def position(self) -> QPointF:
        return self._point

    def globalPos(self) -> QPoint:      # noqa: N802 - Qt 命名
        return self._global


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ruler(qapp):
    widget = TimelineRuler()
    widget.resize(WIDTH, 62)
    return widget


def _plan() -> BoundaryPlan:
    # 4 集等分：切点在 30 / 60 / 90 秒（750 / 1500 / 2250 ticks）
    return BoundaryPlan.uniform(4, TB, TOTAL_TICKS)


def _paint(widget: TimelineRuler):
    """强制走一遍 paintEvent（静态层在这一步按需重建）。"""
    pixmap = QPixmap(widget.size())
    widget.render(pixmap)
    return pixmap


class TestGeometry:
    def test_x_from_seconds_matches_track_layout(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        assert ruler._x_from_seconds(Fraction(0)) == pytest.approx(ruler.MARGIN)
        assert ruler._x_from_seconds(Fraction(60)) == pytest.approx(400.0, abs=0.01)
        assert ruler._x_from_seconds(Fraction(120)) == pytest.approx(
            WIDTH - ruler.MARGIN, abs=0.01
        )

    def test_round_trip_is_exact(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        for seconds in (0, 7, 30, 60, 119, 120):
            x = ruler._x_from_seconds(Fraction(seconds))
            assert ruler._seconds_from_x(x) == Fraction(seconds)

    def test_seconds_from_x_clamps_to_range(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        assert ruler._seconds_from_x(-500) == Fraction(0)
        assert ruler._seconds_from_x(99999) == Fraction(120)


class TestStaticLayerCache:
    def test_playhead_updates_do_not_rebuild_static_layer(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        _paint(ruler)
        baseline = ruler._static_rebuild_count
        assert baseline == 1

        for step in range(1000):
            ruler.set_playhead(Fraction(step % 121))

        _paint(ruler)
        assert ruler._static_rebuild_count == baseline, "播放头移动不应重建静态层"

    def test_selection_change_rebuilds(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        _paint(ruler)
        baseline = ruler._static_rebuild_count
        ruler.set_selected_episode(3)
        _paint(ruler)
        assert ruler._static_rebuild_count == baseline + 1

    def test_plan_change_rebuilds(self, ruler):
        plan = _plan()
        ruler.set_project(_Media(), plan, {}, 1)
        _paint(ruler)
        baseline = ruler._static_rebuild_count
        plan.move_boundary(1, TB.seconds_to_ticks(Fraction(40)))
        ruler.set_project(_Media(), plan, {}, 1)
        _paint(ruler)
        assert ruler._static_rebuild_count == baseline + 1

    def test_static_layer_is_dpi_aware(self, ruler, monkeypatch):
        """静态层位图必须按 devicePixelRatio 建。

        不设 DPR 的话，125%/150% 缩放的屏幕上这张位图会被插值放大——
        时间码与刻度线全部发虚（用户报「数字看着很模糊」就是这个）。
        """
        monkeypatch.setattr(type(ruler), "devicePixelRatioF", lambda self: 2.0)
        pixmap = ruler._rebuild_static()
        assert pixmap.devicePixelRatio() == pytest.approx(2.0)
        assert pixmap.width() == ruler.width() * 2
        assert pixmap.height() == ruler.height() * 2

    def test_dpi_change_rebuilds_static_layer(self, ruler, monkeypatch):
        """换到缩放比不同的屏幕要重建，否则位图分辨率对不上。"""
        ruler.set_project(_Media(), None, {}, None)
        _paint(ruler)
        before = ruler._static_rebuild_count
        monkeypatch.setattr(type(ruler), "devicePixelRatioF", lambda self: 2.0)
        _paint(ruler)
        assert ruler._static_rebuild_count > before, (
            "DPR 变了却没重建静态层，字会发虚"
        )

    def test_error_marker_rebuilds_and_clears(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        _paint(ruler)
        baseline = ruler._static_rebuild_count
        ruler.set_error(2, "第二集时长超出上限")
        _paint(ruler)
        assert ruler._static_rebuild_count == baseline + 1
        ruler.clear_error()
        _paint(ruler)
        assert ruler._static_rebuild_count == baseline + 2


class TestInteraction:
    def test_click_track_seeks_and_selects_episode(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        seeks: list[float] = []
        episodes: list[int] = []
        ruler.seek_requested.connect(seeks.append)
        ruler.episode_activated.connect(episodes.append)

        x = ruler._x_from_seconds(Fraction(75))     # 落在第 3 集（60–90s）
        ruler.mousePressEvent(_Event(x))
        ruler.mouseReleaseEvent(_Event(x))

        assert episodes and episodes[0] == 3
        assert seeks and seeks[0] == pytest.approx(75.0, abs=0.05)

    def test_drag_marker_snaps_to_frame_and_emits_ticks(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        moves: list[tuple[int, int]] = []
        ruler.boundary_move_requested.connect(lambda i, t: moves.append((i, t)))

        start_x = ruler._x_from_seconds(Fraction(30))       # 第 1 集结束切点
        ruler.mousePressEvent(_Event(start_x))
        end_x = ruler._x_from_seconds(Fraction(36.37))      # 非帧对齐位置
        ruler.mouseMoveEvent(_Event(end_x))
        ruler.mouseReleaseEvent(_Event(end_x))

        assert moves, "拖动切点应发出 boundary_move_requested"
        index, ticks = moves[-1]
        assert index == 1
        # 36.37s 直接换算得 909 ticks，吸附到 5 帧网格后是第 910 帧 → 910 ticks。
        # 只有真的走了 media.snap_to_frame 才能得到 910。
        assert ticks == 910, f"应吸附到帧起点后再换算，实际 ticks={ticks}"

    def test_locked_marker_cannot_be_dragged(self, ruler):
        plan = _plan()
        plan.lock_boundary(1, True)
        ruler.set_project(_Media(), plan, {}, 1)
        moves: list[tuple[int, int]] = []
        ruler.boundary_move_requested.connect(lambda i, t: moves.append((i, t)))

        x = ruler._x_from_seconds(Fraction(30))
        ruler.mousePressEvent(_Event(x))
        ruler.mouseMoveEvent(_Event(ruler._x_from_seconds(Fraction(45))))
        ruler.mouseReleaseEvent(_Event(ruler._x_from_seconds(Fraction(45))))

        assert moves == [], "锁定切点不允许拖动"

    def test_playhead_drag_emits_seek(self, ruler):
        ruler.set_project(_Media(), _plan(), {}, 1)
        seeks: list[float] = []
        ruler.seek_requested.connect(seeks.append)

        x0 = ruler._x_from_seconds(Fraction(10))
        ruler.mousePressEvent(_Event(x0))        # 空白处按下 → 直接跳一次
        ruler.set_playhead(Fraction(10))
        ruler.mousePressEvent(_Event(x0))        # 命中播放头 → 进入拖动
        ruler.mouseMoveEvent(_Event(ruler._x_from_seconds(Fraction(50))))
        ruler.mouseReleaseEvent(_Event(ruler._x_from_seconds(Fraction(50))))

        assert seeks[-1] == pytest.approx(50.0, abs=0.05)

    def test_playhead_drag_stays_where_released(self, ruler):
        """回归：拖动播放头松手后必须停在松手处。

        曾经的现象是"拖完弹回原位"：松手时先发 seek_requested、后清拖动标记，
        而接收方（StageView.goto）会同步回写 set_playhead，此时护栏仍生效 →
        这次回写被吞掉，重绘时又用回旧的 _playhead。
        """
        ruler.set_project(_Media(), _plan(), {}, 1)

        # 忠实模拟 StageView 的接线：seek → goto(秒) → 回写播放头
        ruler.seek_requested.connect(lambda seconds: ruler.set_playhead(seconds))

        x0 = ruler._x_from_seconds(Fraction(10))
        ruler.mousePressEvent(_Event(x0))
        end_x = ruler._x_from_seconds(Fraction(50))
        ruler.mouseMoveEvent(_Event(end_x))

        ruler.mouseReleaseEvent(_Event(end_x))

        assert ruler.playhead_seconds() == Fraction(50), (
            f"松手后播放头应停在 50s，实际 {float(ruler.playhead_seconds()):.3f}s"
        )
        assert ruler._drag is None, "松手后拖动状态必须清掉"

    def test_vfr_source_paints_hint_without_crash(self, ruler):
        ruler.set_project(_Media(vfr=True), _plan(), {}, 1)
        _paint(ruler)

    def test_paints_placeholder_without_media(self, ruler):
        _paint(ruler)          # 不应抛异常


class TestStylesheetGuard:
    """凡设置 font-size 的规则必须同时写 font-family。

    Qt 的 QSS 覆盖 font-size 时会把字体族重置为默认值，符号字符（⚙ 🤖 🎬）
    因此落到无字形的字体上渲染成豆腐块。本项目已踩过两次，这里做机械守卫。
    """

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_every_font_size_rule_declares_family(self, mode):
        from app.gui.theme import build_stylesheet

        sheet = build_stylesheet(mode)
        blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", sheet)
        offenders = [
            selector.strip()
            for selector, body in blocks
            if "font-size" in body and "font-family" not in body
        ]
        assert not offenders, f"{mode} 主题下缺少 font-family 的规则：{offenders}"