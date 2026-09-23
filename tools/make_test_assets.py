"""生成合成测试素材（方案 §20.2）。

产出的每个素材都带**帧号条码**与**已知时间点的声音标记**，因此可以自动校验：
- 导出后每集的首帧/末帧是否是源片里应有的那一帧（帧身份，而非像素哈希）；
- 切点附近的音画同步误差是否在一个视频帧以内。

素材清单（对应 §20.2 要求）：
    cfr_120s_25fps.mp4   基准：固定帧率、含音轨、非零可寻址
    noaudio_120s.mp4     无音轨变体
    multiaudio_120s.mp4  双音轨变体（第二轨为静音）
    nonzero_pts_120s.mp4 非零起始 PTS 变体
    vfr_120s.mp4         可变帧率变体（用于验证被正确识别并拦截）

用法：
    python tools/make_test_assets.py [输出目录] [--seconds 120]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markers import (  # noqa: E402
    BACKGROUND_GRAY,
    BEEP_AMPLITUDE,
    BEEP_DURATION_SECONDS,
    BEEP_FREQUENCY_HZ,
    BEEP_INTERVAL_FRAMES,
    BLOCK_SIZE,
    FLASH_INTERVAL_FRAMES,
    FPS,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    SAMPLE_RATE,
    encode_marker_bits,
    marker_block_positions,
)

ROOT = Path(__file__).resolve().parent.parent


def ffmpeg_path() -> str:
    sys.path.insert(0, str(ROOT))
    from app.core.ffmpeg import locate_ffmpeg

    return str(locate_ffmpeg().ffmpeg)


def iter_frame_batches(frame_count: int, batch: int = 120):
    """按批次产出 RGB24 字节流。

    每帧内容：
    - 灰色背景（每 FLASH_INTERVAL_FRAMES 帧底部带一条白条，便于目视找跳帧）；
    - 顶部 14 个黑白块编码帧号（MSB 在左）；
    - 中下部一根随帧号匀速右移的白条，提供视觉连续性。
    """
    positions = marker_block_positions()
    height, width = FRAME_HEIGHT, FRAME_WIDTH

    for start in range(0, frame_count, batch):
        end = min(frame_count, start + batch)
        block = np.full((end - start, height, width, 3), BACKGROUND_GRAY, dtype=np.uint8)

        for offset in range(end - start):
            index = start + offset
            frame = block[offset]
            bits = encode_marker_bits(index)
            for bit_index, bit in enumerate(bits):
                x0, y0 = positions[bit_index]
                frame[y0 : y0 + BLOCK_SIZE, x0 : x0 + BLOCK_SIZE] = (
                    255 if bit else 0
                )
            if index % FLASH_INTERVAL_FRAMES == 0:
                frame[height - 6 : height, :] = 255
            bar_x = int(index * (width - 20) / max(1, frame_count))
            frame[60:150, bar_x : bar_x + 20] = 255

        yield block.tobytes()


def write_video(ffmpeg: str, frame_count: int, target: Path) -> None:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(target),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for batch_bytes in iter_frame_batches(frame_count):
            proc.stdin.write(batch_bytes)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"视频生成失败：{stderr.strip()[-800:]}")


def render_audio(frame_count: int) -> np.ndarray:
    """渲染单声道音频：静音 + 每 5.000 秒帧起点处一个短促 1kHz 提示音。"""
    # 25fps 下每帧恰好 1920 个采样点，因此 125 帧 = 240000 采样点 = 精确 5 秒
    samples_per_frame = SAMPLE_RATE // FPS
    total_samples = frame_count * samples_per_frame
    audio = np.zeros(total_samples, dtype=np.float32)

    beep_len = int(BEEP_DURATION_SECONDS * SAMPLE_RATE)
    ramp = max(1, int(0.005 * SAMPLE_RATE))
    time_axis = np.arange(beep_len, dtype=np.float64) / SAMPLE_RATE
    tone = np.sin(2 * np.pi * BEEP_FREQUENCY_HZ * time_axis) * BEEP_AMPLITUDE
    envelope = np.ones(beep_len, dtype=np.float64)
    envelope[:ramp] = np.linspace(0.0, 1.0, ramp)
    envelope[-ramp:] = np.linspace(1.0, 0.0, ramp)
    tone = (tone * envelope).astype(np.float32)

    interval_samples = BEEP_INTERVAL_FRAMES * samples_per_frame
    beep_index = 0
    while True:
        start = beep_index * interval_samples
        if start + beep_len > total_samples:
            break
        audio[start : start + beep_len] += tone
        beep_index += 1

    return np.clip(audio, -1.0, 1.0)


def write_audio(ffmpeg: str, frame_count: int, target: Path) -> None:
    audio = render_audio(frame_count)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "-",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(target),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    proc.stdin.write((audio * 32767.0).astype(np.int16).tobytes())
    proc.stdin.close()
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"音频生成失败：{stderr.strip()[-800:]}")


def run(ffmpeg: str, args: list[str], what: str) -> None:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{what} 失败：{(proc.stderr or '').strip()[-800:]}")


def build(out_dir: Path, seconds: float) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path()

    frame_count = int(round(seconds * FPS))
    print(f"[1/6] 渲染视频 {frame_count} 帧（{seconds}s @ {FPS}fps）…", flush=True)
    video_only = out_dir / "_video_only.mp4"
    write_video(ffmpeg, frame_count, video_only)

    print("[2/6] 渲染音频标记…", flush=True)
    audio_wav = out_dir / "_audio.wav"
    write_audio(ffmpeg, frame_count, audio_wav)

    assets: dict[str, Path] = {}

    print("[3/6] 合成基准素材（CFR + 单音轨）…", flush=True)
    base = out_dir / "cfr_120s_25fps.mp4"
    run(
        ffmpeg,
        [
            "-i",
            str(video_only),
            "-i",
            str(audio_wav),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-y",
            str(base),
        ],
        "合成基准素材",
    )
    assets["cfr"] = base

    print("[4/6] 生成无音轨变体…", flush=True)
    noaudio = out_dir / "noaudio_120s.mp4"
    run(ffmpeg, ["-i", str(base), "-an", "-c:v", "copy", "-y", str(noaudio)], "生成无音轨变体")
    assets["no_audio"] = noaudio

    print("[5/6] 生成双音轨与非零起始 PTS 变体…", flush=True)
    multiaudio = out_dir / "multiaudio_120s.mp4"
    run(
        ffmpeg,
        [
            "-i",
            str(base),
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-map",
            "0",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-t",
            str(Fraction(frame_count, FPS)),
            "-y",
            str(multiaudio),
        ],
        "生成双音轨变体",
    )
    assets["multi_audio"] = multiaudio

    nonzero = out_dir / "nonzero_pts_120s.mp4"
    run(
        ffmpeg,
        [
            "-i",
            str(base),
            "-c",
            "copy",
            "-output_ts_offset",
            "5",
            "-avoid_negative_ts",
            "disabled",
            "-y",
            str(nonzero),
        ],
        "生成非零起始 PTS 变体",
    )
    assets["nonzero_pts"] = nonzero

    print("[6/6] 生成 VFR 变体（非均匀丢弃帧造成不等间隔）…", flush=True)
    vfr = out_dir / "vfr_120s.mp4"
    # 注意：`select='not(mod(n,7))'` 这类**均匀**抽帧得到的是低帧率 CFR，
    # 不是 VFR——帧间隔恒定，探测不会（也不该）把它判为 VFR。
    # 必须用互相错开的周期（37 与 53 互质）制造不等间隔：绝大多数间隔是
    # 40ms，遇到被丢弃处变成 80ms。同时 r_frame_rate 与 avg_frame_rate 会分离。
    run(
        ffmpeg,
        [
            "-i",
            str(video_only),
            "-vf",
            "select='gt(mod(n\\,37)*mod(n\\,53)\\,0)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-an",
            "-y",
            str(vfr),
        ],
        "生成 VFR 变体",
    )
    assets["vfr"] = vfr

    for temp in (video_only, audio_wav):
        try:
            temp.unlink()
        except OSError:
            pass

    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description="生成分集器合成测试素材")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=str(ROOT / "testdata"),
        help="输出目录，默认 <项目>/testdata",
    )
    parser.add_argument("--seconds", type=float, default=120.0, help="基准素材时长（秒）")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    assets = build(out_dir, args.seconds)

    print("\n生成完成：")
    for name, path in assets.items():
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {name:12s} {path.name:28s} {size_mb:7.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
