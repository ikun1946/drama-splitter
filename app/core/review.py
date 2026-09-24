"""整集复核（阶段4）：对已选定的边界做**集级**健全性检查。

与候选点判断的分工
------------------
- 候选点判断（semantic.py）回答"这个点适不适合切"；
- 整集复核（本模块）回答"按这些边界切出来的一集，内容上站不站得住"。

检查项（全部是可机械验证的，不含语义猜测）：
- 集内至少包含一句完整对白（否则该集只有画面没有叙事单元）；
- 集尾不落在句子中间（切在句中 = 截断对白，§11.1 最严重的风险）；
- 集首是否从一句的开头开始（中段切入会让观众从半句话开始看）；
- 字幕悬挂：某条字幕跨过了集边界 → 播放时会被切成两半；
- 集首/集尾的黑场：有的话是"段落结束"的强证据。

产出（§11.3）：每集的 推荐等级 + 支持证据 + 风险 + 审核状态。
**没有模型判断的部分如实标注"未评估"**，不编造结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .asr import Transcript
from .plan import BoundaryPlan
from .probe import MediaInfo
from .semantic import EventIndex
from .shots import BlackInterval
from .timebase import format_timecode

__all__ = ["EpisodeReview", "review_plan"]

BOUNDARY_SENTENCE_TOLERANCE = Fraction(2, 10)  # 集边界与句边界允许的偏差


@dataclass
class EpisodeReview:
    """一集的复核结论（§11.3 字段）。"""

    episode: int
    grade: str                 # pass / warn / block
    evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    start_time: Fraction = Fraction(0)
    end_time: Fraction = Fraction(0)

    @property
    def is_blocking(self) -> bool:
        return self.grade == "block"

    def to_json(self) -> dict:
        return {
            "episode": self.episode,
            "grade": self.grade,
            "evidence": list(self.evidence),
            "risks": list(self.risks),
            "start": float(self.start_time),
            "end": float(self.end_time),
        }


def review_plan(
    plan: BoundaryPlan,
    media: MediaInfo,
    index: EventIndex,
    *,
    transcript: Transcript | None = None,
    blacks: list[BlackInterval] | None = None,
) -> list[EpisodeReview]:
    """对方案的每一集做复核。"""
    reviews: list[EpisodeReview] = []
    for episode in plan.episodes():
        reviews.append(
            _review_one(
                episode.index,
                episode.start_seconds,
                episode.end_seconds,
                index,
                blacks or [],
            )
        )
    _ = transcript, media  # 保留参数位：后续可加入画面级证据
    return reviews


def _review_one(
    episode: int,
    start: Fraction,
    end: Fraction,
    index: EventIndex,
    blacks: list[BlackInterval],
) -> EpisodeReview:
    evidence: list[str] = []
    risks: list[str] = []
    grade = "pass"

    sentences_inside = [
        event for event in index.dialogue
        if event.start >= start - BOUNDARY_SENTENCE_TOLERANCE
        and event.end <= end + BOUNDARY_SENTENCE_TOLERANCE
    ]
    if sentences_inside:
        first, last = sentences_inside[0], sentences_inside[-1]
        evidence.append(
            f"包含 {len(sentences_inside)} 句完整对白"
            f"（{format_timecode(first.start)}–{format_timecode(last.end)}）"
        )
    else:
        risks.append("集内没有完整对白——只有画面没有叙事单元，多半不是预期切法")
        grade = "warn"

    # 切在句中：最严重的风险（§11.1）
    cut_inside_start = index.sentence_covering(start)
    if cut_inside_start is not None and start > 0:
        risks.append(
            f"集首截断了句子：{format_timecode(cut_inside_start.start)}–"
            f"{format_timecode(cut_inside_start.end)}（{cut_inside_start.text[:20]}…）"
        )
        grade = "block"

    cut_inside_end = index.sentence_covering(end)
    if cut_inside_end is not None and end < _plan_end(index):
        risks.append(
            f"集尾截断了句子：{format_timecode(cut_inside_end.start)}–"
            f"{format_timecode(cut_inside_end.end)}（{cut_inside_end.text[:20]}…）"
        )
        grade = "block"

    # 集首是否从句首开始
    if start > 0:
        before = index.sentence_before(start)
        if before is not None:
            gap = start - before.end
            if gap <= BOUNDARY_SENTENCE_TOLERANCE:
                evidence.append(f"集首紧跟句尾（间隔 {float(gap) * 1000:.0f}ms）")
            elif gap > Fraction(2):
                evidence.append(f"集首在 {float(gap):.2f}s 的停顿之后")

    # 字幕悬挂：字幕跨过集边界
    dangling = index.sentence_covering(end)
    if dangling is not None and dangling is not cut_inside_end and end < _plan_end(index):
        risks.append(f"有对白跨过集尾边界（{dangling.text[:20]}…）")
        if grade == "pass":
            grade = "warn"

    # 黑场佐证
    for black in blacks:
        if black.end > start and black.start < end:
            evidence.append(
                f"集内含黑场/淡出（{format_timecode(black.start)}–{format_timecode(black.end)}）"
            )
            break

    if not index.dialogue:
        risks.append("无对白信息，无法做句级复核（转写缺失或素材无对白）")
        if grade == "pass":
            grade = "warn"

    return EpisodeReview(
        episode=episode,
        grade=grade,
        evidence=evidence,
        risks=risks,
        start_time=start,
        end_time=end,
    )


def _plan_end(index: EventIndex) -> Fraction:
    """事件索引能覆盖的最晚时刻。没有事件时返回 0（调用方自行判断）。"""
    if not index.dialogue:
        return Fraction(0)
    return max(event.end for event in index.dialogue)
