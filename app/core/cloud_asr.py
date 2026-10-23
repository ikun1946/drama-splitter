"""云端转写：厂商 API 作为 `AsrEngine` 的另一个实现。

设计依据（docs/云端模型接入_设计.md §3）
---------------------------------------
**流水线一条都不重写**：分块（静音吸附）、抽音（16k 单声道）、块起点偏移回填、
跨块去重、句子重建、取消与进度、缓存——全部沿用 `AsrEngine` 的同一套代码，
本类只覆盖"每块怎么转写"这一处：把音频交给 `AsrTransport`。

为什么用继承而不是复制一份
--------------------------
复制一份编排逻辑意味着以后每修一次分块/去重，都要记得改两处；而"分块边界吸附
静音""偏移必须加回"这些规则是实测定下来的（§7.3），漏改的代价是切点错位。
继承之后，差异只剩 :meth:`CloudAsrEngine._transcribe_range` 一个方法。

两个必须知道的差异
------------------
1. 云端**不接受 mp4**（只收 wav / mp3 / ogg），因此源片必须先抽音轨——本地路径
   是把整片直接交给 Whisper 的，云端不能照抄。抽音后优先压成 mp3 省流量。
2. 云端失败是**整轮回退**，不做"半云端半本地"：块级失败会记录是第几段，
   由上层决定回退本地重跑（设计 §7）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from .app_config import AppConfig
from .asr import AsrEngine, AsrSegment, AsrSettings, AsrWord, ModelInfo, extract_audio
from .cache import CacheKey, CacheStore, SourceFingerprint
from .cloud import build_asr_transport
from .probe import LEVEL_WARN, Issue
from .providers import (
    CAP_ASR,
    PROTO_LABELS,
    AsrTransport,
    ProviderError,
    ProviderProfile,
    ProviderTranscript,
    ProviderWord,
    asr_target_host,
    block_reason,
    describe_egress,
)

__all__ = ["CloudAsrEngine", "select_asr_engine"]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: 上传前的压缩码率（16k 单声道语音足够；600 秒约 4.8MB，远低于 100MB 上限）
_UPLOAD_BITRATE = "64k"


def _encoder_listed(ffmpeg: str | Path, name: str) -> bool:
    """`ffmpeg -encoders` 里有没有这个编码器。

    刻意不用 `app.core.ffmpeg.check_encoders()`：那个函数会把结果**写回**
    `FfmpegBinaries.encoders`，而那份数据同时被兼容性检查（libx264 / aac）读取。
    为了查一个 mp3 编码器去改共享状态，可能把导入时的"编码器齐备"结论弄坏。
    """
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        len(parts) >= 2 and parts[1] == name
        for parts in (line.split() for line in (proc.stdout or "").splitlines())
    )


def _ms_to_frac(value: int) -> Fraction:
    """毫秒 → 秒的精确分数（整数运算，不经过 float）。"""
    return Fraction(int(value), 1000)


class CloudAsrEngine(AsrEngine):
    """云端转写引擎：与本地 `AsrEngine` 同形（`transcribe` / `transcribe_cached`）。"""

    #: 让上层一眼看出"这一轮是谁算的"（回退逻辑据此决定）
    is_cloud = True

    def __init__(
        self,
        transport: AsrTransport,
        profile: ProviderProfile,
        *,
        settings: AsrSettings | None = None,
        cache: CacheStore | None = None,
        fingerprint: SourceFingerprint | None = None,
    ) -> None:
        super().__init__(settings, cache=cache, fingerprint=fingerprint)
        self._transport = transport
        self._profile = profile
        self.backend_id = f"cloud:{profile.id}/{profile.asr_model_name}"
        #: 块级失败的原话（含"第几段"）：上层用它告诉用户到底哪一块出了问题
        self.last_chunk_failure: str | None = None
        self._source: Path | None = None
        self._work_dir: Path | None = None
        self._upload_seq = 0
        self._has_mp3: bool | None = None
        self._warned_word_timestamps = False

    # ---- 对外信息 ------------------------------------------------------

    def describe(self) -> str:
        return f"云端转写 {self._profile.label}／{self._profile.asr_model_name}"

    def egress_line(self, transcript) -> str:
        """转写结束后的外发流水（设计 §6 要求的那一行）。"""
        return (
            f"外发流水：音频 {float(transcript.duration):.0f} 秒 / "
            f"{transcript.chunk_count} 块 → {asr_target_host(self._profile)}"
        )

    # ---- 不依赖本地模型 ------------------------------------------------

    def ensure_model(self) -> ModelInfo:
        """云端不需要本地模型；返回一个合成 ModelInfo，让 `model_tier` 记下"谁算的"。"""
        return ModelInfo(tier=self.backend_id, path=Path("<cloud>"), size_bytes=0)

    # ---- 转写编排（沿用基类） -------------------------------------------

    def transcribe(
        self,
        media,
        *,
        duration_seconds: Fraction | None = None,
        audio_stream_index: int | None = None,
        on_progress=None,
        cancel_check=None,
        ffmpeg=None,
    ):
        # 每次转写都从干净的工作目录开始。上一次 _cleanup() 已删掉旧目录，
        # 若这里不重置，复用同一个引擎时抽音会写入一个已不存在的路径，
        # 表现成"源片没有音轨"——这是测试缓存路径暴露出来的真实缺陷。
        self._cleanup()
        self._upload_seq = 0
        self._warned_word_timestamps = False
        self._source = Path(media).resolve()
        self.last_chunk_failure = None
        try:
            return super().transcribe(
                media,
                duration_seconds=duration_seconds,
                audio_stream_index=audio_stream_index,
                on_progress=on_progress,
                cancel_check=cancel_check,
                ffmpeg=ffmpeg,
            )
        finally:
            self._cleanup()

    def transcribe_cached(
        self,
        media,
        fingerprint: SourceFingerprint,
        store: CacheStore,
        *,
        audio_stream_index: int | None = None,
        duration_seconds: Fraction | None = None,
        on_progress=None,
        cancel_check=None,
        ffmpeg=None,
    ) -> tuple[object, bool]:
        """带缓存的转写。

        缓存键里带 `backend`：换后端**绝不能**复用旧转写——云端与本地的时间戳
        分布不同，混用会让审核页看到的切点与方案算出来的不一致。本地键保持原样，
        历史缓存继续命中。
        """
        key = CacheKey.build(
            "asr",
            fingerprint,
            audio_stream=self._audio_key(audio_stream_index),
            backend=self.backend_id,
            **self.settings.cache_parts(),
        )
        cached = store.load(key)
        if cached is not None:
            from .asr import Transcript

            return Transcript.from_json(cached), True

        transcript = self.transcribe(
            media,
            duration_seconds=duration_seconds,
            audio_stream_index=audio_stream_index,
            on_progress=on_progress,
            cancel_check=cancel_check,
            ffmpeg=ffmpeg,
        )
        store.save(key, transcript.to_json(), note=f"转写 {self.backend_id}")
        return transcript, False

    @staticmethod
    def _audio_key(audio_stream_index: int | None):
        return audio_stream_index if audio_stream_index is not None else "auto"

    # ---- 单块转写（与本地唯一的差异） ------------------------------------

    def _transcribe_range(
        self,
        audio: Path,
        offset_seconds: float,
        span_seconds: float | None,
        *,
        chunk_index: int,
        transcript,
        ffmpeg,
    ) -> None:
        """把一段音频交给云端，并把时间戳平移回源时间轴（§7.3 同一条规则）。"""
        span = float(span_seconds) if span_seconds is not None else float(transcript.duration)
        try:
            upload = self._prepare_upload(Path(audio), span, ffmpeg, chunk_index=chunk_index)
        except ProviderError as exc:
            # 抽音失败同样算"块级失败"：必须说清是第几段，上层才能如实回退并说明
            self.last_chunk_failure = f"第 {chunk_index + 1} 段：{exc.describe()}"
            raise
        try:
            result: ProviderTranscript = self._transport.transcribe_file(
                upload, language=self.settings.language
            )
        except ProviderError as exc:
            # 记下"第几段失败"，再抛给上层决定整体回退（不做半云端半本地）
            self.last_chunk_failure = f"第 {chunk_index + 1} 段：{exc.describe()}"
            raise

        offset = Fraction(str(offset_seconds)).limit_denominator(1_000_000)
        for segment in result.segments:
            transcript.segments.append(
                AsrSegment(
                    start=_ms_to_frac(segment.start_ms) + offset,
                    end=_ms_to_frac(segment.end_ms) + offset,
                    text=segment.text,
                    chunk_index=chunk_index,
                )
            )

        words: list[ProviderWord] = list(result.words)
        if not words and result.segments:
            # 厂商没给词级时间戳：如实降级为"分句级"，不编造词边界（设计 §7）
            self._note_missing_word_timestamps(transcript)
            words = [
                ProviderWord(start_ms=s.start_ms, end_ms=s.end_ms, text=s.text)
                for s in result.segments
            ]
        for word in words:
            transcript.words.append(
                AsrWord(
                    start=_ms_to_frac(word.start_ms) + offset,
                    end=_ms_to_frac(word.end_ms) + offset,
                    text=word.text,
                    probability=word.confidence,
                    chunk_index=chunk_index,
                )
            )

    def _note_missing_word_timestamps(self, transcript) -> None:
        if self._warned_word_timestamps:
            return
        self._warned_word_timestamps = True
        transcript.issues.append(
            Issue(
                LEVEL_WARN,
                "ASR_NO_WORD_TIMESTAMPS",
                "该后端未提供词级时间戳，候选点质量降级。",
                "句子按厂商分句重建，句内不再有词级边界；"
                "候选切点仍可用，但精度不如词级时间戳。",
            )
        )

    # ---- 上传前的音频准备 -----------------------------------------------

    def _prepare_upload(
        self, audio: Path, span_seconds: float, ffmpeg, *, chunk_index: int
    ) -> Path:
        """保证交给厂商的是**纯音频**且尽量小。

        - 源片（本地路径直接把整片交给 Whisper）必须先抽音轨：云端不接受 mp4，
          也不该把画面传出去；
        - 抽出的块是 16k 单声道 wav（600 秒约 19MB），有 mp3 编码器就压到 ~5MB。
        """
        work = self._workdir()
        if self._source is not None and audio.resolve() == self._source:
            self._upload_seq += 1
            extracted = work / f"src{self._upload_seq:03d}.wav"
            if not extract_audio(ffmpeg, audio, extracted, 0.0, span_seconds):
                # 不能把一切抽音失败都归因成“源片没有音轨”：临时目录不可写、
                # ffmpeg 被拦截同样是常见原因。让用户看到真正可行动的细节。
                env_name = "TMP" if sys.platform == "win32" else "TMPDIR"
                raise ProviderError(
                    f"音轨提取失败：ffmpeg 没有产出音频文件。"
                    f"请确认源片含音轨，且临时目录可写（当前 {env_name}={os.environ.get(env_name, '')!r}）。"
                )
            audio = extracted
        return self._maybe_compress(audio, work, ffmpeg)

    def _maybe_compress(self, audio: Path, work: Path, ffmpeg) -> Path:
        if audio.suffix.lower() in (".mp3", ".ogg"):
            return audio
        if ffmpeg is None:
            from .ffmpeg import locate_ffmpeg

            ffmpeg = locate_ffmpeg().ffmpeg
        if self._has_mp3 is None:
            self._has_mp3 = _encoder_listed(ffmpeg, "libmp3lame")
        if not self._has_mp3:
            return audio  # 没有 mp3 编码器就老实上传 wav：19MB 仍在 100MB 上限内
        target = work / f"{audio.stem}.mp3"
        proc = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error",
                "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", _UPLOAD_BITRATE, "-y", str(target),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0 or not target.exists() or target.stat().st_size <= 1024:
            return audio
        return target

    def _workdir(self) -> Path:
        if self._work_dir is None:
            self._work_dir = Path(tempfile.mkdtemp(prefix="drama_cloud_asr_"))
        return self._work_dir

    def _cleanup(self) -> None:
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None


def select_asr_engine(
    config: AppConfig | None,
    settings: AsrSettings,
    *,
    cache: CacheStore | None = None,
    fingerprint: SourceFingerprint | None = None,
) -> tuple[AsrEngine, list[str]]:
    """按用户配置选转写后端，并把"为什么这样选"变成可展示的说明。

    默认返回本地引擎（行为与以前完全一致）；云端档案被选中且两个开关都打开时才
    换成 `CloudAsrEngine`。notes 必须原样进日志——"选了云端却没生效"的原因要能
    被用户看到（与语义判断同一条规矩）。
    """
    notes: list[str] = []
    profile = config.active_profile(CAP_ASR) if config is not None else None
    if profile is not None:
        reason = block_reason(profile, CAP_ASR)
        if reason is not None:
            notes.append(reason + "，本次仍用本地模型。")
        else:
            transport = build_asr_transport(profile)
            if transport is None:
                protocol = PROTO_LABELS.get(profile.asr_protocol, profile.asr_protocol)
                notes.append(
                    f"暂不支持「{protocol}」协议的云端转写：{profile.id}"
                    "（可在「模型管理 → 云端厂商」里改协议），本次仍用本地模型。"
                )
            else:
                engine = CloudAsrEngine(
                    transport, profile, settings=settings, cache=cache, fingerprint=fingerprint
                )
                notes.append(
                    f"转写后端：{engine.describe()}（{describe_egress(profile, (CAP_ASR,))}）"
                )
                return engine, notes
    return AsrEngine(settings, cache=cache, fingerprint=fingerprint), notes
