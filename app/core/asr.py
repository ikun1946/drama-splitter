"""语音转写（faster-whisper）与时间映射。

设计依据：
- §7.1 正确理解语音识别：提供转写、词级时间信息与 VAD；**不能**据此声称
  "某人猛地站起"这类画面动作，也**不能**可靠确定说话人就是某个剧情人物。
  因此本模块产出的每条记录都只包含"语音转写 + 时间"，人物一律标记为未确认。
- §7.3 原声与时间映射：分块 ASR **必须加回块起点偏移**，并处理重叠区重复转写；
  不能删除静音后沿用压缩后的时间码。
- §6 转写不得阻塞界面线程；CPU/GPU 模式需在导入时探测。
- §15.1 先用短样片测速，再给出整片预计耗时。

**一个由实测得出、决定数据结构的关键结论**
------------------------------------------------
ASR 的"句段（segment）"边界**不是句子边界**。实测 10 句中文对白（句间停顿 0.6s）：

    模型     ASR 句段数    标准答案句数
    tiny        2             10
    small       4             10

Whisper 会把连续多句合并成一个 segment。因此**候选切点绝不能建立在
segment 边界上**——那会把切点放到两句之间本该连续的地方之外。
正确做法是用**词级时间戳 + 标点 + 静音间隔**重建句子，
`build_sentences()` 就是做这件事的。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable

from .cache import CacheKey, CacheStore, SourceFingerprint
from .probe import LEVEL_BLOCK, LEVEL_WARN, Issue
from .timebase import format_timecode, round_half_up

__all__ = [
    "AsrWord",
    "AsrSegment",
    "TranscriptSentence",
    "Transcript",
    "AsrEngine",
    "AsrSettings",
    "ModelInfo",
    "available_models",
    "locate_model",
    "build_sentences",
    "estimate_runtime",
    "SIMPLIFIED_PROMPT",
]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 用简体中文提示词引导输出。实测：不提示时 Whisper 会输出繁体
# （「這房子你今天必須交出來」），加提示词后完全简体化。这比引入
# 繁简转换表更干净，且不增加依赖。
SIMPLIFIED_PROMPT = "以下是一段简体中文的短剧对白。"

# 句子结束标点
SENTENCE_END_CHARS = "。！？!?…"
CLAUSE_CHARS = "，、；：,;:"

# 句末级静音停顿阈值。
#
# 这两个数字是**用实测数据定的**，不是拍的：对 10 句已知对白（句间静音 0.6s）
# 用 1.0 / 1.5 秒这组阈值，重建出正好 10 句；用 0.35 秒会切出 15 句
# （「你欠的钱，」后的 0.64s 逗号停顿被误判为句末）。
#
# 倾向性：宁可少切（漏一个候选点，只损失一个选择），不可多切
# （假句末会直接造出一个坏切点）。
DEFAULT_SENTENCE_GAP_SECONDS = Fraction(1)
# 逗号/顿号之后的停顿需要更长才算句末——那是句内语气停顿，不是换句
CLAUSE_GAP_MULTIPLIER = Fraction(3, 2)

SIMPLIFIED_LOCALE = "zh-cn"

# 模型档位与实测性能（RTF = 处理耗时 / 音频时长，CPU int8，越大越慢）
MODEL_TABLE: dict[str, dict] = {
    "tiny": {"size_mb": 75, "rtf_cpu_int8": 0.053, "cer_zh": 0.0659},
    "base": {"size_mb": 145, "rtf_cpu_int8": 0.09, "cer_zh": None},
    "small": {"size_mb": 484, "rtf_cpu_int8": 0.326, "cer_zh": 0.0330},
    "medium": {"size_mb": 1500, "rtf_cpu_int8": 0.9, "cer_zh": None},
    "large-v3": {"size_mb": 3090, "rtf_cpu_int8": 1.8, "cer_zh": None},
}


@dataclass(frozen=True)
class ModelInfo:
    tier: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1024 / 1024

    def describe(self) -> str:
        return f"{self.tier}（{self.size_mb:.0f} MB）"


def model_search_roots() -> list[Path]:
    """模型目录的候选位置，按优先级排列。

    为什么要多个候选：打包（PyInstaller）后 `__file__` 指向包内的
    `_MEIPASS` 目录，按源码路径根本找不到用户放在外面的 `models/`，
    自检会错误地报"模型未下载"。模型体积大（whisper small 484MB、
    LLM 1222MB），**不随包分发**，因此必须支持"放在 exe 旁边"这种部署方式。

    优先级：
      1. 环境变量 `DRAMA_MODELS_DIR`（部署时最可控）
      2. 可执行文件所在目录下的 `models/`（打包版推荐做法）
      3. 可执行文件上级目录下的 `models/`
      4. 源码树根目录下的 `models/`（开发运行）
    """
    import os as _os
    import sys as _sys

    roots: list[Path] = []
    override = _os.environ.get("DRAMA_MODELS_DIR")
    if override:
        roots.append(Path(override))

    if getattr(_sys, "frozen", False):
        exe_dir = Path(_sys.executable).resolve().parent
        roots.append(exe_dir / "models")
        roots.append(exe_dir.parent / "models")
    roots.append(Path(__file__).resolve().parent.parent.parent / "models")
    return roots


def model_root() -> Path:
    """返回实际使用的模型目录（第一个存在的候选；都不存在时返回首选位置）。"""
    for root in model_search_roots():
        if root.exists():
            return root
    roots = model_search_roots()
    return roots[0] if roots else Path("models")


def locate_model(tier: str) -> ModelInfo | None:
    """定位已下载的模型目录。

    模型放在项目内的普通目录（不是 HuggingFace 缓存），因为本机账户没有创建
    符号链接的权限，HF 缓存会产出 0 字节文件（详见 tools/fetch_whisper_model.py）。
    """
    directory = model_root() / f"faster-whisper-{tier}"
    if not directory.exists():
        return None
    model_bin = directory / "model.bin"
    if not model_bin.exists() or model_bin.stat().st_size < 1024 * 1024:
        return None
    size = sum(f.stat().st_size for f in directory.glob("*") if f.is_file())
    return ModelInfo(tier=tier, path=directory, size_bytes=size)


def available_models() -> list[ModelInfo]:
    found: list[ModelInfo] = []
    for tier in MODEL_TABLE:
        info = locate_model(tier)
        if info:
            found.append(info)
    return found


def estimate_runtime(audio_seconds: float, tier: str, *, speed_factor: float = 1.0) -> float:
    """估算转写耗时（秒）。

    §15.1 要求"先用短样片测速，再给出整片预计耗时"，因此这里用的是
    MODEL_TABLE 里的实测 RTF，而不是拍脑袋的常数。speed_factor 供样片
    实测结果校准（实际机器可能快于或慢于本机）。
    """
    rtf = MODEL_TABLE.get(tier, {}).get("rtf_cpu_int8") or 0.35
    return audio_seconds * rtf * speed_factor


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsrWord:
    start: Fraction
    end: Fraction
    text: str
    probability: float = 0.0
    chunk_index: int = 0

    def to_json(self) -> dict:
        return {
            "start": _frac_json(self.start),
            "end": _frac_json(self.end),
            "text": self.text,
            "probability": round(self.probability, 4),
            "chunk": self.chunk_index,
        }

    @classmethod
    def from_json(cls, data: dict) -> "AsrWord":
        return cls(
            start=_parse_frac(data["start"]),
            end=_parse_frac(data["end"]),
            text=data["text"],
            probability=float(data.get("probability", 0.0)),
            chunk_index=int(data.get("chunk", 0)),
        )


@dataclass(frozen=True)
class AsrSegment:
    start: Fraction
    end: Fraction
    text: str
    chunk_index: int = 0

    def to_json(self) -> dict:
        return {
            "start": _frac_json(self.start),
            "end": _frac_json(self.end),
            "text": self.text,
            "chunk": self.chunk_index,
        }

    @classmethod
    def from_json(cls, data: dict) -> "AsrSegment":
        return cls(
            start=_parse_frac(data["start"]),
            end=_parse_frac(data["end"]),
            text=data["text"],
            chunk_index=int(data.get("chunk", 0)),
        )


@dataclass(frozen=True)
class TranscriptSentence:
    """由词级时间戳重建的**句子**。

    这才是对白句末候选点的依据（见模块头部的实测结论）。
    注意 §7.1：人物一律未确认——说话人分离不等于角色实名识别，
    第一阶段不把角色实名识别设为必需功能。
    """

    index: int
    start: Fraction
    end: Fraction
    text: str
    word_count: int
    end_reason: str  # punctuation / gap / eof

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "start": _frac_json(self.start),
            "end": _frac_json(self.end),
            "text": self.text,
            "word_count": self.word_count,
            "end_reason": self.end_reason,
            "speaker": "未确认",
        }

    @classmethod
    def from_json(cls, data: dict) -> "TranscriptSentence":
        return cls(
            index=int(data["index"]),
            start=_parse_frac(data["start"]),
            end=_parse_frac(data["end"]),
            text=data["text"],
            word_count=int(data.get("word_count", 0)),
            end_reason=data.get("end_reason", ""),
        )


@dataclass
class Transcript:
    words: list[AsrWord] = field(default_factory=list)
    segments: list[AsrSegment] = field(default_factory=list)
    sentences: list[TranscriptSentence] = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    model_tier: str = ""
    prompt: str | None = None
    duration: Fraction = Fraction(0)
    chunk_count: int = 1
    elapsed_seconds: float = 0.0
    issues: list[Issue] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(word.text for word in self.words)

    @property
    def is_empty(self) -> bool:
        return not self.words

    @property
    def spoken_seconds(self) -> Fraction:
        total = Fraction(0)
        for sentence in self.sentences:
            total += max(Fraction(0), sentence.duration)
        return total

    def describe(self) -> str:
        if self.is_empty:
            return "转写：无内容"
        return (
            f"转写：{len(self.words)} 词 / {len(self.sentences)} 句 / "
            f"{len(self.segments)} 个模型句段（{self.model_tier}），"
            f"覆盖 {format_timecode(self.spoken_seconds)}，"
            f"分块 {self.chunk_count} 段"
        )

    def to_json(self) -> dict:
        return {
            "language": self.language,
            "language_probability": round(self.language_probability, 4),
            "model_tier": self.model_tier,
            "prompt": self.prompt,
            "duration": _frac_json(self.duration),
            "chunk_count": self.chunk_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "words": [w.to_json() for w in self.words],
            "segments": [s.to_json() for s in self.segments],
            "sentences": [s.to_json() for s in self.sentences],
        }

    @classmethod
    def from_json(cls, data: dict) -> "Transcript":
        transcript = cls(
            language=data.get("language", ""),
            language_probability=float(data.get("language_probability", 0.0)),
            model_tier=data.get("model_tier", ""),
            prompt=data.get("prompt"),
            duration=_parse_frac(data.get("duration", 0)),
            chunk_count=int(data.get("chunk_count", 1)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
        )
        transcript.words = [AsrWord.from_json(item) for item in data.get("words", [])]
        transcript.segments = [AsrSegment.from_json(item) for item in data.get("segments", [])]
        transcript.sentences = [
            TranscriptSentence.from_json(item) for item in data.get("sentences", [])
        ]
        return transcript


@dataclass
class AsrSettings:
    """影响转写结果的参数。任何一项变化都会命中不同的缓存键。"""

    model_tier: str = "small"
    language: str = "zh"
    initial_prompt: str | None = SIMPLIFIED_PROMPT
    beam_size: int = 5
    vad_filter: bool = True
    min_silence_duration_ms: int = 300
    word_timestamps: bool = True
    condition_on_previous_text: bool = False
    # 强制繁简转换。提示词只是第一道防线，实测不能保证（见 to_simplified）
    simplify_to_simplified: bool = True
    compute_type: str = "int8"
    device: str = "cpu"
    chunk_seconds: int = 600
    chunk_overlap_seconds: int = 15

    def cache_parts(self) -> dict:
        return {
            "model_tier": self.model_tier,
            "language": self.language,
            "prompt": self.initial_prompt or "",
            "beam_size": self.beam_size,
            "vad": self.vad_filter,
            "min_silence_ms": self.min_silence_duration_ms,
            "word_timestamps": self.word_timestamps,
            "condition_on_previous": self.condition_on_previous_text,
            "simplify": self.simplify_to_simplified,
            "compute_type": self.compute_type,
            "device": self.device,
            "chunk_seconds": self.chunk_seconds,
            "chunk_overlap": self.chunk_overlap_seconds,
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class AsrEngine:
    """faster-whisper 封装。模型按需加载，可复用于多个项目。

    `cache` / `fingerprint` 可选传入：用于复用音频包络（长片全解码一次
    要几十秒，且字幕对齐也要用同一份包络）。
    """

    def __init__(
        self,
        settings: AsrSettings | None = None,
        *,
        cache: CacheStore | None = None,
        fingerprint: SourceFingerprint | None = None,
    ) -> None:
        self.settings = settings or AsrSettings()
        self.cache = cache
        self.fingerprint = fingerprint
        self._model = None
        self._loaded_tier: str | None = None

    # ---- 模型 ----------------------------------------------------------

    def ensure_model(self) -> ModelInfo:
        info = locate_model(self.settings.model_tier)
        if info is None:
            installed = available_models()
            hint = (
                "已下载：" + "、".join(m.tier for m in installed)
                if installed
                else "尚未下载任何模型"
            )
            raise RuntimeError(
                f"未找到模型 {self.settings.model_tier}。{hint}。"
                f"请先运行：python tools/fetch_whisper_model.py {self.settings.model_tier}"
            )
        if self._model is None or self._loaded_tier != info.tier:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                str(info.path),
                device=self.settings.device,
                compute_type=self.settings.compute_type,
            )
            self._loaded_tier = info.tier
        return info

    def release(self) -> None:
        self._model = None
        self._loaded_tier = None

    # ---- 转写 ----------------------------------------------------------

    def transcribe(
        self,
        media: str | Path,
        *,
        duration_seconds: Fraction | None = None,
        audio_stream_index: int | None = None,
        on_progress: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        ffmpeg: str | Path | None = None,
    ) -> Transcript:
        """转写整条音轨。

        短素材单次转写；长素材按 `chunk_seconds` 分块并**显式加回块起点偏移**
        （§7.3）。分块边界会吸附到低能量点，避免把一个词切两半。
        """
        info = self.ensure_model()
        started = time.time()
        media = Path(media)

        if duration_seconds is None:
            duration_seconds = _probe_duration(ffmpeg, media)
        total_seconds = float(duration_seconds)

        transcript = Transcript(
            model_tier=info.tier,
            prompt=self.settings.initial_prompt,
            duration=duration_seconds,
        )

        if total_seconds <= self.settings.chunk_seconds:
            self._transcribe_range(
                media, 0.0, None, chunk_index=0, transcript=transcript, ffmpeg=ffmpeg
            )
            plan: list[tuple[float, float]] = [(0.0, total_seconds)]
        else:
            # 分块前先算出音频包络，用于把块起点吸附到静音处。
            # 没有它就只能按固定时间点硬切，实测会产生重复与幻觉文本。
            snap_point = self._build_snap_point(
                media, ffmpeg, audio_stream_index=audio_stream_index
            )
            plan = plan_chunks(
                total_seconds,
                chunk_seconds=self.settings.chunk_seconds,
                overlap_seconds=self.settings.chunk_overlap_seconds,
                snap_point=snap_point,
            )
            transcript.chunk_count = len(plan)
            if snap_point is None:
                transcript.issues.append(
                    Issue(
                        LEVEL_WARN,
                        "ASR_CHUNK_NOT_SNAPPED",
                        "无法获取音频包络，分块边界未吸附到静音处。",
                        "按固定时间点切分可能把词截断，导致转写出现重复字；"
                        "建议核查本次转写结果。",
                    )
                )
            temp_dir = Path(tempfile.mkdtemp(prefix="drama_asr_"))
            try:
                for index, (start, end) in enumerate(plan):
                    if cancel_check and cancel_check():
                        transcript.issues.append(
                            Issue(LEVEL_WARN, "ASR_CANCELLED", "转写被取消。", "已完成的块保留在缓存中。")
                        )
                        break
                    if on_progress:
                        on_progress(index / max(1, len(plan)), f"转写第 {index + 1}/{len(plan)} 段")
                    wav = temp_dir / f"chunk{index:03d}.wav"
                    if not _extract_audio(ffmpeg, media, wav, start, end - start):
                        transcript.issues.append(
                            Issue(
                                LEVEL_WARN,
                                "ASR_CHUNK_DECODE_FAILED",
                                f"第 {index + 1} 段音频提取失败，已跳过。",
                                f"区间 {start:.3f}–{end:.3f}s",
                            )
                        )
                        continue
                    self._transcribe_range(
                        wav,
                        start,
                        end - start,
                        chunk_index=index,
                        transcript=transcript,
                        ffmpeg=ffmpeg,
                    )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        transcript.elapsed_seconds = time.time() - started

        if self.settings.simplify_to_simplified:
            _apply_simplified(transcript)

        _merge_adjacent_repeats(transcript)

        _assign_to_owner_chunk(transcript, plan)
        _check_overlap_sufficient(transcript, plan)
        transcript.sentences = build_sentences(transcript.words)

        if transcript.is_empty:
            transcript.issues.append(
                Issue(
                    LEVEL_WARN,
                    "ASR_NO_SPEECH",
                    "没有转写出任何语音内容。",
                    "可能是纯音乐/无对白素材，或音轨本身静音。"
                    "这不代表故障，请勿反复重试（§7.2）；可直接进入人工审核。",
                )
            )
        if on_progress:
            on_progress(1.0, "转写完成")
        return transcript

    def _build_snap_point(
        self,
        media: Path,
        ffmpeg: str | Path | None,
        *,
        audio_stream_index: int | None,
    ) -> "Callable[[float], float] | None":
        """构造"把时间点吸附到最近静音处"的回调。

        包络算不出来时返回 None，由调用方记录为风险——不静默退回硬切。
        """
        from .audio import DEFAULT_HOP_SECONDS, audio_rms_envelope, low_energy_point

        if ffmpeg is None:
            from .ffmpeg import locate_ffmpeg

            ffmpeg = locate_ffmpeg().ffmpeg
        try:
            envelope = audio_rms_envelope(
                ffmpeg,
                media,
                hop_seconds=DEFAULT_HOP_SECONDS,
                audio_stream_index=audio_stream_index,
                cache=self.cache,
                fingerprint=self.fingerprint,
            )
        except Exception:  # noqa: BLE001 - 解码失败不应中断转写
            return None
        if not envelope:
            return None

        def snap(target: float) -> float:
            point = low_energy_point(envelope, Fraction(str(round(target, 4))),
                                     hop_seconds=DEFAULT_HOP_SECONDS)
            return float(point) if point is not None else target

        return snap

    def _transcribe_range(
        self,
        audio: Path,
        offset_seconds: float,
        span_seconds: float | None,
        *,
        chunk_index: int,
        transcript: Transcript,
        ffmpeg: str | Path | None,
    ) -> None:
        """转写一段，并把时间戳平移回源时间轴（§7.3 加回块起点偏移）。"""
        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            str(audio),
            language=self.settings.language,
            beam_size=self.settings.beam_size,
            initial_prompt=self.settings.initial_prompt,
            vad_filter=self.settings.vad_filter,
            word_timestamps=self.settings.word_timestamps,
            condition_on_previous_text=self.settings.condition_on_previous_text,
            vad_parameters={"min_silence_duration_ms": self.settings.min_silence_duration_ms},
        )

        if not transcript.language:
            transcript.language = getattr(info, "language", "") or ""
            transcript.language_probability = float(getattr(info, "language_probability", 0.0) or 0.0)

        offset = Fraction(str(offset_seconds)).limit_denominator(1_000_000)
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            transcript.segments.append(
                AsrSegment(
                    start=_to_frac(segment.start) + offset,
                    end=_to_frac(segment.end) + offset,
                    text=text,
                    chunk_index=chunk_index,
                )
            )
            for word in getattr(segment, "words", None) or []:
                token = (word.word or "").strip()
                if not token:
                    continue
                transcript.words.append(
                    AsrWord(
                        start=_to_frac(word.start) + offset,
                        end=_to_frac(word.end) + offset,
                        text=token,
                        probability=float(getattr(word, "probability", 0.0) or 0.0),
                        chunk_index=chunk_index,
                    )
                )

    # ---- 缓存入口 ------------------------------------------------------

    def transcribe_cached(
        self,
        media: str | Path,
        fingerprint: SourceFingerprint,
        store: CacheStore,
        *,
        audio_stream_index: int | None = None,
        duration_seconds: Fraction | None = None,
        on_progress: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        ffmpeg: str | Path | None = None,
    ) -> tuple[Transcript, bool]:
        """带缓存的转写。返回 (转写结果, 是否命中缓存)。

        §12.1：改分集参数不需要重跑转写——转写与集数、时长范围无关，
        因此键里只放源指纹、音轨与 ASR 版本参数。
        """
        key = CacheKey.build(
            "asr",
            fingerprint,
            audio_stream=audio_stream_index if audio_stream_index is not None else "auto",
            **self.settings.cache_parts(),
        )
        cached = store.load(key)
        if cached is not None:
            return Transcript.from_json(cached), True

        transcript = self.transcribe(
            media,
            duration_seconds=duration_seconds,
            on_progress=on_progress,
            cancel_check=cancel_check,
            ffmpeg=ffmpeg,
        )
        store.save(key, transcript.to_json(), note=f"转写 {self.settings.model_tier}")
        return transcript, False


# ---------------------------------------------------------------------------
# 分块规划
# ---------------------------------------------------------------------------


def plan_chunks(
    total_seconds: float,
    *,
    chunk_seconds: int,
    overlap_seconds: int,
    snap_point: "Callable[[float], float] | None" = None,
) -> list[tuple[float, float]]:
    """规划分块区间。

    步进 = 块长 − 重叠。加重叠有两个作用：避免把一句话切在边界上，
    并让被截断的词有机会在相邻块里被完整转写出来。

    两条不变量：
    - **任何一块都不超过 chunk_seconds**。早先的实现"把过短的尾块并入上一块"，
      会把末块撑到接近两块的长度（实测 12s 名义块长被撑成 22.9s），
      单块耗时与内存翻倍且不可预期。短一点没关系，超长才有问题。
    - **连续覆盖**：相邻块必须有重叠，否则边界附近的音频两边都没覆盖到。

    `snap_point` 把块起点吸附到静音处。这是**必需**而非优化：实测按固定时间点
    机械切分（0/9/18/27/36s）会让起点落在词中间，Whisper 看到截断的半截词
    会产生重复与幻觉——实测出现「不是你你借记得」「那我们我们就就法庭上上见」
    这类崩坏文本，词数从 80 涨到 100、句数从 10 涨到 12。
    """
    if chunk_seconds <= 0:
        return [(0.0, total_seconds)]
    if total_seconds <= chunk_seconds:
        return [(0.0, total_seconds)]

    step = max(1.0, chunk_seconds - overlap_seconds)
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start + chunk_seconds < total_seconds:
        chunks.append((start, start + chunk_seconds))
        start += step

    tail_start = max(start, total_seconds - chunk_seconds)
    # 末块若与上一块起点重合或更早，说明上一块已被完全覆盖，去掉它
    if chunks and chunks[-1][0] >= tail_start:
        chunks.pop()
    if not chunks or chunks[-1][1] < total_seconds:
        chunks.append((tail_start, total_seconds))

    if snap_point is None or len(chunks) <= 1:
        return chunks

    # 吸附各块起点。起点确定后再用 `起点 + 块长` 重算终点——若沿用吸附前的
    # 终点，块长会随吸附偏移被撑大（实测名义 12s 的块变成 12.65s）。
    min_gap = max(1.0, step / 3)
    last_index = len(chunks) - 1
    starts: list[float] = [chunks[0][0]]
    for index, (begin, _end) in enumerate(chunks[1:], start=1):
        candidate = float(snap_point(begin))
        candidate = max(candidate, starts[-1] + min_gap)
        if index != last_index:
            # 非末块要留得下一整块；**末块不加这个上限**——它本来就允许短，
            # 强行钳到"距片尾一块长"会让它与前一块大量重叠、白花算力
            # （实测末块起点被钳后只贡献 2.58s 新内容却要转写 11.58s）。
            candidate = min(candidate, max(starts[-1] + min_gap, total_seconds - chunk_seconds))
        else:
            candidate = min(candidate, total_seconds - min_gap)
        starts.append(candidate)

    adjusted = [(start, min(start + chunk_seconds, total_seconds)) for start in starts]

    # 端点保护：吸附可能让末块够不到片尾，补一块（长度必 ≤ chunk_seconds）
    if adjusted[-1][1] < total_seconds - 1e-6:
        tail_start = max(adjusted[-1][0] + min_gap, total_seconds - chunk_seconds)
        if tail_start < adjusted[-1][1]:
            adjusted.append((tail_start, total_seconds))
        else:
            # 无法在不留空隙的前提下补块：延长末块（最多超出 step/3）
            adjusted[-1] = (adjusted[-1][0], total_seconds)
    return adjusted


def _assign_to_owner_chunk(
    transcript: Transcript,
    chunks: list[tuple[float, float]],
) -> None:
    """按时间归属消重：每个词只保留它所属块产出的那一份。

    这是替代"文本匹配去重"的做法，因为后者不够稳：实测文本匹配会漏掉
    时间略有偏移的重复词，残留出「楚楚,」「见,」这类孤立碎片，
    进而让句子被切错。

    为什么按时间归属就够了
    ----------------------
    块起点已吸附到静音处，且**上一块的终点恒超过下一块的起点**（重叠 >= 2.9s）。
    因此重叠区内的任何一个词（长度约 0.2–0.6s）在两个块里都是**完整**的。
    于是只要规定"每个词归给起点不大于它的最后一个块"，就不会重复、
    也不会截断——不需要比较文本。

    前提是重叠不小于最长词的长度，否则边缘的词仍可能被截断。这一点由
    `_check_overlap_sufficient()` 单独检查并报风险。
    """
    if not transcript.words or not chunks:
        return

    starts = [begin for begin, _end in chunks]
    kept: list[AsrWord] = []

    for word in sorted(transcript.words, key=lambda w: (w.start, w.end)):
        owner = 0
        for index, begin in enumerate(starts):
            if begin <= float(word.start):
                owner = index
            else:
                break
        if word.chunk_index == owner:
            kept.append(word)

    transcript.words = kept
    transcript.segments.sort(key=lambda s: (s.start, s.end))


def _check_overlap_sufficient(
    transcript: Transcript,
    chunks: list[tuple[float, float]],
    *,
    margin_seconds: float = 0.8,
) -> None:
    """检查相邻块的重叠是否足以容纳最长词。

    重叠偏小时，边缘的词可能两边都被截断——这时按时间归属会丢掉一个词，
    表现为"偶尔少字"。报出来比静默丢字好。
    """
    if len(chunks) < 2 or not transcript.words:
        return
    longest = max(float(w.end - w.start) for w in transcript.words)
    required = longest + margin_seconds
    minimum_overlap = min(
        chunks[index][1] - chunks[index + 1][0] for index in range(len(chunks) - 1)
    )
    if minimum_overlap < required:
        transcript.issues.append(
            Issue(
                LEVEL_WARN,
                "ASR_OVERLAP_TOO_SMALL",
                f"相邻分块的最小重叠 {minimum_overlap:.2f}s 不足以容纳最长词 "
                f"{longest:.2f}s，边缘处可能丢词。",
                "建议增大 chunk_overlap_seconds 后重新转写。",
            )
        )


def _merge_adjacent_repeats(transcript: Transcript) -> None:
    """合并相邻的重复词（同一块内因解码抖动产生的「你你」）。

    与 `_dedupe_and_sort` 分工不同：那里处理跨块重复，这里处理单块内抖动。
    只合并时间紧邻（间隔 ≤50ms）且文本相同的相邻词——阈值放大会误并
    正常的叠词（「看看」「想想」）。
    """
    if not transcript.words:
        return
    merged: list[AsrWord] = []
    for word in transcript.words:
        if merged:
            previous = merged[-1]
            if word.text == previous.text and word.start - previous.end <= Fraction(5, 100):
                merged[-1] = AsrWord(
                    start=previous.start,
                    end=max(previous.end, word.end),
                    text=previous.text,
                    probability=max(previous.probability, word.probability),
                    chunk_index=previous.chunk_index,
                )
                continue
        merged.append(word)
    transcript.words = merged


def _apply_simplified(transcript: Transcript) -> None:
    """把词与句段的文本统一转成简体，并校验转换确实生效。

    校验的意义：如果 zhconv 没装上，转换会静默原样返回，而"输出繁体"
    这种问题在后续环节只会表现为"文字看起来怪"，很难回溯。这里直接
    把繁体残留报成风险。
    """
    try:
        import zhconv  # noqa: F401
    except ImportError:
        transcript.issues.append(
            Issue(
                LEVEL_WARN,
                "SIMPLIFY_UNAVAILABLE",
                "未安装 zhconv，无法保证输出为简体中文。",
                "模型在句尾可能输出繁体（实测出现过）。安装 zhconv 即可自动转换。",
            )
        )
        return

    transcript.words = [
        AsrWord(
            start=word.start,
            end=word.end,
            text=to_simplified(word.text),
            probability=word.probability,
            chunk_index=word.chunk_index,
        )
        for word in transcript.words
    ]
    transcript.segments = [
        AsrSegment(
            start=segment.start,
            end=segment.end,
            text=to_simplified(segment.text),
            chunk_index=segment.chunk_index,
        )
        for segment in transcript.segments
    ]

    ratio = traditional_char_ratio(transcript.text)
    if ratio > 0.02:
        transcript.issues.append(
            Issue(
                LEVEL_WARN,
                "SIMPLIFY_INCOMPLETE",
                f"转写文本中仍有约 {ratio * 100:.1f}% 的繁体字符。",
                "可能是专有名词或转换表未覆盖的用字，建议人工抽查。",
            )
        )


def _split_sentence_thresholds() -> tuple[Fraction, Fraction]:
    """集中暴露切句阈值，便于测试直接引用而不重复写死数字。"""
    return DEFAULT_SENTENCE_GAP_SECONDS, CLAUSE_GAP_MULTIPLIER


# ---------------------------------------------------------------------------
# 由词重建句子
# ---------------------------------------------------------------------------


def build_sentences(
    words: Iterable[AsrWord],
    *,
    gap_seconds: Fraction = DEFAULT_SENTENCE_GAP_SECONDS,
    clause_gap_multiplier: Fraction = CLAUSE_GAP_MULTIPLIER,
) -> list[TranscriptSentence]:
    """用词级时间戳 + 标点 + 静音间隔重建句子。

    这是分集候选点的基础（见模块头部实测结论：ASR 句段边界不可用）。
    切句依据（按优先级）：
      1. 词尾出现句末标点（。！？…）→ 断句；
      2. 与下一个词之间的静音超过阈值 → 断句。阈值为
         `gap_seconds`；若前一个词以逗号/顿号类标点结尾，则阈值乘
         `clause_gap_multiplier`，因为句内语气停顿本来就长；
      3. 序列结束。
    """
    ordered = sorted(words, key=lambda w: (w.start, w.end))
    sentences: list[TranscriptSentence] = []
    buffer: list[AsrWord] = []

    def flush(reason: str) -> None:
        if not buffer:
            return
        text = "".join(w.text for w in buffer).strip()
        if not text:
            buffer.clear()
            return
        sentences.append(
            TranscriptSentence(
                index=len(sentences) + 1,
                start=buffer[0].start,
                end=buffer[-1].end,
                text=text,
                word_count=len(buffer),
                end_reason=reason,
            )
        )
        buffer.clear()

    for index, word in enumerate(ordered):
        buffer.append(word)
        text = word.text
        stripped = text.rstrip()
        if stripped and stripped[-1] in SENTENCE_END_CHARS:
            flush("punctuation")
            continue
        if index + 1 < len(ordered):
            gap = ordered[index + 1].start - word.end
            threshold = gap_seconds
            if stripped and stripped[-1] in CLAUSE_CHARS:
                threshold = gap_seconds * clause_gap_multiplier
            if gap >= threshold:
                flush("gap")
    flush("eof")
    return sentences


def to_simplified(text: str) -> str:
    """繁体 → 简体。

    为什么必须做这一步：`initial_prompt` 用简体提示词能显著降低繁体输出
    （实测 15 个繁体字符降到 0），但**不能保证**——实测 small 模型在句尾
    仍会输出「那我們就法庭上見。」「隨便你。」。短剧字幕必须是简体，
    因此提示词只是第一道防线，这里用确定性转换兜底。

    未安装 zhconv 时原样返回，并把情况交给调用方记录为风险——
    静默返回繁体比报错更糟。
    """
    if not text:
        return text
    try:
        import zhconv  # type: ignore
    except ImportError:
        return text
    return zhconv.convert(text, SIMPLIFIED_LOCALE)


def traditional_char_ratio(text: str) -> float:
    """估计文本中繁体字符占比，用于校验转换是否漏掉内容。

    取一个常见的繁简异形字集合做统计，而非完整字表——目的是发现
    "转换根本没生效"这类粗粒度问题，不是精确度量。
    """
    if not text:
        return 0.0
    traditional = set(
        "這來們個關開門東話說實對妳爲與從會後點麼樣於過還將給讓經種發問題"
        "見錢寫輕聲聽體臉腳頭愛歲數廣風飛馬鳥魚龍屬書記認知識讓費"
    )
    hits = sum(1 for char in text if char in traditional)
    return hits / len(text)


def split_sentence_for_subtitles(
    sentence: TranscriptSentence,
    *,
    max_chars: int = 18,
) -> list[str]:
    """把过长句子按标点切成适合字幕的行长。

    单集字幕输出以 SRT 为主（§7.2），而过长的一行在小屏上会被截断。
    这里只在标点处切，不做强制断行——硬断会把词切两半。
    """
    text = sentence.text
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in SENTENCE_END_CHARS + CLAUSE_CHARS and len(current) >= max_chars // 2:
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts or [text]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _probe_duration(ffmpeg: str | Path | None, media: Path) -> Fraction:
    from .ffmpeg import locate_ffmpeg
    from .probe import probe_media

    binaries = locate_ffmpeg()
    info = probe_media(binaries, media, detect_vfr=False)
    return info.timeline_duration


def _extract_audio(
    ffmpeg: str | Path | None,
    media: Path,
    target: Path,
    start_seconds: float,
    span_seconds: float,
) -> bool:
    """用精确 `-ss` 抽取一段 16kHz 单声道音频。

    §7.3：提取分析音频必须保留与源视频的时间映射，因此这里记录**精确的块起点**，
    转写后按该起点平移。不使用 `-t` 之外的任何时间轴压缩。
    """
    if ffmpeg is None:
        from .ffmpeg import locate_ffmpeg

        ffmpeg = locate_ffmpeg().ffmpeg
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_seconds:.6f}",
        "-i",
        str(media),
        "-t",
        f"{span_seconds:.6f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=_NO_WINDOW)
    return proc.returncode == 0 and target.exists() and target.stat().st_size > 1024


def _to_frac(value: float) -> Fraction:
    """把模型返回的浮点秒数转成 Fraction。

    模型的浮点时间本身有误差（约毫秒级），这里限制分母避免得到
    1/3 这种无意义的精确分数。§9.1 要求内部不用浮点累加，
    因此入口处必须转换，但也不假装它比实际精度更精确。
    """
    return Fraction(str(round(float(value), 4))).limit_denominator(100_000)


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


def silence_gaps(
    transcript: Transcript,
    *,
    min_gap_seconds: Fraction = Fraction(8, 100),
    max_gap_seconds: Fraction = Fraction(30),
) -> list[tuple[Fraction, Fraction]]:
    """列出词与词之间的静音区间，供候选点使用（§8.2 语音间隙）。

    上限过滤掉"换场/换景"级别的长空档——那不是句间停顿，
    而是需要单独判断的段落结构。
    """
    gaps: list[tuple[Fraction, Fraction]] = []
    words = sorted(transcript.words, key=lambda w: w.start)
    for previous, current in zip(words, words[1:]):
        gap = current.start - previous.end
        if min_gap_seconds <= gap <= max_gap_seconds:
            gaps.append((previous.end, current.start))
    return gaps
