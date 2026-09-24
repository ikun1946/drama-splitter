"""镜头切换与黑场检测。

设计依据（§8.1 镜头切换不等于剧情结束）：
- PySceneDetect 依据像素、颜色等视觉变化发现切换。正反打、快速运动或闪光
  都可能形成候选，但**同一段对话可能跨越多个镜头**。
- 因此镜头边界只是证据之一，**不可直接批量当作集尾**。

另据 §8.2，黑场/淡出也是候选来源之一。

本模块只负责"看见画面变化"，不负责判断剧情——判定留给候选合并与人工审核。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .cache import CacheKey, CacheStore, SourceFingerprint
from .probe import LEVEL_INFO, LEVEL_WARN, Issue
from .timebase import format_timecode

__all__ = [
    "SceneBoundary",
    "BlackInterval",
    "ShotDetectionResult",
    "detect_scenes",
    "detect_black_intervals",
    "detect_all",
    "DEFAULT_CONTENT_THRESHOLD",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# PySceneDetect 的 ContentDetector 默认阈值是 27.0。数值越低越敏感（误检更多）。
DEFAULT_CONTENT_THRESHOLD = 27.0

# 最短镜头长度（帧）。低于此长度的"切换"在短剧里多为闪光或快速运动，
# 属于误检；设下限比事后过滤更省事。
DEFAULT_MIN_SCENE_LEN_FRAMES = 12


@dataclass(frozen=True)
class SceneBoundary:
    """一次镜头切换。"""

    time: Fraction
    end_time: Fraction
    start_frame: int
    score: float = 0.0

    @property
    def duration(self) -> Fraction:
        return self.end_time - self.time

    def to_json(self) -> dict:
        return {
            "time": _frac_json(self.time),
            "end_time": _frac_json(self.end_time),
            "start_frame": self.start_frame,
            "score": round(self.score, 3),
        }

    @classmethod
    def from_json(cls, data: dict) -> "SceneBoundary":
        return cls(
            time=_parse_frac(data["time"]),
            end_time=_parse_frac(data["end_time"]),
            start_frame=int(data["start_frame"]),
            score=float(data.get("score", 0.0)),
        )


@dataclass(frozen=True)
class BlackInterval:
    """一段黑场或淡出。"""

    start: Fraction
    end: Fraction
    duration: Fraction
    is_fade: bool = True

    def to_json(self) -> dict:
        return {
            "start": _frac_json(self.start),
            "end": _frac_json(self.end),
            "duration": _frac_json(self.duration),
            "is_fade": self.is_fade,
        }

    @classmethod
    def from_json(cls, data: dict) -> "BlackInterval":
        return cls(
            start=_parse_frac(data["start"]),
            end=_parse_frac(data["end"]),
            duration=_parse_frac(data["duration"]),
            is_fade=bool(data.get("is_fade", True)),
        )


@dataclass
class ShotDetectionResult:
    scenes: list[SceneBoundary] = field(default_factory=list)
    blacks: list[BlackInterval] = field(default_factory=list)
    threshold: float = DEFAULT_CONTENT_THRESHOLD
    detector: str = "content"
    elapsed_seconds: float = 0.0
    issues: list[Issue] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.scenes and not self.blacks

    def describe(self) -> str:
        return (
            f"镜头：{len(self.scenes)} 次切换；"
            f"黑场/淡出：{len(self.blacks)} 段（检测器 {self.detector}，阈值 {self.threshold}）"
        )

    def to_json(self) -> dict:
        return {
            "threshold": self.threshold,
            "detector": self.detector,
            "scenes": [s.to_json() for s in self.scenes],
            "blacks": [b.to_json() for b in self.blacks],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ShotDetectionResult":
        return cls(
            scenes=[SceneBoundary.from_json(item) for item in data.get("scenes", [])],
            blacks=[BlackInterval.from_json(item) for item in data.get("blacks", [])],
            threshold=float(data.get("threshold", DEFAULT_CONTENT_THRESHOLD)),
            detector=data.get("detector", "content"),
        )


# ---------------------------------------------------------------------------
# 镜头切换
# ---------------------------------------------------------------------------


def detect_scenes(
    video: str | Path,
    *,
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_scene_len_frames: int = DEFAULT_MIN_SCENE_LEN_FRAMES,
    frame_rate: float | None = None,
    on_progress=None,
) -> tuple[list[SceneBoundary], list[Issue]]:
    """用 PySceneDetect 检测镜头切换。

    返回 (切换列表, 问题列表)。PySceneDetect 未安装时返回空列表并说明原因——
    镜头检测是**可选增强**，缺失不应让整条链路失败。
    """
    issues: list[Issue] = []
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError:
        issues.append(
            Issue(
                LEVEL_WARN,
                "SCENEDETECT_UNAVAILABLE",
                "未安装 PySceneDetect，跳过镜头检测。",
                "安装后可使用镜头切换作为候选来源；缺失时仅凭对白与静音也能分集。",
            )
        )
        return [], issues

    video_path = str(video)
    try:
        source = open_video(video_path)
    except Exception as exc:  # noqa: BLE001 - 解码后端异常类型不稳定
        issues.append(
            Issue(LEVEL_WARN, "SCENE_OPEN_FAILED", f"打开视频失败：{exc}", "已跳过镜头检测。")
        )
        return [], issues

    manager = SceneManager()
    manager.add_detector(
        ContentDetector(threshold=threshold, min_scene_len=min_scene_len_frames)
    )

    try:
        manager.detect_scenes(source, show_progress=False)
        scene_list = manager.get_scene_list()
    except Exception as exc:  # noqa: BLE001
        issues.append(
            Issue(LEVEL_WARN, "SCENE_DETECT_FAILED", f"镜头检测失败：{exc}", "已跳过。")
        )
        return [], issues

    fps = frame_rate or float(source.frame_rate or 25.0)
    boundaries: list[SceneBoundary] = []
    for start, end in scene_list:
        boundaries.append(
            SceneBoundary(
                time=_scene_time(start),
                end_time=_scene_time(end),
                start_frame=_scene_frame(start),
                score=_scene_score(start),
            )
        )

    if len(boundaries) > 200 and len(boundaries) > 0:
        # 短剧里镜头极多属正常，但若远超片长可能意味着阈值过低导致误检
        span = boundaries[-1].end_time if boundaries else Fraction(0)
        if span > 0 and len(boundaries) / max(1.0, float(span)) > 3.0:
            issues.append(
                Issue(
                    LEVEL_INFO,
                    "SCENE_OVERSENSITIVE",
                    f"平均每秒检出超过 3 次切换（共 {len(boundaries)} 次）。",
                    "可能是阈值偏低把快速运动或闪光当成切换。这些只是候选证据，"
                    "不会直接成为集尾（§8.1），但会增大候选量。",
                )
            )
    _ = fps  # 保留以便将来按帧对齐
    return boundaries, issues


def _scene_time(frame_time) -> Fraction:
    """从 PySceneDetect 的 FrameTimecode 取精确秒数。

    0.7 起 `get_seconds()` 已废弃、推荐 `seconds` 属性；两者在不同小版本上
    可能只存在其一，因此都试一遍，而不是绑死某一个。
    """
    value = getattr(frame_time, "seconds", None)
    if value is None:
        value = frame_time.get_seconds()  # type: ignore[union-attr]
    return Fraction(str(round(float(value), 4))).limit_denominator(100_000)


def _scene_frame(frame_time) -> int:
    """取帧号。0.7 起 `get_frames()` 已废弃、推荐 `frame_num`。"""
    value = getattr(frame_time, "frame_num", None)
    if value is None:
        value = frame_time.get_frames()  # type: ignore[union-attr]
    return int(value)


def _scene_score(frame_time) -> float:
    return float(getattr(frame_time, "score", 0.0) or 0.0)


def _detection_completed(result: ShotDetectionResult) -> bool:
    """判断检测是"跑完但没找到"还是"根本没跑起来"。

    这个区分决定能否写缓存。早先的实现用"结果为空就不缓存"，等于把
    **跑成功但确实没有镜头**的素材也排除在缓存之外，每次都白跑一遍慢检测。
    正确的判据是**后端是否可用**，而不是结果是否为空。
    """
    unavailable_codes = {
        "SCENEDETECT_UNAVAILABLE",
        "SCENE_OPEN_FAILED",
        "SCENE_DETECT_FAILED",
        "BLACKDETECT_FAILED",
    }
    return not any(issue.code in unavailable_codes for issue in result.issues)


# ---------------------------------------------------------------------------
# 黑场 / 淡出
# ---------------------------------------------------------------------------

_BLACKDETECT_RE = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<dur>[\d.]+)"
)


def detect_black_intervals(
    ffmpeg: str | Path,
    video: str | Path,
    *,
    min_duration: float = 0.3,
    picture_threshold: float = 0.98,
    pixel_threshold: float = 0.10,
) -> tuple[list[BlackInterval], list[Issue]]:
    """用 FFmpeg `blackdetect` 检测黑场与淡出。

    选 FFmpeg 而不是 PySceneDetect 的 ThresholdDetector：黑场判定是明确的
    像素阈值问题，`blackdetect` 直接给出起止时间，实现更可靠也更省依赖。

    两个阈值的语义**极易搞反**，这里明确记下：
    - `pic_th`（percentage of black pixels）：一帧中至少这么高**比例**的像素
      是黑的，才判定该帧为黑场。默认 0.98。
    - `pix_th`（pixel threshold）：单个像素的 RGB 均值低于此值才算**黑像素**。
      默认 0.10。

    踩过的坑：把 `pix_th` 写成 0.98 意味着"几乎所有像素都算黑"，
    于是整片被报成一段从头到尾的黑场（实测 119.96 秒的素材报出
    `black_start:0 black_end:119.96`）。
    """
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-i",
        str(video),
        "-vf",
        f"blackdetect=d={min_duration}:pic_th={picture_threshold}:pix_th={pixel_threshold}",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [
            Issue(LEVEL_WARN, "BLACKDETECT_FAILED", f"黑场检测未执行：{exc}", "已跳过。")
        ]

    intervals: list[BlackInterval] = []
    for match in _BLACKDETECT_RE.finditer(proc.stderr or ""):
        start = Fraction(str(round(float(match.group("start")), 4))).limit_denominator(100_000)
        end = Fraction(str(round(float(match.group("end")), 4))).limit_denominator(100_000)
        intervals.append(
            BlackInterval(start=start, end=end, duration=end - start, is_fade=True)
        )
    return intervals, []


# ---------------------------------------------------------------------------
# 组合入口
# ---------------------------------------------------------------------------


def detect_all(
    video: str | Path,
    ffmpeg: str | Path,
    *,
    threshold: float = DEFAULT_CONTENT_THRESHOLD,
    min_scene_len_frames: int = DEFAULT_MIN_SCENE_LEN_FRAMES,
    detect_blacks: bool = True,
    cache: CacheStore | None = None,
    fingerprint: SourceFingerprint | None = None,
) -> ShotDetectionResult:
    """镜头 + 黑场一次性检测，可带缓存（§12.1 与分集参数无关，可长期复用）。"""
    import time

    key = None
    if cache is not None and fingerprint is not None:
        key = CacheKey.build(
            "shots",
            fingerprint,
            threshold=threshold,
            min_scene_len_frames=min_scene_len_frames,
            detect_blacks=detect_blacks,
        )
        cached = cache.load(key)
        if cached is not None:
            return ShotDetectionResult.from_json(cached)

    started = time.time()
    scenes, scene_issues = detect_scenes(
        video, threshold=threshold, min_scene_len_frames=min_scene_len_frames
    )
    blacks: list[BlackInterval] = []
    black_issues: list[Issue] = []
    if detect_blacks:
        blacks, black_issues = detect_black_intervals(ffmpeg, video)

    result = ShotDetectionResult(
        scenes=scenes,
        blacks=blacks,
        threshold=threshold,
        elapsed_seconds=time.time() - started,
        issues=[*scene_issues, *black_issues],
    )

    # 只在"检测确实跑完"时写缓存。"跑完但没找到镜头"同样值得缓存——
    # 否则每次都要白跑一遍慢检测（见 _detection_completed）。
    if cache is not None and key is not None and _detection_completed(result):
        cache.save(key, result.to_json(), note="镜头与黑场检测")

    return result


def nearest_scene_boundaries(
    scenes: list[SceneBoundary],
    target: Fraction,
    *,
    max_distance: Fraction = Fraction(3),
) -> list[SceneBoundary]:
    """找出目标时刻附近的镜头边界，供候选点合并与证据展示使用。"""
    return [s for s in scenes if abs(s.time - target) <= max_distance]


def fades_within(
    blacks: list[BlackInterval],
    start: Fraction,
    end: Fraction,
) -> list[BlackInterval]:
    """返回与给定区间有交叠的黑场段。"""
    return [b for b in blacks if b.end > start and b.start < end]


def describe_boundary(scene: SceneBoundary, timeline_duration: Fraction) -> str:
    position = float(scene.time) / float(timeline_duration) * 100 if timeline_duration else 0.0
    return (
        f"{format_timecode(scene.time)}（第 {scene.start_frame} 帧，"
        f"镜头时长 {float(scene.duration):.2f}s，片长 {position:.1f}% 处）"
    )


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
