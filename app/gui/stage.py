"""单页工作台的阶段裁决（纯逻辑，不依赖 Qt）。

单页工作台没有"页面切换"：界面形态完全由项目状态推导。本模块是唯一的
裁决入口，界面各处（三栏 stack、底栏文案与主动作、菜单启用态）都读它，
避免出现"底栏说待审核、左栏已显示集列表"这类自相矛盾。

四档阶段
--------
- ``EMPTY``     未导入源视频
- ``READY``     源片就绪、尚无方案（可开始分析）
- ``ANALYZING`` 分析线程运行中（由调用方传入，因为"线程在跑"不在 state 里）
- ``REVIEW``    已有方案（逐集审核）

``current_stage`` 之外只提供两件事：底栏主动作的文案与角色、底栏左侧的阶段
文案。界面结构的映射（哪一栏显示哪一屏）属于视图层，放在 workbench 里。
"""

from __future__ import annotations

from enum import Enum

from ..core.timebase import format_seconds_brief, format_timecode
from .state import ProjectState

__all__ = [
    "Stage",
    "current_stage",
    "primary_action",
    "is_confirmable",
    "stage_caption",
]


class Stage(str, Enum):
    EMPTY = "empty"
    READY = "ready"
    ANALYZING = "analyzing"
    REVIEW = "review"


def current_stage(state: ProjectState, analyzing: bool = False) -> Stage:
    """按项目状态裁决当前阶段。

    ``analyzing`` 由调用方传入（工作台持有分析线程引用），其余三档全部从
    state 推导：有方案即 REVIEW，有媒体即 READY，否则 EMPTY。分析中优先级
    最高——此时即便旧方案还在，界面也该让位给进度。
    """
    if analyzing:
        return Stage.ANALYZING
    if state.current_plan is not None:
        return Stage.REVIEW
    if state.media is not None:
        return Stage.READY
    return Stage.EMPTY


def primary_action(stage: Stage) -> tuple[str, str]:
    """底栏主动作的 (文案, 按钮角色)。

    角色决定 QSS：primary 是常规推进，danger 用于"取消分析"这类中断动作。
    """
    return {
        Stage.EMPTY: ("选择源视频…", "primary"),
        Stage.READY: ("▶  开始分析", "primary"),
        Stage.ANALYZING: ("取消分析", "danger"),
        Stage.REVIEW: ("导出…", "primary"),
    }[stage]


def is_confirmable(stage: Stage) -> bool:
    """该阶段是否有可确认的推进动作（EMPTY 只是打开文件对话框）。"""
    return stage is not Stage.EMPTY


def stage_caption(
    stage: Stage,
    state: ProjectState,
    selected_episode: int | None = None,
) -> str:
    """底栏左侧的一行阶段文案。

    REVIEW 阶段带上当前集与方案版本——底栏是用户不移动视线就能读到"我在哪"
    的地方，因此这里重复一点左栏信息是刻意的，不算冗余展示。
    """
    if stage is Stage.EMPTY:
        return "未导入源视频"
    media = state.media
    if media is None:                      # 理论上不可达，兜底避免崩
        return "未导入源视频"
    if stage is Stage.READY:
        fps = float(media.video.nominal_fps) if media.video else 0.0
        return (
            f"源片就绪 · {format_seconds_brief(media.timeline_duration)} · "
            f"{fps:.3f}fps · 待分析"
        )
    if stage is Stage.ANALYZING:
        return "分析进行中"
    plan = state.current_plan
    if plan is None:
        return "待审核"
    episodes = plan.episodes()
    index = max(1, min(selected_episode or 1, len(episodes)))
    episode = episodes[index - 1]
    return (
        f"第{episode.index:02d}集 · {format_timecode(episode.start_seconds)}—"
        f"{format_timecode(episode.end_seconds)} · 方案 v{plan.version} · "
        f"{len(episodes)} 集"
    )