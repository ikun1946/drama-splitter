"""合成中文语音测试素材（带转写标准答案）。

为什么需要它
------------
真实短剧素材不可得时，ASR 正确性无法评判——"转出来一段文字"看起来永远是对的。
本脚本用系统自带的中文 SAPI 引擎（Microsoft Huihui Desktop）合成**已知文本**的
语音，并记录每句话在时间轴上的精确起止，于是可以量化：

- 字符错误率（CER）：转写文本 vs 标准答案
- 时间戳误差：ASR 给出的句段边界 vs 真实边界
- 分块转写的偏移正确性（§7.3 分块须加回块起点偏移）

素材结构刻意贴近短剧形态：短句、句间停顿短（模拟抢话与紧凑对白）。

用法：
    python tools/make_speech_asset.py [输出目录]
    python tools/make_speech_asset.py --voice 0 --gap 0.6
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 贴近"现代都市短剧"的短句对白。刻意包含疑问句、否定句与短促回应，
# 因为这三类最容易在断句上出错。
SCRIPT_LINES = [
    "这房子你今天必须交出来。",
    "如果我不交呢？",
    "你欠的钱，白纸黑字写得清清楚楚。",
    "那笔钱不是我借的。",
    "不是你借的，那是谁签的字？",
    "字是我签的，可钱我没拿。",
    "你觉得我会信吗？",
    "信不信由你，反正我问心无愧。",
    "好，那我们就法庭上见。",
    "随便你。",
]

TARGET_SAMPLE_RATE = 16000


@dataclass
class SpeechLine:
    index: int
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def list_voices() -> list[str]:
    import comtypes.client as cc

    voices = cc.CreateObject("SAPI.SpVoice").GetVoices()
    return [voices.Item(i).GetDescription() for i in range(voices.Count)]


def synth_line(text: str, voice_index: int, target: Path) -> bool:
    """用 SAPI 合成一句到 WAV。"""
    import comtypes.client as cc

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    voice = cc.CreateObject("SAPI.SpVoice")
    stream = cc.CreateObject("SAPI.SpFileStream")
    try:
        # 3 = SSFMCreateForWrite
        stream.Open(str(target), 3, False)
        voice.AudioOutputStream = stream
        voice.Voice = voice.GetVoices().Item(voice_index)
        # 稍降语速，更接近正常对白而非播报
        voice.Rate = -1
        voice.Speak(text)
    finally:
        try:
            stream.Close()
        except Exception:  # noqa: BLE001 - COM 清理失败不应掩盖主流程结果
            pass

    return target.exists() and target.stat().st_size > 1024


def to_16k_mono(ffmpeg: str, source: Path, target: Path) -> bool:
    """统一重采样为 16kHz 单声道 s16le WAV。

    不依赖 SAPI 的输出格式（其可选格式枚举因系统而异），统一转一次更可靠。
    """
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-y",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        print(f"    重采样失败：{(proc.stderr or '').strip()[-200:]}")
        return False
    return True


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"仅支持 16 位 PCM，实际 {width * 8} 位")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def build(
    out_dir: Path,
    *,
    voice_index: int,
    gap_seconds: float,
    lead_seconds: float,
) -> list[SpeechLine]:
    from app.core.ffmpeg import locate_ffmpeg

    ffmpeg = str(locate_ffmpeg().ffmpeg)
    work = out_dir / "_lines"
    work.mkdir(parents=True, exist_ok=True)

    pieces: list[np.ndarray] = []
    lines: list[SpeechLine] = []
    cursor = 0.0

    lead = np.zeros(int(lead_seconds * TARGET_SAMPLE_RATE), dtype=np.float32)
    pieces.append(lead)
    cursor += lead_seconds

    for index, text in enumerate(SCRIPT_LINES, start=1):
        raw_wav = work / f"line{index:02d}_raw.wav"
        norm_wav = work / f"line{index:02d}.wav"
        print(f"  [{index:02d}] 合成：{text}", flush=True)
        if not synth_line(text, voice_index, raw_wav):
            raise RuntimeError(f"SAPI 合成失败：{text}")
        if not to_16k_mono(ffmpeg, raw_wav, norm_wav):
            raise RuntimeError(f"重采样失败：{text}")

        audio, rate = read_wav_mono(norm_wav)
        if rate != TARGET_SAMPLE_RATE:
            raise RuntimeError(f"采样率不是 {TARGET_SAMPLE_RATE}：{rate}")

        duration = len(audio) / rate
        lines.append(
            SpeechLine(index=index, text=text, start=cursor, end=cursor + duration)
        )
        pieces.append(audio)
        cursor += duration

        # 句间停顿：短停顿让分块边界更容易落在句子中间，测试更有意义
        if index < len(SCRIPT_LINES):
            silence = np.zeros(int(gap_seconds * TARGET_SAMPLE_RATE), dtype=np.float32)
            pieces.append(silence)
            cursor += gap_seconds

    full = np.concatenate(pieces)
    speech_wav = out_dir / "speech_16k.wav"
    write_wav_mono(speech_wav, full, TARGET_SAMPLE_RATE)

    payload = {
        "sample_rate": TARGET_SAMPLE_RATE,
        "total_seconds": round(len(full) / TARGET_SAMPLE_RATE, 6),
        "voice_index": voice_index,
        "gap_seconds": gap_seconds,
        "lead_seconds": lead_seconds,
        "lines": [
            {
                "index": line.index,
                "text": line.text,
                "start": round(line.start, 6),
                "end": round(line.end, 6),
            }
            for line in lines
        ],
        "note": "标准答案由合成过程产生，时间边界即真实边界；用于量化 ASR 的 CER 与时间戳误差",
    }
    (out_dir / "speech_ground_truth.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 清理逐句中间产物
    for path in work.glob("*"):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        work.rmdir()
    except OSError:
        pass

    return lines


def write_wav_mono(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((clipped * 32767.0).astype("<i2").tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="合成中文语音测试素材（带转写标准答案）")
    parser.add_argument("output_dir", nargs="?", default=str(ROOT / "testdata"))
    parser.add_argument("--voice", type=int, default=0, help="SAPI 语音索引（0 通常为中文女声）")
    parser.add_argument("--gap", type=float, default=0.6, help="句间静音秒数")
    parser.add_argument("--lead", type=float, default=1.0, help="开头静音秒数")
    parser.add_argument("--list-voices", action="store_true", help="列出可用语音引擎")
    args = parser.parse_args()

    if args.list_voices:
        for index, name in enumerate(list_voices()):
            marker = "  ← 中文" if "Chinese" in name else ""
            print(f"  [{index}] {name}{marker}")
        return 0

    out_dir = Path(args.output_dir)
    print(f"用 SAPI 语音索引 {args.voice} 合成 {len(SCRIPT_LINES)} 句中文对白…")
    lines = build(out_dir, voice_index=args.voice, gap_seconds=args.gap,
                  lead_seconds=args.lead)

    print()
    print(f"完成：{out_dir / 'speech_16k.wav'}")
    for line in lines:
        print(f"  {line.start:7.3f} – {line.end:7.3f}  {line.text}")
    total = lines[-1].end
    print(f"  总时长 {total:.3f}s，标准答案写入 speech_ground_truth.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
