"""候选点数据库：收集、合并、评分与限流。

设计依据：
- §8.2 候选来源与合并：收集对白句末、语音间隙、镜头切换、黑场/淡出、人工标记。
  每个候选点记录**源时间、实际可切帧边界、来源、前后台词、局部画面证据和风险**。
  相近候选点可按可配置距离合并，但应**保留全部证据**，并从已有真实帧边界中
  **选代表点**；**不能取几个时间码的平均值而落到一句话中间**。
- §8.4 控制分析规模：先廉价规则筛选，再对优质候选做语义分析；限制每个窗口的
  候选数量及总调用预算；**若为限流缩小了搜索范围，必须记录限制**。
- §11.2 可比较的评分：软特征统一到同一量纲，权重与惩罚保存在策略配置中
  （先用样片标定再确定默认值）。
- §11.3 删除伪精确概率：用**推荐等级 + 支持证据 + 风险 + 审核状态**，
  综合评分只作同一体系内的排序分，不是正确率概率。

本模块是阶段3 的交付物，也是阶段2 动态规划的输入：没有候选点就没有候选图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .asr import Transcript, TranscriptSentence
from .probe import LEVEL_INFO, LEVEL_WARN, Issue
from .shots import BlackInterval, SceneBoundary
from .timebase import format_seconds_brief, format_timecode, round_half_up

__all__ = [
    "CandidateSource",
    "CandidatePoint",
    "CandidateSet",
    "RuleWeights",
    "collect_candidates",
    "merge_candidates",
    "limit_candidates",
    "GRADE_PRIORITY",
    "GRADE_USABLE",
    "GRADE_REVIEW",
]

# 候选来源。用常量而非枚举，便于配置文件里直接书写。
CandidateSource = str
SOURCE_SENTENCE_END = "sentence_end"     # 对白句末
SOURCE_SPEECH_GAP = "speech_gap"         # 语音间隙
SOURCE_SHOT_CUT = "shot_cut"             # 镜头切换
SOURCE_BLACK = "black"                   # 黑场 / 淡出
SOURCE_MANUAL = "manual"                 # 人工标记

SOURCE_LABELS = {
    SOURCE_SENTENCE_END: "对白句末",
    SOURCE_SPEECH_GAP: "语音间隙",
    SOURCE_SHOT_CUT: "镜头切换",
    SOURCE_BLACK: "黑场/淡出",
    SOURCE_MANUAL: "人工标记",
}

# 推荐等级（§11.3：不用"置信度 94%"这类伪精确概率）
GRADE_PRIORITY = "优先推荐"
GRADE_USABLE = "可用"
GRADE_REVIEW = "需人工复核"


@dataclass
class RuleWeights:
    """规则评分的权重与惩罚。

    §11.2 要求"权重和惩罚保存在策略配置中，先用样片标定再确定默认值"。
    这里的默认值是**先验设定**，尚未用真实短剧标定——因此评分只用于排序与
    分级，不对外声称正确率。改动这些值会改变排序，请配合
    `tools/eval_candidates.py` 之类的对照工具使用。
    """

    sentence_end: float = 0.40      # 对白在此结束
    speech_gap: float = 0.25        # 处于语音间隙中
    gap_length: float = 0.20        # 间隙长度的归一化贡献
    shot_cut: float = 0.15          # 附近有镜头切换（仅作佐证，不可单独成尾）
    in_black: float = 0.25          # 落在黑场/淡出中
    penalty_next_speech_too_close: float = 0.35  # 距下一句过近 → 可能截断台词
    penalty_no_evidence: float = 0.30            # 无任何强证据

    max_gap_for_full_credit: float = 1.5  # 间隙达到此长度即给满分
    too_close_seconds: float = 0.3        # 与下一句开始的距离低于此值算过近


@dataclass
class CandidatePoint:
    """一个候选切点。"""

    time: Fraction                # 源时间（代表点）
    frame_index: int              # 实际可切帧边界
    sources: list[str] = field(default_factory=list)
    score: float = 0.0
    grade: str = GRADE_REVIEW
    evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    speech_before: str = ""
    speech_after: str = ""
    scene_score: float | None = None   # 附近镜头切换的检测置信度
    in_black: bool = False
    gap_after: Fraction | None = None  # 该点之后的句间停顿长度
    review_status: str = "pending"
    merged_from: list[Fraction] = field(default_factory=list)

    @property
    def source_labels(self) -> str:
        return "／".join(SOURCE_LABELS.get(s, s) for s in self.sources)

    def shift_to(self, time: Fraction, frame_index: int) -> None:
        """把代表点移到另一个**真实帧边界**上。

        §8.2 明确禁止取平均——合并后的代表点必须落在真实可切帧上，
        否则会落进一句话中间。
        """
        self.time = time
        self.frame_index = frame_index

    def describe(self) -> str:
        return (
            f"{format_timecode(self.time)}（第 {self.frame_index} 帧）"
            f"｜{self.source_labels}｜{self.grade}（{self.score:.2f}）"
        )

    def review_card(self) -> str:
        """审核卡片文本（§13.2 的字段，§11.3 的证据/风险/状态）。"""
        lines = [
            f"候选切点｜{format_timecode(self.time)}｜第 {self.frame_index} 帧",
            f"推荐等级：{self.grade}",
            f"来源：{self.source_labels}",
        ]
        if self.speech_before:
            lines.append(f"前一句：{self.speech_before}")
        if self.speech_after:
            lines.append(f"后一句：{self.speech_after}")
        lines.append("支持证据：")
        lines.extend(f"  · {item}" for item in self.evidence) if self.evidence else lines.append("  · （无）")
        lines.append("风险：")
        lines.extend(f"  · {item}" for item in self.risks) if self.risks else lines.append("  · （未发现）")
        lines.append(f"审核状态：{self.review_status}")
        if len(self.merged_from) > 1:
            lines.append(
                f"合并自 {len(self.merged_from)} 个原始候选："
                + "、".join(format_timecode(t) for t in sorted(self.merged_from))
            )
        lines.append(
            "注：评分为同体系内的排序分，不是正确率概率；"
            "本版尚未接入剧情判断，动作与悬念落点均未评估。"
        )
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "time": _frac_json(self.time),
            "frame_index": self.frame_index,
            "sources": list(self.sources),
            "score": round(self.score, 4),
            "grade": self.grade,
            "evidence": list(self.evidence),
            "risks": list(self.risks),
            "speech_before": self.speech_before,
            "speech_after": self.speech_after,
            "scene_score": self.scene_score,
            "in_black": self.in_black,
            "gap_after": _frac_json(self.gap_after) if self.gap_after is not None else None,
            "review_status": self.review_status,
            "merged_from": [_frac_json(t) for t in self.merged_from],
        }

    @classmethod
    def from_json(cls, data: dict) -> "CandidatePoint":
        return cls(
            time=_parse_frac(data["time"]),
            frame_index=int(data["frame_index"]),
            sources=list(data.get("sources", [])),
            score=float(data.get("score", 0.0)),
            grade=data.get("grade", GRADE_REVIEW),
            evidence=list(data.get("evidence", [])),
            risks=list(data.get("risks", [])),
            speech_before=data.get("speech_before", ""),
            speech_after=data.get("speech_after", ""),
            scene_score=data.get("scene_score"),
            in_black=bool(data.get("in_black", False)),
            gap_after=(
                _parse_frac(data["gap_after"]) if data.get("gap_after") is not None else None
            ),
            review_status=data.get("review_status", "pending"),
            merged_from=[_parse_frac(t) for t in data.get("merged_from", [])],
        )


@dataclass
class CandidateSet:
    points: list[CandidatePoint] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    merge_distance: Fraction = Fraction(0)
    limitations: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.points)

    @property
    def is_empty(self) -> bool:
        return not self.points

    def top(self, count: int) -> list[CandidatePoint]:
        return sorted(self.points, key=lambda p: -p.score)[:count]

    def describe(self) -> str:
        if self.is_empty:
            return "候选点：无"
        parts = [f"{len(self.points)} 个候选点"]
        if self.source_counts:
            detail = "、".join(
                f"{SOURCE_LABELS.get(k, k)} {v}" for k, v in sorted(self.source_counts.items())
            )
            parts.append(f"（合并前来源：{detail}）")
        if self.merge_distance:
            parts.append(f"合并距离 {format_seconds_brief(self.merge_distance)}")
        return "".join(parts)

    def to_json(self) -> dict:
        return {
            "merge_distance": _frac_json(self.merge_distance),
            "source_counts": dict(self.source_counts),
            "limitations": list(self.limitations),
            "points": [p.to_json() for p in self.points],
        }

    @classmethod
    def from_json(cls, data: dict) -> "CandidateSet":
        return cls(
            points=[CandidatePoint.from_json(item) for item in data.get("points", [])],
            merge_distance=_parse_frac(data.get("merge_distance", 0)),
            limitations=list(data.get("limitations", [])),
            source_counts=dict(data.get("source_counts", {})),
        )


# ---------------------------------------------------------------------------
# 收集
# ---------------------------------------------------------------------------


def collect_candidates(
    transcript: Transcript | None,
    *,
    shots: list[SceneBoundary] | None = None,
    blacks: list[BlackInterval] | None = None,
    timeline_duration: Fraction,
    frame_index_at_or_after,
    snap_to_frame,
    gap_min_seconds: Fraction = Fraction(8, 100),
    gap_max_seconds: Fraction = Fraction(30),
    shot_tolerance_seconds: Fraction = Fraction(2),
) -> CandidateSet:
    """收集各类候选来源，**尚未合并**。

    设计上刻意不在这里做评分与限流：先把所有证据收齐、每条都记录来源，
    再统一合并与评分。这样"合并保留全部证据"才有可能（§8.2）。
    """
    candidates = CandidateSet()
    raw: list[tuple[Fraction, str, str, dict]] = []

    # 1. 对白句末（§8.2 的首要来源）
    if transcript is not None:
        sentences = transcript.sentences
        for index, sentence in enumerate(sentences):
            following = sentences[index + 1] if index + 1 < len(sentences) else None
            gap = (following.start - sentence.end) if following else None
            raw.append(
                (
                    sentence.end,
                    SOURCE_SENTENCE_END,
                    f"对白在此结束（{sentence.end_reason}）：{_short(sentence.text)}",
                    {
                        "speech_before": sentence.text,
                        "speech_after": following.text if following else "",
                        "gap_after": gap,
                    },
                )
            )
            # 2. 语音间隙中点：句末之后确有一段停顿，则停顿本身也是候选
            if gap is not None and gap_min_seconds <= gap <= gap_max_seconds:
                midpoint = sentence.end + gap / 2
                raw.append(
                    (
                        midpoint,
                        SOURCE_SPEECH_GAP,
                        f"句间停顿 {float(gap):.2f}s 的中点",
                        {
                            "speech_before": sentence.text,
                            "speech_after": following.text,
                            "gap_after": gap,
                        },
                    )
                )

    # 3. 镜头切换（§8.1 只是证据之一）
    for scene in shots or []:
        raw.append(
            (
                scene.time,
                SOURCE_SHOT_CUT,
                f"镜头切换（第 {scene.start_frame} 帧起，前一镜头 "
                f"{float(scene.duration):.2f}s）",
                {"scene_score": scene.score or None},
            )
        )

    # 4. 黑场 / 淡出
    for black in blacks or []:
        raw.append(
            (
                black.start + black.duration / 2,
                SOURCE_BLACK,
                f"黑场/淡出（{float(black.duration):.2f}s）",
                {"in_black": True},
            )
        )

    if not raw:
        candidates.issues.append(
            Issue(
                LEVEL_WARN,
                "CANDIDATES_EMPTY",
                "没有收集到任何候选点。",
                "可能缺少对白转写、镜头检测与黑场信息。"
                "没有候选点就无法进行整体规划（§10.4 的'候选图无路径'）。",
            )
        )
        return candidates

    # 落地为候选点，并吸附到真实帧边界
    for time, source, evidence, extra in raw:
        if timeline_duration > 0 and (time < 0 or time > timeline_duration):
            continue
        frame_index, snapped = snap_to_frame(time)
        point = CandidatePoint(
            time=snapped,
            frame_index=frame_index,
            sources=[source],
            evidence=[evidence],
            merged_from=[time],
            speech_before=extra.get("speech_before", ""),
            speech_after=extra.get("speech_after", ""),
            scene_score=extra.get("scene_score"),
            in_black=bool(extra.get("in_black", False)),
            gap_after=extra.get("gap_after"),
        )
        candidates.points.append(point)

    candidates.points.sort(key=lambda p: p.time)
    return candidates


# ---------------------------------------------------------------------------
# 合并（§8.2）
# ---------------------------------------------------------------------------


def merge_candidates(
    candidates: CandidateSet,
    *,
    distance_seconds: Fraction,
    snap_to_frame,
) -> CandidateSet:
    """合并相近候选点。

    两条硬要求（§8.2）：
    1. **保留全部证据**——合并后 sources 与 evidence 是所有成员的并集，
       并记录每个成员的原始时间，便于回溯"这个点是哪些证据凑出来的"。
    2. **代表点必须是真实帧边界**——取组内评分最高者所在的那个边界，
       **绝不取时间平均**。平均会让切点落进一句话中间，而这正是本工具
       要避免的事。
    """
    if not candidates.points:
        return candidates

    ordered = sorted(candidates.points, key=lambda p: p.time)
    groups: list[list[CandidatePoint]] = []
    for point in ordered:
        if groups and point.time - groups[-1][-1].time <= distance_seconds:
            groups[-1].append(point)
        else:
            groups.append([point])

    merged: list[CandidatePoint] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue

        # 代表点取"证据最强的那个原始候选"的时间，再吸附到真实帧边界。
        # 选择依据是来源优先级而不是时间平均：句末与黑场比镜头切换更可靠。
        representative = max(group, key=_representative_priority)
        frame_index, snapped = snap_to_frame(representative.time)

        sources: list[str] = []
        evidence: list[str] = []
        merged_from: list[Fraction] = []
        for member in group:
            for source in member.sources:
                if source not in sources:
                    sources.append(source)
            evidence.extend(member.evidence)
            merged_from.extend(member.merged_from)

        first, last = group[0], group[-1]
        point = CandidatePoint(
            time=snapped,
            frame_index=frame_index,
            sources=sources,
            evidence=evidence,
            merged_from=merged_from,
            speech_before=first.speech_before,
            speech_after=last.speech_after,
            scene_score=max((m.scene_score or 0.0) for m in group) or None,
            in_black=any(m.in_black for m in group),
        )
        point.gap_after = first.gap_after
        merged.append(point)

    result = CandidateSet(
        points=merged,
        issues=list(candidates.issues),
        merge_distance=distance_seconds,
        limitations=list(candidates.limitations),
    )
    _recount_sources(result, candidates)
    return result


def _representative_priority(point: CandidatePoint) -> tuple[int, int, float]:
    """代表点优先级：来源可靠性 > 合并成员数 > 评分。

    顺序很重要：**黑场与句末是"硬"证据**，而镜头切换在正反打里频繁出现、
    单独用它当切点很容易把一句话切成两半（§8.1）。因此即使某个镜头切换
    候选评分更高，也不应让它成为代表点。
    """
    source_rank = {
        SOURCE_BLACK: 5,
        SOURCE_SENTENCE_END: 4,
        SOURCE_MANUAL: 6,
        SOURCE_SPEECH_GAP: 3,
        SOURCE_SHOT_CUT: 1,
    }
    best = max((source_rank.get(s, 0) for s in point.sources), default=0)
    return (best, len(point.merged_from), point.score)


def _recount_sources(result: CandidateSet, before: CandidateSet) -> None:
    counts: dict[str, int] = {}
    for point in before.points:
        for source in point.sources:
            counts[source] = counts.get(source, 0) + 1
    result.source_counts = counts


# ---------------------------------------------------------------------------
# 规则评分（§11.2）
# ---------------------------------------------------------------------------


def score_candidates(
    candidates: CandidateSet,
    *,
    weights: RuleWeights | None = None,
    timeline_duration: Fraction | None = None,
) -> CandidateSet:
    """按规则评分并分级。

    评分是**同一体系内的排序分**，不是正确率概率（§11.3）。
    缺失的证据不会被当成高分——没有任何强证据的候选会被罚分。
    """
    weights = weights or RuleWeights()
    for point in candidates.points:
        _score_one(point, weights, timeline_duration)
    candidates.points.sort(key=lambda p: (-p.score, p.time))
    return candidates


def _score_one(
    point: CandidatePoint,
    weights: RuleWeights,
    timeline_duration: Fraction | None,
) -> None:
    score = 0.0
    evidence = list(point.evidence)
    risks = list(point.risks)

    if SOURCE_SENTENCE_END in point.sources or SOURCE_MANUAL in point.sources:
        score += weights.sentence_end
    if SOURCE_SPEECH_GAP in point.sources:
        score += weights.speech_gap
    gap = point.gap_after
    if gap is not None:
        ratio = min(1.0, float(gap) / weights.max_gap_for_full_credit)
        score += weights.gap_length * ratio
        evidence.append(f"句后停顿 {float(gap):.2f}s")
        if float(gap) < weights.too_close_seconds:
            score -= weights.penalty_next_speech_too_close
            risks.append(
                f"紧随下一句（间隔 {float(gap) * 1000:.0f}ms），有截断对白的风险"
            )
    if SOURCE_SHOT_CUT in point.sources:
        # 镜头切换只作佐证：给分但不作为主依据（§8.1）
        score += weights.shot_cut
        evidence.append("附近有镜头切换（仅作佐证，不能单独作为集尾）")
    # 黑场证据有两个载体：来源列表与 in_black 标记。任一处成立即算成立——
    # 只认其中一处会让"带黑场来源但标记为假"的候选被漏掉计分。
    has_black = SOURCE_BLACK in point.sources or point.in_black
    if has_black:
        score += weights.in_black
        evidence.append("位于黑场/淡出中，是该段落结束的强证据")

    if not (point.sources or has_black):
        score -= weights.penalty_no_evidence
        risks.append("无任何来源证据")

    if point.scene_score is not None:
        evidence.append(f"镜头切换检测置信度 {point.scene_score:.1f}")

    # 靠近片头片尾的位置不适合做切点
    if timeline_duration is not None and timeline_duration > 0:
        margin = Fraction(1)
        if point.time < margin or timeline_duration - point.time < margin:
            score -= weights.penalty_no_evidence
            risks.append("过于靠近片头或片尾，不适合作为集间切点")

    point.score = max(0.0, min(1.0, score))
    point.evidence = evidence
    point.risks = risks
    if point.score >= 0.75 and not risks:
        point.grade = GRADE_PRIORITY
    elif point.score >= 0.45:
        point.grade = GRADE_USABLE
    else:
        point.grade = GRADE_REVIEW


# ---------------------------------------------------------------------------
# 限流（§8.4）
# ---------------------------------------------------------------------------


def limit_candidates(
    candidates: CandidateSet,
    *,
    per_window: int = 12,
    window_seconds: Fraction = Fraction(60),
    budget: int | None = None,
) -> CandidateSet:
    """限制每个时间窗口的候选数量与总量（§8.4 控制分析规模）。

    **若因限流而丢弃了候选，必须在 limitations 里记录**——§8.4 明确要求
    "不能宣称已找到全部候选中的最优解"。

    保留策略：窗口内按评分取前 N；同时**保留低分候选**中位于窗口首尾的那一个，
    因为它们可能是维持可行路径所必需的（§8.4 要求保留这类候选）。
    """
    if candidates.is_empty:
        return candidates

    ordered = sorted(candidates.points, key=lambda p: p.time)
    kept: list[CandidatePoint] = []
    dropped = 0

    start_index = 0
    while start_index < len(ordered):
        window_start = ordered[start_index].time
        window_end = window_start + window_seconds
        window = []
        index = start_index
        while index < len(ordered) and ordered[index].time < window_end:
            window.append(ordered[index])
            index += 1

        if len(window) <= per_window:
            kept.extend(window)
        else:
            ranked = sorted(window, key=lambda p: -p.score)
            chosen = ranked[:per_window]
            # 窗口首尾各保留一个（哪怕分数低），避免整段失去可行边界
            for edge in (window[0], window[-1]):
                if edge not in chosen:
                    chosen.append(edge)
            dropped += len(window) - len(chosen)
            kept.extend(chosen)
        start_index = index

    kept.sort(key=lambda p: p.time)

    if budget is not None and len(kept) > budget:
        ranked = sorted(kept, key=lambda p: -p.score)[:budget]
        kept = sorted(ranked, key=lambda p: p.time)
        candidates.limitations.append(
            f"候选总数超出预算 {budget}，按评分截断（第 {budget} 名之后的候选未参与后续分析）"
        )

    if dropped:
        candidates.limitations.append(
            f"每个 {format_seconds_brief(window_seconds)} 窗口最多保留 {per_window} 个候选，"
            f"因限流丢弃 {dropped} 个低分候选"
        )

    result = CandidateSet(
        points=kept,
        issues=list(candidates.issues),
        merge_distance=candidates.merge_distance,
        limitations=list(candidates.limitations),
        source_counts=dict(candidates.source_counts),
    )
    if dropped:
        result.issues.append(
            Issue(
                LEVEL_INFO,
                "CANDIDATES_LIMITED",
                f"候选点已限流：丢弃 {dropped} 个。",
                "限流是为了控制分析规模。结果不声称是所有候选中的最优解（§8.4）。",
            )
        )
    return result


# ---------------------------------------------------------------------------
# 完整流水线
# ---------------------------------------------------------------------------


def build_candidate_set(
    *,
    transcript: Transcript | None,
    shots: list[SceneBoundary] | None,
    blacks: list[BlackInterval] | None,
    timeline_duration: Fraction,
    frame_index_at_or_after,
    snap_to_frame,
    merge_distance: Fraction = Fraction(1),
    per_window: int = 12,
    window_seconds: Fraction = Fraction(60),
    budget: int | None = None,
    weights: RuleWeights | None = None,
) -> CandidateSet:
    """收集 → 合并 → 评分 → 限流。阶段3 的对外主入口。"""
    collected = collect_candidates(
        transcript,
        shots=shots,
        blacks=blacks,
        timeline_duration=timeline_duration,
        frame_index_at_or_after=frame_index_at_or_after,
        snap_to_frame=snap_to_frame,
    )
    if collected.is_empty:
        return collected

    merged = merge_candidates(collected, distance_seconds=merge_distance, snap_to_frame=snap_to_frame)
    scored = score_candidates(merged, weights=weights, timeline_duration=timeline_duration)
    limited = limit_candidates(
        scored, per_window=per_window, window_seconds=window_seconds, budget=budget
    )
    return limited


def _short(text: str, limit: int = 24) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _frac_json(value: Fraction):
    if value.denominator == 1:
        return value.numerator
    return f"{value.numerator}/{value.denominator}"


def _parse_frac(value) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    return Fraction(str(value)).limit_denominator(1_000_000)
