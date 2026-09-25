"""单集字幕导出（§7.2：单集字幕输出以 SRT 为主）。

归属规则
--------
一条字幕属于"其**起点**所在的那一集"——与视频帧的归属规则一致
（帧属于包含它的那一集）。这样字幕与画面的内容归属就不会错位。

时间换算
--------
导出的 SRT 时间是**相对该集开头**的（观众从 0 开始看），因此
`srt_start = 原始起点 − 集起点`。计算全程用精确 Fraction，最后写文件时
才按 §4.1 四舍五入到毫秒。

跨集句子的处理
--------------
句尾跨过集尾的句子（即整集复核里"字幕悬挂"风险的那种）按起点归属到前
一集，但其**结束时间会被截到集尾**——否则下一集开头会凭空出现半句话。
截断会被如实记录在返回值里，不静默。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .asr import Transcript
from .plan import BoundaryPlan
from .timebase import format_timecode, round_half_up

__all__ = ["EpisodeSubtitleResult", "export_episode_subtitles", "_srt_timestamp"]


@dataclass
class EpisodeSubtitleResult:
    """一集的字幕导出结果。"""

    episode: int
    path: Path | None
    sentence_count: int = 0
    clipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.path is not None


def _srt_timestamp(seconds: Fraction) -> str:
    """SRT 时间戳 HH:MM:SS,mmm。负值按 0 处理并保留符号信息由调用方决策。"""
    total_ms = round_half_up(seconds * 1000)
    if total_ms < 0:
        total_ms = 0
    hours, remainder = divmod(int(total_ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_part, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def export_episode_subtitles(
    plan: BoundaryPlan,
    transcript: Transcript | None,
    output_dir: Path,
    *,
    filename_pattern: str = "第{:02d}集.srt",
) -> list[EpisodeSubtitleResult]:
    """为方案里的每一集导出 SRT 字幕。

    返回与集数等长的结果列表。转写缺失时返回的每条结果都带说明（不产出文件），
    让调用方能如实呈现"为什么没有字幕"，而不是静默跳过。
    """
    output_dir = Path(output_dir)
    results: list[EpisodeSubtitleResult] = []

    if transcript is None or not transcript.sentences:
        for episode in plan.episodes():
            results.append(
                EpisodeSubtitleResult(
                    episode=episode.index,
                    path=None,
                    notes=["素材无对白或转写被跳过，未产出字幕"],
                )
            )
        return results

    boundaries = [
        plan.time_base.ticks_to_seconds(tick) for tick in plan.boundary_ticks
    ]

    for episode in plan.episodes():
        start = boundaries[episode.index - 1]
        end = boundaries[episode.index]
        lines: list[str] = []
        clipped: list[str] = []
        order = 0

        for sentence in transcript.sentences:
            # 按起点归属：句子起点落在 [start, end) 即属于本集
            if not (start <= sentence.start < end):
                continue
            subtitle_start = sentence.start - start
            subtitle_end = min(sentence.end, end) - start  # 跨集句子截到集尾
            if sentence.end > end:
                clipped.append(
                    f"「{sentence.text[:16]}…」句尾越过集尾"
                    f"（{format_timecode(sentence.end)} > {format_timecode(end)}），已截断"
                )
            if subtitle_end <= subtitle_start:
                continue

            order += 1
            lines.append(
                f"{order}\n"
                f"{_srt_timestamp(subtitle_start)} --> {_srt_timestamp(subtitle_end)}\n"
                f"{sentence.text}\n"
            )

        result = EpisodeSubtitleResult(
            episode=episode.index, path=None, sentence_count=order, clipped=clipped
        )
        if order == 0:
            result.notes.append("该集内没有对白，未产出字幕文件")
        else:
            target = output_dir / filename_pattern.format(episode.index)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines), encoding="utf-8")
            result.path = target
        results.append(result)

    return results
