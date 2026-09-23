"""边界方案模型与校验。

设计依据：
- §9.2 用边界数组 b0…bN 表示方案：b0=0、bN=T、严格递增；第 i 集拥有 [b(i−1), bi)。
  相邻两集共享同一个边界数据，不各自存一份可独立漂移的开始/结束时间；
  音频切点从同一组边界映射到采样索引，同一个共享边界只计算一次。
- §13.3 改动共享边界后必须同时更新相邻两集，并立即重检两侧时长、禁切区、锁定及集数。
- §20.1 上线前必须通过的客观检查。
- §4.4 单集例外默认关闭，启用后须记录原因和实际时长。

本模块不依赖 FFmpeg，也不依赖界面，可被独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Iterable

from .settings import CountPolicy, DerivedParams, SplitMode, SplitSettings
from .timebase import TimeBase, format_seconds_brief, format_timecode, round_half_up

__all__ = [
    "PlanProblem",
    "Episode",
    "EpisodeException",
    "BoundaryPlan",
    "PlanDiff",
    "PROBLEM_CODES",
]

# 问题码集中登记，避免各模块自行发明字符串
PROBLEM_CODES = {
    "TOO_FEW_BOUNDARIES": "边界数量不足",
    "NOT_START_AT_ZERO": "方案起点不是 0",
    "NOT_END_AT_TOTAL": "方案终点不是片尾",
    "NOT_STRICTLY_INCREASING": "边界未严格递增",
    "ZERO_LENGTH_EPISODE": "存在零时长集",
    "WRONG_EPISODE_COUNT": "集数与严格模式要求不符",
    "DURATION_OUT_OF_RANGE": "存在超出集长范围的集",
    "EXCEPTION_WITHOUT_REASON": "单集例外缺少原因记录",
    "LOCK_NOT_PRESERVED": "已锁定的切点未出现在新方案中",
    "FRAME_COUNT_MISMATCH": "各集帧数之和与源帧数不符",
    "FRAME_ALIGNMENT": "边界未落在合法帧起点上",
}


@dataclass(frozen=True)
class PlanProblem:
    code: str
    level: str  # info / warn / block
    message: str
    episode: int | None = None

    @property
    def is_blocking(self) -> bool:
        return self.level == "block"

    def describe(self) -> str:
        prefix = f"第{self.episode:02d}集：" if self.episode else ""
        return f"[{self.code}] {prefix}{self.message}"


@dataclass(frozen=True)
class EpisodeException:
    """§4.4 单集例外：只有用户主动启用后，指定集才可以超出默认范围。"""

    episode: int
    actual_seconds: Fraction
    reason: str
    approved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict:
        return {
            "episode": self.episode,
            "actual_seconds": _frac_json(self.actual_seconds),
            "reason": self.reason,
            "approved_at": self.approved_at,
        }


@dataclass(frozen=True)
class Episode:
    """一集。时长为派生展示值，不作为独立时间轴（§18.1）。"""

    index: int                 # 1 起
    start_boundary: int        # 边界数组下标
    end_boundary: int
    start_ticks: int
    end_ticks: int
    time_base: TimeBase

    @property
    def duration_seconds(self) -> Fraction:
        return self.time_base.ticks_to_seconds(self.end_ticks - self.start_ticks)

    def describe(self) -> str:
        return (
            f"第{self.index:02d}集｜{format_timecode(self.start_seconds)}—"
            f"{format_timecode(self.end_seconds)}｜{format_seconds_brief(self.duration_seconds)}"
        )

    @property
    def start_seconds(self) -> Fraction:
        return self.time_base.ticks_to_seconds(self.start_ticks)

    @property
    def end_seconds(self) -> Fraction:
        return self.time_base.ticks_to_seconds(self.end_ticks)


@dataclass
class PlanDiff:
    """两个方案版本的差异（§12.3 可查看差异并回退）。"""

    moved_boundaries: list[tuple[Fraction, Fraction]] = field(default_factory=list)
    episode_count_from: int = 0
    episode_count_to: int = 0
    affected_episodes: list[int] = field(default_factory=list)

    @property
    def is_identical(self) -> bool:
        return not self.moved_boundaries and self.episode_count_from == self.episode_count_to

    def summary(self) -> str:
        if self.is_identical:
            return "与上一版一致"
        parts = []
        if self.episode_count_from != self.episode_count_to:
            parts.append(f"集数 {self.episode_count_from} → {self.episode_count_to}")
        if self.moved_boundaries:
            parts.append(f"{len(self.moved_boundaries)} 个切点移动")
        if self.affected_episodes:
            parts.append(f"影响第 {', '.join(f'{e:02d}' for e in self.affected_episodes)} 集")
        return "；".join(parts)


@dataclass
class BoundaryPlan:
    """连续覆盖的分集方案：唯一事实来源是 boundary_ticks。"""

    boundary_ticks: list[int]
    time_base: TimeBase
    total_ticks: int
    version: int = 1
    source: str = "auto"  # auto / manual
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    strategy: str = "story"
    # 用户锁定的切点，按 ticks 值记录，重新规划时按值匹配（§12.3）
    locked_ticks: set[int] = field(default_factory=set)
    exceptions: dict[int, EpisodeException] = field(default_factory=dict)
    semantic_review_status: str = "pending"
    export_status: str = "not_started"

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_seconds(
        cls,
        boundaries: Iterable[Fraction | float],
        time_base: TimeBase,
        total_ticks: int,
        **kwargs,
    ) -> "BoundaryPlan":
        """从完整边界数组构造方案。

        边界必须**完整覆盖** [0, T]：首项为 0、末项为片尾。不满足时直接报错。

        这里刻意不做静默修正。早先的实现会把末项改写为片尾，结果是调用方
        少给一个边界时，方案会悄悄少掉一集并且顺利通过所有校验——
        把"我漏了一个边界"伪装成"方案就是这样"。属于 §10.4 明确反对的做法。
        需要"只给中间切点、其余自动覆盖"时请用 from_interior_cuts()。
        """
        ticks = [time_base.seconds_to_ticks(Fraction(b)) for b in boundaries]
        if len(ticks) < 2:
            raise ValueError("边界至少需要两个（片头与片尾）")
        if ticks[0] != 0:
            raise ValueError(f"首个边界必须为 0，实际为 {ticks[0]} ticks")
        if ticks[-1] != total_ticks:
            raise ValueError(
                f"末个边界必须等于片尾 {total_ticks} ticks，实际为 {ticks[-1]} ticks。"
                "若只想指定中间切点，请使用 from_interior_cuts()。"
            )
        return cls(boundary_ticks=ticks, time_base=time_base, total_ticks=total_ticks, **kwargs)

    @classmethod
    def from_interior_cuts(
        cls,
        cuts: Iterable[Fraction | float],
        time_base: TimeBase,
        total_ticks: int,
        **kwargs,
    ) -> "BoundaryPlan":
        """从**中间切点**构造方案，自动补上片头 0 与片尾 T。

        语义明确：调用方只负责中间切点，覆盖由本方法保证。
        """
        ticks = [0]
        ticks.extend(time_base.seconds_to_ticks(Fraction(cut)) for cut in cuts)
        ticks.append(total_ticks)
        if any(ticks[i] <= ticks[i - 1] for i in range(1, len(ticks))):
            raise ValueError(f"切点必须严格递增且落在 (0, 片尾) 之内：{ticks}")
        return cls(boundary_ticks=ticks, time_base=time_base, total_ticks=total_ticks, **kwargs)

    @classmethod
    def uniform(
        cls,
        episode_count: int,
        time_base: TimeBase,
        total_ticks: int,
        **kwargs,
    ) -> "BoundaryPlan":
        """等分方案。用于阶段1的手动分集闭环与可行性占位。"""
        if episode_count <= 0:
            raise ValueError("集数必须为正")
        ticks = [round_half_up(Fraction(total_ticks * i, episode_count)) for i in range(episode_count + 1)]
        ticks[0] = 0
        ticks[-1] = total_ticks
        return cls(boundary_ticks=ticks, time_base=time_base, total_ticks=total_ticks, **kwargs)

    # ------------------------------------------------------------------
    # 基本访问
    # ------------------------------------------------------------------

    @property
    def episode_count(self) -> int:
        return max(0, len(self.boundary_ticks) - 1)

    def episodes(self) -> list[Episode]:
        out: list[Episode] = []
        for i in range(self.episode_count):
            out.append(
                Episode(
                    index=i + 1,
                    start_boundary=i,
                    end_boundary=i + 1,
                    start_ticks=self.boundary_ticks[i],
                    end_ticks=self.boundary_ticks[i + 1],
                    time_base=self.time_base,
                )
            )
        return out

    def durations(self) -> list[Fraction]:
        return [ep.duration_seconds for ep in self.episodes()]

    def total_seconds(self) -> Fraction:
        return self.time_base.ticks_to_seconds(self.total_ticks)

    def audio_cut_samples(self, sample_rate: int) -> list[int]:
        """§9.2 音频切点：从同一组边界映射到采样索引，共享边界只算一次。"""
        if sample_rate <= 0:
            raise ValueError("采样率必须为正")
        return [
            round_half_up(self.time_base.ticks_to_seconds(t) * sample_rate)
            for t in self.boundary_ticks
        ]

    def frame_counts(self, timeline) -> list[int]:
        """每集帧数。timeline 为 probe.MediaInfo（提供 frame_index_at_or_after）。

        帧数是 §14.5 视频层校验的依据：固定帧率且不改变帧率的素材中，
        各集帧数之和应等于源帧数。使用 ceiling 语义的帧号映射后，该等式
        天然成立（望远镜求和），因此这个校验能真正抓到少帧/重复。
        """
        counts: list[int] = []
        for ep in self.episodes():
            start_index = timeline.frame_index_at_or_after(ep.start_seconds)
            end_index = timeline.frame_index_at_or_after(ep.end_seconds)
            counts.append(max(0, end_index - start_index))
        return counts

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(
        self,
        settings: SplitSettings | None = None,
        derived: DerivedParams | None = None,
    ) -> list[PlanProblem]:
        """§20.1 计划层校验：边界严格递增、恰好覆盖 [0,T)、集数正确、硬约束满足。"""
        problems: list[PlanProblem] = []

        if len(self.boundary_ticks) < 2:
            problems.append(
                PlanProblem("TOO_FEW_BOUNDARIES", "block", "方案至少需要两个边界才能构成一集。")
            )
            return problems

        if self.boundary_ticks[0] != 0:
            problems.append(
                PlanProblem(
                    "NOT_START_AT_ZERO",
                    "block",
                    f"起点应为 0，实际为 {self.boundary_ticks[0]} ticks。",
                )
            )

        if self.boundary_ticks[-1] != self.total_ticks:
            problems.append(
                PlanProblem(
                    "NOT_END_AT_TOTAL",
                    "block",
                    f"终点应为片尾 {self.total_ticks} ticks，"
                    f"实际为 {self.boundary_ticks[-1]} ticks，存在未覆盖的尾部素材。",
                )
            )

        for i in range(1, len(self.boundary_ticks)):
            previous, current = self.boundary_ticks[i - 1], self.boundary_ticks[i]
            if current <= previous:
                problems.append(
                    PlanProblem(
                        "NOT_STRICTLY_INCREASING",
                        "block",
                        f"边界未严格递增：{previous} → {current}。",
                        episode=i,
                    )
                )
            elif current == previous:
                problems.append(
                    PlanProblem("ZERO_LENGTH_EPISODE", "block", "存在零时长集。", episode=i)
                )

        # 严格模式集数
        if (
            settings is not None
            and settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
            and settings.count_policy == CountPolicy.EXACT
            and settings.target_episode_count is not None
        ):
            if self.episode_count != settings.target_episode_count:
                problems.append(
                    PlanProblem(
                        "WRONG_EPISODE_COUNT",
                        "block",
                        f"严格模式要求 {settings.target_episode_count} 集，"
                        f"当前方案为 {self.episode_count} 集。",
                    )
                )

        # 集长范围
        if derived is not None:
            for ep in self.episodes():
                duration = ep.duration_seconds
                exception = self.exceptions.get(ep.index)
                if duration <= 0:
                    problems.append(
                        PlanProblem("ZERO_LENGTH_EPISODE", "block", "存在零时长集。", episode=ep.index)
                    )
                    continue
                if duration < derived.min_duration or duration > derived.max_duration:
                    if exception is None:
                        problems.append(
                            PlanProblem(
                                "DURATION_OUT_OF_RANGE",
                                "block",
                                f"时长 {format_seconds_brief(duration)} 超出范围 "
                                f"{format_seconds_brief(derived.min_duration)}–"
                                f"{format_seconds_brief(derived.max_duration)}。",
                                episode=ep.index,
                            )
                        )
                    elif not exception.reason.strip():
                        problems.append(
                            PlanProblem(
                                "EXCEPTION_WITHOUT_REASON",
                                "warn",
                                "该集为单集例外，但未记录原因。",
                                episode=ep.index,
                            )
                        )
            # 例外中引用了不存在的集
            for index, exception in self.exceptions.items():
                if index < 1 or index > self.episode_count:
                    problems.append(
                        PlanProblem(
                            "EXCEPTION_WITHOUT_REASON",
                            "warn",
                            f"例外记录指向不存在的第 {index} 集。",
                            episode=index,
                        )
                    )
                del exception  # 保持显式：此处仅校验索引范围

        # 锁定切点是否保留（§12.3）
        present = set(self.boundary_ticks)
        missing_locks = sorted(self.locked_ticks - present)
        for tick in missing_locks:
            problems.append(
                PlanProblem(
                    "LOCK_NOT_PRESERVED",
                    "block",
                    f"已锁定的切点 {format_timecode(self.time_base.ticks_to_seconds(tick))} "
                    "未出现在当前方案中。",
                )
            )

        return problems

    @property
    def is_valid_plan(self) -> bool:
        return not any(p.is_blocking for p in self.validate())

    def coverage_valid(self) -> bool:
        """连续覆盖校验：b0=0、bN=T、严格递增（§9.2）。"""
        if len(self.boundary_ticks) < 2:
            return False
        if self.boundary_ticks[0] != 0 or self.boundary_ticks[-1] != self.total_ticks:
            return False
        return all(
            self.boundary_ticks[i] > self.boundary_ticks[i - 1]
            for i in range(1, len(self.boundary_ticks))
        )

    # ------------------------------------------------------------------
    # 帧对齐
    # ------------------------------------------------------------------

    def is_frame_aligned(self, timeline) -> tuple[bool, list[int]]:
        """检查所有边界是否落在帧起点上。

        这是**正确性前提**，不是可选优化：若某边界位于两帧之间，
        `-ss` 精确定位会丢弃到该时刻之前的全部帧，实际起点会顺延到下一帧，
        于是这一集与下一集重叠若干帧，帧数之和也不再守恒（§9.2、§14.1）。
        """
        if timeline.is_vfr:
            # VFR 的帧起点由实际 PTS 决定，交由 VFR 兼容性路径处理
            return True, []
        fps = timeline.video.nominal_fps if timeline.video else None
        if not fps:
            return True, []
        offenders: list[int] = []
        for index, tick in enumerate(self.boundary_ticks):
            product = self.time_base.ticks_to_seconds(tick) * fps
            if product.denominator != 1:
                offenders.append(index)
        return (not offenders), offenders

    def snap_to_frames(self, timeline) -> list["PlanProblem"]:
        """把所有边界吸附到最近的合法帧起点（§13.3）。

        吸附后强制保持严格递增：若吸附导致相邻边界重合，则向后推开一帧，
        再从尾部向前回收，避免末集被推过片尾。
        """
        fps = timeline.video.nominal_fps if timeline.video else None
        if timeline.is_vfr or not fps:
            return []

        total_frames = timeline.video.nb_frames
        if not total_frames:
            total_frames = timeline.frame_index_at_or_after(self.time_base.ticks_to_seconds(self.total_ticks))

        issues: list[PlanProblem] = []
        frames = [0]
        for index in range(1, len(self.boundary_ticks) - 1):
            seconds = self.time_base.ticks_to_seconds(self.boundary_ticks[index])
            frame_index, _ = timeline.snap_to_frame(seconds)
            frames.append(frame_index)
        frames.append(total_frames)

        interior = len(frames) - 2
        if interior > total_frames - 1:
            issues.append(
                PlanProblem(
                    "FRAME_ALIGNMENT",
                    "block",
                    f"边界数量（{len(self.boundary_ticks)}）超过可用帧数（{total_frames}），"
                    "无法保证每集至少一帧。",
                )
            )
            return issues

        # 正向：保证每集至少一帧
        for i in range(1, len(frames) - 1):
            if frames[i] <= frames[i - 1]:
                frames[i] = frames[i - 1] + 1
        # 反向：保证不越过片尾
        for i in range(len(frames) - 2, 0, -1):
            if frames[i] >= frames[i + 1]:
                frames[i] = frames[i + 1] - 1
        if frames[1] <= 0:
            issues.append(
                PlanProblem("FRAME_ALIGNMENT", "block", "吸附后无法容纳全部集数，请减少集数。")
            )
            return issues

        self.boundary_ticks = [
            self.time_base.seconds_to_ticks(timeline.frame_start_seconds(index)) for index in frames
        ]
        self.boundary_ticks[0] = 0
        self.boundary_ticks[-1] = self.total_ticks

        # 锁定值按新位置迁移
        if self.locked_ticks:
            present = set(self.boundary_ticks)
            self.locked_ticks = {tick for tick in self.locked_ticks if tick in present}

        return issues

    # ------------------------------------------------------------------
    # 编辑操作（界面层通过这里改方案，不直接碰 boundary_ticks）
    # ------------------------------------------------------------------

    def move_boundary(self, index: int, new_ticks: int) -> list[PlanProblem]:
        """移动一个共享边界，立即返回重检问题（§13.3）。

        相邻两集因共享同一边界而同步变化；越过相邻边界属于禁止提交项。
        """
        if index <= 0 or index >= len(self.boundary_ticks) - 1:
            return [
                PlanProblem(
                    "NOT_STRICTLY_INCREASING",
                    "block",
                    "首尾边界不可移动；它们是片头与片尾。",
                )
            ]

        lower = self.boundary_ticks[index - 1]
        upper = self.boundary_ticks[index + 1]
        problems: list[PlanProblem] = []
        if new_ticks <= lower or new_ticks >= upper:
            problems.append(
                PlanProblem(
                    "ZERO_LENGTH_EPISODE",
                    "block",
                    "该移动会使相邻集变为零时长或跨越相邻边界，已阻止提交。",
                    episode=index + 1,
                )
            )
            return problems

        old_ticks = self.boundary_ticks[index]
        self.boundary_ticks[index] = new_ticks

        # 锁定值随边界一起迁移
        if old_ticks in self.locked_ticks:
            self.locked_ticks.discard(old_ticks)
            self.locked_ticks.add(new_ticks)

        return self.validate()

    def lock_boundary(self, index: int, locked: bool = True) -> None:
        if 0 < index < len(self.boundary_ticks) - 1:
            tick = self.boundary_ticks[index]
            if locked:
                self.locked_ticks.add(tick)
            else:
                self.locked_ticks.discard(tick)

    def set_exception(self, episode: int, reason: str) -> None:
        """登记单集例外（§4.4）。只有用户主动启用后才应调用。"""
        if episode < 1 or episode > self.episode_count:
            raise ValueError(f"集号越界: {episode}")
        duration = self.boundary_ticks[episode] - self.boundary_ticks[episode - 1]
        self.exceptions[episode] = EpisodeException(
            episode=episode,
            actual_seconds=self.time_base.ticks_to_seconds(duration),
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 版本差异与序列化
    # ------------------------------------------------------------------

    def diff(self, other: "BoundaryPlan") -> PlanDiff:
        """与另一版本比较差异（§12.3）。other 视为旧版本。"""
        diff = PlanDiff(
            episode_count_from=other.episode_count,
            episode_count_to=self.episode_count,
        )
        common = min(len(other.boundary_ticks), len(self.boundary_ticks))
        affected: set[int] = set()
        for i in range(common):
            old_tick = other.boundary_ticks[i]
            new_tick = self.boundary_ticks[i]
            if old_tick != new_tick:
                diff.moved_boundaries.append(
                    (
                        other.time_base.ticks_to_seconds(old_tick),
                        self.time_base.ticks_to_seconds(new_tick),
                    )
                )
                affected.add(i)
                affected.add(i + 1)
        diff.affected_episodes = sorted(e for e in affected if 1 <= e <= self.episode_count)
        return diff

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "strategy": self.strategy,
            "created_at": self.created_at,
            "boundary_ticks": list(self.boundary_ticks),
            "episodes": [
                {
                    "episode": ep.index,
                    "start_boundary_index": ep.start_boundary,
                    "end_boundary_index": ep.end_boundary,
                    "start_ticks": ep.start_ticks,
                    "end_ticks": ep.end_ticks,
                    "duration_seconds": _frac_json(ep.duration_seconds),
                }
                for ep in self.episodes()
            ],
            "locked_ticks": sorted(self.locked_ticks),
            "exceptions": [e.to_json() for e in self.exceptions.values()],
            "coverage_valid": self.coverage_valid(),
            "constraints_valid": not any(p.is_blocking for p in self.validate()),
            "semantic_review_status": self.semantic_review_status,
            "export_status": self.export_status,
        }

    @classmethod
    def from_json(cls, data: dict, time_base: TimeBase, total_ticks: int) -> "BoundaryPlan":
        plan = cls(
            boundary_ticks=[int(t) for t in data["boundary_ticks"]],
            time_base=time_base,
            total_ticks=total_ticks,
            version=int(data.get("version", 1)),
            source=data.get("source", "auto"),
            strategy=data.get("strategy", "story"),
            semantic_review_status=data.get("semantic_review_status", "pending"),
            export_status=data.get("export_status", "not_started"),
        )
        plan.locked_ticks = {int(t) for t in data.get("locked_ticks", [])}
        for item in data.get("exceptions", []):
            plan.exceptions[int(item["episode"])] = EpisodeException(
                episode=int(item["episode"]),
                actual_seconds=_parse_frac(item["actual_seconds"]),
                reason=item.get("reason", ""),
                approved_at=item.get("approved_at", ""),
            )
        return plan

    def export_directory_name(self) -> str:
        """§18.2 输出目录按方案版本隔离。"""
        return f"plan_{self.version:04d}"


def _frac_json(value: Fraction):
    if value.denominator == 1:
        return value.numerator
    return f"{value.numerator}/{value.denominator}"


def _parse_frac(value) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"无法解析分数: {value!r}")
