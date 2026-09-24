"""字幕解析、时间合法性与偏移校正。

设计依据（§7.2 字幕输入规则）：
- 外挂 SRT/ASS/VTT 或可提取的文本字幕轨：解析后检查与原声的时间偏移。
- ASS 中的**样式信息不作为剧情文本**；第一版分析提取文本与时间，单集字幕输出以 SRT 为主。
- 画面内已经烧录的字幕**不能**按 SRT 直接读取（本模块不处理，需 OCR 或改用转写）。
- 扫描整份字幕的时间合法性，再抽查片头、中段、片尾是否同步，发现明显偏移先校正。
- 字幕不完整、方言、抢话、多人重叠、噪声等情况记录风险。
- 无音轨时跳过 ASR，启用画面分析和人工审核；不能把转写空结果当作故障反复重试。

时间一律用精确 Fraction 表示（§9.1），解析过程中不经过 float。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from .cache import CacheKey, CacheStore, SourceFingerprint
from .probe import LEVEL_INFO, LEVEL_WARN, Issue
from .timebase import format_timecode, round_half_up

__all__ = [
    "SubtitleCue",
    "SubtitleTrack",
    "SubtitleOffsetResult",
    "load_subtitle_file",
    "extract_text_subtitle_track",
    "validate_timing",
    "audio_rms_envelope",
    "estimate_offset",
    "apply_offset",
    "find_sidecar_subtitles",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 单条字幕的合理上限：超过它基本可判定为时间轴错误（如把毫秒当秒写）
MAX_PLAUSIBLE_CUE_SECONDS = Fraction(120)
# 偏移搜索上限（秒）。字幕整体错位通常来自帧率错配或时间轴起点差异，
# 几分钟的偏移已属极端；搜索范围越大越容易被无关事件配对带偏。
MAX_OFFSET_SEARCH_SECONDS = Fraction(300)

# 语音起始点检测的**系统性滞后**。
#
# 起始点靠"能量越过阈值"判定，而语音起音是渐强的，因此检出的时刻总比真实
# 起音晚一点。实测四种已知偏移（0 / +1.2 / −0.8 / +3.5 秒）下的残留偏差
# 稳定在 +0.22 秒——它是测量方法本身的偏置，不是随机噪声，也无法靠调阈值消除
# （调低阈值会开始把词内音节凹陷当成起始点）。
#
# 因此校正门槛必须**高于这个偏置**：否则一段本来对齐的字幕会被"校正"0.22 秒，
# 越改越偏。宁可不动。
ONSET_DETECTION_LAG_SECONDS = Fraction(22, 100)


@dataclass(frozen=True)
class SubtitleCue:
    """一条字幕。时间与源时间轴对齐。"""

    index: int
    start: Fraction
    end: Fraction
    text: str

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    def shifted(self, delta: Fraction, index: int | None = None) -> "SubtitleCue":
        return SubtitleCue(
            index=self.index if index is None else index,
            start=self.start + delta,
            end=self.end + delta,
            text=self.text,
        )

    def to_srt_block(self, index: int | None = None) -> str:
        number = self.index if index is None else index
        return (
            f"{number}\n"
            f"{_srt_time(self.start)} --> {_srt_time(self.end)}\n"
            f"{self.text}\n"
        )


@dataclass
class SubtitleTrack:
    """一份字幕轨的解析结果与质量记录。"""

    path: Path | None
    fmt: str
    cues: list[SubtitleCue] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    offset_seconds: Fraction = Fraction(0)
    offset_confidence: float = 0.0
    offset_applied: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.cues

    @property
    def spoken_seconds(self) -> Fraction:
        """字幕覆盖总时长。用于判断字幕是否明显不完整。"""
        total = Fraction(0)
        for cue in self.cues:
            total += max(Fraction(0), cue.duration)
        return total

    def active_at(self, seconds: Fraction) -> bool:
        return any(cue.start <= seconds < cue.end for cue in self.cues)

    def describe(self) -> str:
        if self.is_empty:
            return "字幕：无内容"
        return (
            f"字幕：{len(self.cues)} 条（{self.fmt}），"
            f"覆盖 {format_timecode(self.spoken_seconds)}"
        )

    def to_srt(self) -> str:
        blocks = [cue.to_srt_block(i + 1) for i, cue in enumerate(self.cues)]
        return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_ASS_TIME_RE = re.compile(
    r"Dialogue:\s*[^,]*,\s*(\d+):(\d{2}):(\d{2})[.](\d{1,2}),"
    r"\s*(\d+):(\d{2}):(\d{2})[.](\d{1,2}),"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")
# ASS 的内联覆盖标签，如 {\an8}{\pos(320,50)}，不是剧情文本
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def load_subtitle_file(path: str | Path) -> SubtitleTrack:
    """按扩展名解析字幕文件。

    分工：
    - **SRT / VTT 用内置解析器**。两者格式简单，内置解析器能精确处理
      "小数位是十进制小数"这一点；实测 pysubs2 会把 VTT 的 `00:00:01.50`
      解析成 1.05 秒（正确应为 1.5 秒），因此不在这些格式上使用它。
    - **ASS / SSA 用 pysubs2**。其样式与覆盖标签语法复杂，值得依赖成熟实现；
      我们只取时间与文本，样式信息一律丢弃（§7.2）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"字幕文件不存在: {path}")

    suffix = path.suffix.lower()
    if suffix == ".srt":
        return _parse_text_subtitle(path, "srt")
    if suffix in {".vtt", ".webvtt"}:
        return _parse_text_subtitle(path, "vtt")
    if suffix in {".ass", ".ssa"}:
        track = _load_ass_with_pysubs2(path)
        if track is None:
            track = SubtitleTrack(path=path, fmt="ass")
            track.issues.append(
                Issue(
                    LEVEL_WARN,
                    "ASS_PARSER_UNAVAILABLE",
                    "未安装 pysubs2，无法解析 ASS/SSA 字幕。",
                    "请安装 pysubs2，或先用工具把字幕转换为 SRT 再导入。",
                )
            )
        return track

    track = SubtitleTrack(path=path, fmt=suffix.lstrip(".") or "unknown")
    track.issues.append(
        Issue(
            LEVEL_WARN,
            "SUBTITLE_FORMAT_UNSUPPORTED",
            f"不支持的字幕扩展名：{path.suffix}",
            "本版支持 SRT / VTT / ASS / SSA。",
        )
    )
    return track


def _load_with_pysubs2(path: Path, fmt: str) -> SubtitleTrack | None:
    try:
        import pysubs2  # type: ignore
    except ImportError:
        return None

    try:
        parsed = pysubs2.load(str(path), encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001 - 第三方解析器异常类型不稳定
        track = SubtitleTrack(path=path, fmt=fmt)
        track.issues.append(
            Issue(LEVEL_WARN, "SUBTITLE_PARSE_FAILED", f"解析失败：{exc}", "将尝试内置解析器。")
        )
        return track

    cues: list[SubtitleCue] = []
    for order, event in enumerate(parsed):
        if getattr(event, "is_comment", False):
            continue
        text = _clean_text(event.plaintext or "")
        if not text:
            continue
        cues.append(
            SubtitleCue(
                index=order + 1,
                start=_ms_to_fraction(event.start),
                end=_ms_to_fraction(event.end),
                text=text,
            )
        )
    return SubtitleTrack(path=path, fmt=fmt, cues=cues)


def _load_ass_with_pysubs2(path: Path) -> SubtitleTrack | None:
    return _load_with_pysubs2(path, "ass")


def _parse_text_subtitle(path: Path, fmt: str) -> SubtitleTrack:
    """内置 SRT / VTT 解析器。

    刻意保持简单：只取时间与文本。**样式信息一律丢弃**（§7.2：ASS 中的样式
    信息不作为剧情文本），VTT 的内联标签同样剥掉。
    """
    track = SubtitleTrack(path=path, fmt=fmt)
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        track.issues.append(Issue(LEVEL_WARN, "SUBTITLE_READ_FAILED", f"读取失败：{exc}"))
        return track

    blocks = re.split(r"\r?\n\r?\n", raw)
    order = 0
    for block in blocks:
        match = _SRT_TIME_RE.search(block)
        if not match:
            continue
        start = _hms_frac(*match.group(1, 2, 3), _ms_fraction(match.group(4)))
        end = _hms_frac(*match.group(5, 6, 7), _ms_fraction(match.group(8)))
        body = block[match.end() :]
        text = _clean_text(body)
        if not text:
            continue
        order += 1
        track.cues.append(SubtitleCue(index=order, start=start, end=end, text=text))

    if not track.cues:
        track.issues.append(
            Issue(
                LEVEL_WARN,
                "SUBTITLE_EMPTY",
                "字幕文件里没有解析出任何有效字幕。",
                "请确认文件内容与编码（建议 UTF-8）。",
            )
        )
    return track


def _clean_text(text: str) -> str:
    """清洗字幕文本：剥样式标签、统一换行、去首尾空白。

    保留换行（多行字幕的断行位置对判断语气有意义），但把 \\N 之类
    的字幕内换行标记统一成真实换行。
    """
    cleaned = _ASS_OVERRIDE_RE.sub("", text)
    cleaned = _VTT_TAG_RE.sub("", cleaned)
    cleaned = cleaned.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    lines = [line.strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _ms_fraction(digits: str) -> Fraction:
    """字幕的毫秒/厘秒字段 → Fraction。

    SRT 用 3 位（毫秒），VTT 可能用 2 位（厘秒）。按位数判断，不猜。
    """
    value = int(digits)
    if len(digits) == 1:
        return Fraction(value, 10)
    if len(digits) == 2:
        return Fraction(value, 100)
    return Fraction(value, 1000)


def _hms_frac(hours: str, minutes: str, seconds: str, sub: Fraction) -> Fraction:
    return Fraction(int(hours) * 3600 + int(minutes) * 60 + int(seconds)) + sub


def _ms_to_fraction(milliseconds: int) -> Fraction:
    return Fraction(int(milliseconds), 1000)


def _srt_time(value: Fraction) -> str:
    total_ms = round_half_up(value * 1000)
    sign = "-" if total_ms < 0 else ""
    total_ms = abs(total_ms)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


# ---------------------------------------------------------------------------
# 从视频中提取文本字幕轨
# ---------------------------------------------------------------------------


def extract_text_subtitle_track(
    ffmpeg: str | Path,
    video: str | Path,
    target: str | Path,
    *,
    stream_index: int = 0,
) -> tuple[Path | None, Issue | None]:
    """把容器内的文本字幕轨导出为 SRT。

    返回 (产物路径, 问题)。失败时产物为 None 并给出可读原因——
    注意**烧录进画面的字幕不在此列**（§7.2），ffprobe 根本看不到它，
    这种情况只能靠 OCR 或语音转写。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-map",
        f"0:s:{stream_index}",
        "-c:s",
        "srt",
        "-y",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        message = (proc.stderr or "").strip().splitlines()
        return None, Issue(
            LEVEL_WARN,
            "SUBTITLE_TRACK_EXTRACT_FAILED",
            f"字幕轨 {stream_index} 提取失败。",
            message[-1] if message else "该文件可能不含文本字幕轨（烧录字幕属于画面，无法这样读取）。",
        )
    return target, None


# ---------------------------------------------------------------------------
# 时间合法性
# ---------------------------------------------------------------------------


def validate_timing(
    cues: Iterable[SubtitleCue],
    *,
    timeline_duration: Fraction | None = None,
) -> list[Issue]:
    """扫描整份字幕的时间合法性（§7.2）。

    这不是可选的美化检查：时间轴错了会导致后续所有"对白句末"候选点全部偏移，
    而且现象是"AI 判断总是差一点"，极难回溯到字幕本身。
    """
    cues = list(cues)
    issues: list[Issue] = []
    if not cues:
        return issues

    negative = [c for c in cues if c.start < 0 or c.end < 0]
    if negative:
        issues.append(
            Issue(
                LEVEL_WARN,
                "SUBTITLE_NEGATIVE_TIME",
                f"{len(negative)} 条字幕的时间为负。",
                f"首条出现在 {format_timecode(negative[0].start)}，建议先校正时间轴。",
            )
        )

    zero = [c for c in cues if c.duration <= 0]
    if zero:
        issues.append(
            Issue(
                LEVEL_WARN,
                "SUBTITLE_ZERO_DURATION",
                f"{len(zero)} 条字幕时长为零或为负。",
                f"首条为第 {zero[0].index} 条（{format_timecode(zero[0].start)}）。",
            )
        )

    too_long = [c for c in cues if c.duration > MAX_PLAUSIBLE_CUE_SECONDS]
    if too_long:
        issues.append(
            Issue(
                LEVEL_WARN,
                "SUBTITLE_ABSURD_DURATION",
                f"{len(too_long)} 条字幕单条时长超过 {MAX_PLAUSIBLE_CUE_SECONDS} 秒。",
                "常见原因是把毫秒当秒写入（如 00:00:05,000 写成 00:05:00,000）。",
            )
        )

    unordered = [
        i for i in range(1, len(cues)) if cues[i].start < cues[i - 1].start
    ]
    if unordered:
        issues.append(
            Issue(
                LEVEL_WARN,
                "SUBTITLE_NOT_MONOTONIC",
                f"字幕未按开始时间递增（{len(unordered)} 处逆序）。",
                "本模块会按原顺序保留；如需按时间排序请显式启用。",
            )
        )

    overlaps = [
        (cues[i - 1], cues[i])
        for i in range(1, len(cues))
        if cues[i].start < cues[i - 1].end and cues[i].end > cues[i].start
    ]
    if overlaps:
        worst = max(overlap_amount(a, b) for a, b in overlaps)
        issues.append(
            Issue(
                LEVEL_INFO,
                "SUBTITLE_OVERLAP",
                f"{len(overlaps)} 处字幕时间重叠。",
                f"最大重叠 {float(worst) * 1000:.0f}ms。"
                "抢话与多人重叠是常见成因，属于需要人工留意的风险（§7.2）。",
            )
        )

    if timeline_duration is not None:
        beyond = [c for c in cues if c.start >= timeline_duration]
        if beyond:
            issues.append(
                Issue(
                    LEVEL_WARN,
                    "SUBTITLE_BEYOND_VIDEO",
                    f"{len(beyond)} 条字幕落在片尾之后。",
                    f"片长 {format_timecode(timeline_duration)}，"
                    f"最晚一条在 {format_timecode(beyond[-1].start)}——通常说明字幕属于另一个版本。",
                )
            )

        covered = sum(max(Fraction(0), c.duration) for c in cues)
        if timeline_duration > 0:
            ratio = covered / timeline_duration
            if ratio < Fraction(1, 20):
                issues.append(
                    Issue(
                        LEVEL_WARN,
                        "SUBTITLE_SPARSE",
                        f"字幕只覆盖片长的 {float(ratio) * 100:.1f}%。",
                        "字幕可能不完整（只有部分段落），不建议作为唯一对白来源。",
                    )
                )

    return issues


def overlap_amount(first: SubtitleCue, second: SubtitleCue) -> Fraction:
    return max(Fraction(0), min(first.end, second.end) - max(first.start, second.start))


# ---------------------------------------------------------------------------
# 音频活动包络（用于偏移检测）
# ---------------------------------------------------------------------------


def audio_rms_envelope(*args, **kwargs) -> list[float]:
    """按固定步长计算音频 RMS 包络（实现见 `audio.py`）。

    这里保留同名转发是为了让调用方的语义清晰（字幕对齐用包络），
    真正的实现与缓存逻辑集中在 `audio` 模块，避免两处各写一份。
    """
    from .audio import audio_rms_envelope as impl

    return impl(*args, **kwargs)


# ---------------------------------------------------------------------------
# 偏移估计
# ---------------------------------------------------------------------------


@dataclass
class SubtitleOffsetResult:
    offset_seconds: Fraction
    confidence: float
    method: str
    detail: str = ""

    @property
    def is_significant(self) -> bool:
        """是否值得校正。

        只有"偏移明显"且"判定可信"时才动时间轴；否则宁可不动，
        避免把本来正确的字幕推歪（§7.2 要求"发现明显偏移先校正"，
        隐含前提是确实发现了）。

        门槛必须高于起始点检测的系统性滞后（见 ONSET_DETECTION_LAG_SECONDS），
        否则会把检测偏置当成真实错位去"校正"，反而把对齐的字幕改坏。
        """
        threshold = float(ONSET_DETECTION_LAG_SECONDS) + 0.1
        return abs(float(self.offset_seconds)) >= threshold and self.confidence >= 0.35


def estimate_offset(
    cues: list[SubtitleCue],
    envelope: list[float],
    *,
    hop_seconds: Fraction = Fraction(1, 20),
    max_offset_seconds: Fraction = MAX_OFFSET_SEARCH_SECONDS,
) -> SubtitleOffsetResult:
    """估计字幕相对原声的整体偏移。

    方法：**事件对齐投票**，不是稠密序列互相关。

    为什么不用互相关
    ----------------
    实测：短剧对白下字幕覆盖约 85% 的时间轴，"字幕是否有字"与"音频是否有声"
    两条稠密序列高度重合，互相关的峰值只有 0.24，且相邻数格内几乎完全平坦
    （0.243 vs 0.241），几乎没有定位能力，偶尔"蒙对"也差好几格。
    更早一版还把重叠下限设得太松，在极小重叠处刷出 0.984 的**假峰**，
    给出 40.9s 素材偏移 39.85s 的荒谬结果——而置信度看起来还挺高。

    改用事件对齐：语音起始点与字幕起始点都是锐利事件。把每一对
    「字幕起点 − 语音起始点」当作对某个偏移的一次投票，票数最高的偏移即所求。
    峰值清晰、可解释，且每个字幕起点是否命中都可以逐条核对。
    """
    import numpy as np

    if not cues or not envelope:
        return SubtitleOffsetResult(Fraction(0), 0.0, "none", "缺少字幕或音频包络")

    from .audio import speech_onsets

    onsets = speech_onsets(envelope, hop_seconds=hop_seconds)
    if len(onsets) < 3:
        return SubtitleOffsetResult(
            Fraction(0), 0.0, "none", f"检出语音起始点仅 {len(onsets)} 个，不足以对齐"
        )

    hop = float(hop_seconds)
    cue_starts = [float(cue.start) for cue in cues]
    onset_values = np.asarray([float(o) for o in onsets], dtype=np.float64)

    # 投票：每个字幕起点与每个语音起始点的差值都是对一个偏移的投票。
    #
    # 用「桶 → 字幕下标集合」而不是「桶 → 列表」：同一条字幕可能因为语音起始点
    # 落在桶边界两侧而在相邻两格各投一票，用列表累加会出现"命中数超过字幕条数"
    # （实测 10 条字幕报出 12 条命中），命中率指标随之失效。
    limit = float(max_offset_seconds)
    tally: dict[int, set[int]] = {}
    for cue_index, start in enumerate(cue_starts):
        deltas = onset_values - start
        deltas = deltas[(deltas >= -limit) & (deltas <= limit)]
        for delta in deltas:
            tally.setdefault(int(round(delta / hop)), set()).add(cue_index)

    if not tally:
        return SubtitleOffsetResult(Fraction(0), 0.0, "none", "没有任何配对落在搜索范围内")

    # 合并相邻桶（同一峰可能跨两格），按命中的字幕集合去重后取票数最高者
    merged: list[tuple[int, set[int]]] = []
    for bucket in sorted(tally):
        if merged and bucket - merged[-1][0] <= 1:
            merged[-1] = (bucket, merged[-1][1] | tally[bucket])
        else:
            merged.append((bucket, set(tally[bucket])))

    best_bucket, best_hits = max(merged, key=lambda item: len(item[1]))
    runner_up = max((len(hits) for bucket, hits in merged if abs(bucket - best_bucket) > 2), default=0)

    matched = len(best_hits)
    match_ratio = matched / len(cue_starts)  # 结构性 ≤ 1
    separation = (matched - runner_up) / matched if matched else 0.0

    # 偏移取命中该桶的所有配对差值的**中位数**，避免被个别离群配对拉偏
    deltas_in_bucket: list[float] = []
    for start in (cue_starts[i] for i in best_hits):
        candidates = onset_values - start
        candidates = candidates[np.abs(candidates - best_bucket * hop) <= hop]
        deltas_in_bucket.extend(float(v) for v in candidates)
    offset_value = float(np.median(deltas_in_bucket)) if deltas_in_bucket else best_bucket * hop
    offset = Fraction(str(round(offset_value, 4))).limit_denominator(100_000)

    # 置信度 = 命中比例与峰值分离度的综合。命中比例是主项：
    # 只有少数几条字幕能找到对应语音时，对齐并不成立。
    confidence = max(0.0, min(1.0, 0.7 * match_ratio + 0.3 * separation))

    detail = (
        f"{len(cue_starts)} 条字幕中 {matched} 条命中语音起始点"
        f"（{match_ratio:.0%}），次优峰 {runner_up} 条，分离度 {separation:.2f}；"
        f"检出语音起始点 {len(onsets)} 个"
    )
    return SubtitleOffsetResult(offset, confidence, "onset_voting", detail)


def apply_offset(track: SubtitleTrack, result: SubtitleOffsetResult) -> SubtitleTrack:
    """按估计结果校正字幕时间轴，并如实记录做了什么。"""
    if not result.is_significant:
        track.offset_confidence = result.confidence
        track.issues.append(
            Issue(
                LEVEL_INFO,
                "SUBTITLE_OFFSET_NOT_APPLIED",
                "未对字幕时间轴做整体校正。",
                f"估计偏移 {float(result.offset_seconds) * 1000:+.0f}ms，"
                f"置信度 {result.confidence:.2f}，未达到自动校正门槛。{result.detail}",
            )
        )
        return track

    track.cues = [cue.shifted(result.offset_seconds) for cue in track.cues]
    track.offset_seconds = result.offset_seconds
    track.offset_confidence = result.confidence
    track.offset_applied = True
    track.issues.append(
        Issue(
            LEVEL_WARN,
            "SUBTITLE_OFFSET_APPLIED",
            f"已对字幕整体校正 {float(result.offset_seconds) * 1000:+.0f}ms。",
            f"置信度 {result.confidence:.2f}。{result.detail}",
        )
    )
    return track


def find_sidecar_subtitles(video: str | Path) -> list[Path]:
    """查找与视频同名的外挂字幕（§7.2 输入分类的第一类）。"""
    video = Path(video)
    found: list[Path] = []
    for suffix in (".srt", ".vtt", ".ass", ".ssa"):
        for candidate in (
            video.with_suffix(suffix),
            video.with_name(video.stem + ".zh" + suffix),
            video.with_name(video.stem + ".chs" + suffix),
            video.with_name(video.stem + ".chi" + suffix),
        ):
            if candidate.exists():
                found.append(candidate)
    return found
