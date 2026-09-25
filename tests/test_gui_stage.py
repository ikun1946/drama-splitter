"""单页工作台的阶段裁决测试（纯逻辑，不需要 Qt）。

阶段判定是三栏 stack、底栏文案、主动作按钮的唯一依据，错了会同时错三处，
因此把四档状态与优先级（分析中 > 有方案 > 有媒体）都固化在这里。
"""

from __future__ import annotations

from fractions import Fraction

from app.core.plan import BoundaryPlan
from app.core.timebase import TimeBase
from app.gui.stage import (
    Stage,
    current_stage,
    is_confirmable,
    primary_action,
    stage_caption,
)
from app.gui.state import ProjectState

TB = TimeBase(1, 25)          # 1 tick = 1 帧
TOTAL_TICKS = 3000            # 120 秒 / 25fps


class _Video:
    nominal_fps = Fraction(25)


class _Media:
    """只提供 stage.py 与时间轴用到的字段，避免为一个纯函数测试起 ffprobe。"""

    def __init__(self, seconds: int = 120) -> None:
        self.timeline_duration = Fraction(seconds)
        self.video = _Video()
        self.is_vfr = False


def _plan() -> BoundaryPlan:
    return BoundaryPlan.uniform(4, TB, TOTAL_TICKS)


def _state(*, media: bool = False, plan: bool = False) -> ProjectState:
    state = ProjectState()
    if media or plan:
        state.media = _Media()
    if plan:
        state.plans = [_plan()]
    return state


class TestStageDecision:
    def test_empty_without_media(self):
        assert current_stage(_state()) is Stage.EMPTY

    def test_ready_with_media_only(self):
        assert current_stage(_state(media=True)) is Stage.READY

    def test_review_with_plan(self):
        assert current_stage(_state(plan=True)) is Stage.REVIEW

    def test_analyzing_wins_over_existing_plan(self):
        # 重新分析时旧方案还在，界面仍应让位给进度
        assert current_stage(_state(plan=True), analyzing=True) is Stage.ANALYZING

    def test_analyzing_without_media_is_still_analyzing(self):
        assert current_stage(_state(), analyzing=True) is Stage.ANALYZING


class TestStagePresentation:
    def test_primary_action_per_stage(self):
        assert primary_action(Stage.EMPTY) == ("选择源视频…", "primary")
        assert primary_action(Stage.READY) == ("▶  开始分析", "primary")
        assert primary_action(Stage.ANALYZING) == ("取消分析", "danger")
        assert primary_action(Stage.REVIEW) == ("导出…", "primary")

    def test_confirmable_excludes_empty(self):
        assert not is_confirmable(Stage.EMPTY)
        assert is_confirmable(Stage.READY)
        assert is_confirmable(Stage.ANALYZING)
        assert is_confirmable(Stage.REVIEW)

    def test_caption_empty(self):
        assert stage_caption(Stage.EMPTY, _state()) == "未导入源视频"

    def test_caption_ready_mentions_fps_and_wait(self):
        caption = stage_caption(Stage.READY, _state(media=True))
        assert "25.000fps" in caption
        assert "待分析" in caption

    def test_caption_analyzing(self):
        assert stage_caption(Stage.ANALYZING, _state(plan=True)) == "分析进行中"

    def test_caption_review_carries_episode_and_version(self):
        caption = stage_caption(Stage.REVIEW, _state(plan=True))
        assert "第01集" in caption
        assert "方案 v1" in caption
        assert "4 集" in caption

    def test_caption_review_follows_selected_episode(self):
        caption = stage_caption(Stage.REVIEW, _state(plan=True), selected_episode=3)
        assert "第03集" in caption

    def test_caption_review_clamps_out_of_range_selection(self):
        caption = stage_caption(Stage.REVIEW, _state(plan=True), selected_episode=99)
        assert "第04集" in caption