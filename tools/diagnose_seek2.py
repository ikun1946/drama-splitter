"""诊断 v2：用帧身份（而非帧数）判断哪种切帧命令真正正确。

基准真值用 `select='between(n,START,END-1)'` 按**解码帧号**选帧：
`n` 从文件第一帧起计数，恰好就是我们的时间轴帧号，语义上无歧义，
但必须从 0 解码，所以很慢——仅用于诊断，不用于产品。

对每个变体，从起点帧 0 与中间某帧各切 500 帧，读回条码并比对。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.core.ffmpeg import locate_ffmpeg  # noqa: E402
from verify import read_frame_indices  # noqa: E402

BIN = locate_ffmpeg()
OUT_DIR = ROOT / "testdata" / "_diag2"
OUT_DIR.mkdir(parents=True, exist_ok=True)
NO_WINDOW = 0x08000000

FPS = 25
N = 500  # 每集帧数
SPAN = N / FPS  # 20.0 秒


def encode_args() -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def variant_commands(src: Path, start_frame: int) -> dict[str, list[str]]:
    """返回 变体名 → 完整 ffmpeg 参数（不含 -y 输出）。"""
    timeline = start_frame / FPS          # 时间轴秒（相对视频首帧）
    # 源片起始时间 = 5 秒，故绝对时间戳 = timeline + 5
    absolute = timeline + 5.0
    commands: dict[str, list[str]] = {}

    commands["1_输入ss_相对_带t"] = [
        "-ss", f"{timeline:.6f}", "-i", str(src),
        "-map", "0:v:0", "-frames:v", str(N), "-t", f"{SPAN:.6f}",
        *encode_args(),
        "-avoid_negative_ts", "make_zero", "-muxpreload", "0", "-muxdelay", "0",
    ]

    commands["2_输入ss_相对_只frames"] = [
        "-ss", f"{timeline:.6f}", "-i", str(src),
        "-map", "0:v:0", "-frames:v", str(N),
        *encode_args(),
    ]

    commands["3_seek_timestamp_绝对_只frames"] = [
        "-ss", f"{absolute:.6f}", "-seek_timestamp", "1", "-i", str(src),
        "-map", "0:v:0", "-frames:v", str(N),
        *encode_args(),
    ]

    commands["4_seek_timestamp_绝对_带t"] = [
        "-ss", f"{absolute:.6f}", "-seek_timestamp", "1", "-i", str(src),
        "-map", "0:v:0", "-frames:v", str(N), "-t", f"{SPAN:.6f}",
        *encode_args(),
    ]

    commands["5_seek_timestamp_绝对_fastseek"] = [
        "-ss", f"{absolute:.6f}", "-seek_timestamp", "1", "-i", str(src),
        "-map", "0:v:0", "-frames:v", str(N), "-t", f"{SPAN:.6f}",
        "-noaccurate_seek",
        *encode_args(),
    ]

    commands["6_copyts_trim"] = [
        "-copyts", "-ss", f"{absolute:.6f}", "-seek_timestamp", "1", "-i", str(src),
        "-map", "0:v:0",
        "-vf", f"trim=start={absolute:.6f}:end={absolute + SPAN:.6f},setpts=PTS-STARTPTS",
        "-frames:v", str(N),
        *encode_args(),
        "-avoid_negative_ts", "make_zero",
    ]

    if start_frame == 0:
        commands["0_基准真值_select"] = [
            "-i", str(src),
            "-map", "0:v:0",
            "-vf", f"select='between(n\\,{start_frame}\\,{start_frame + N - 1})',setpts=N/FRAME_RATE/TB",
            "-frames:v", str(N),
            *encode_args(),
        ]

    return commands


def run_variant(name: str, args: list[str], target: Path, start_frame: int = 0) -> str:
    if target.exists():
        target.unlink()
    proc = subprocess.run(
        [str(BIN.ffmpeg), "-hide_banner", "-loglevel", "error", *args, "-y", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        return f"失败：{(proc.stderr or '').strip()[-200:]}"

    try:
        indices = read_frame_indices(target)
    except Exception as exc:  # noqa: BLE001
        return f"读取帧号失败：{exc}"

    expected = list(range(start_frame, start_frame + N))
    if indices == expected:
        return "正确"
    if not indices:
        return "无帧"
    first, last = indices[0], indices[-1]
    dup = len(indices) - len(set(indices))
    detail = f"首帧={first} 末帧={last} 帧数={len(indices)} 重复={dup}"
    if indices == list(range(first, first + len(indices))):
        offset = first - start_frame
        if offset == 0:
            return f"起点正确但少尾帧：{detail}"
        return f"整体偏移 {offset:+d} 帧：{detail}"
    return f"序列错乱：{detail}"


def main() -> int:
    src = ROOT / "testdata" / "nonzero_pts_120s.mp4"
    print(f"源片: {src.name}（容器起始 5.000s）")
    print(f"每集 {N} 帧 / {SPAN} 秒\n")

    for start_frame in (0, 750):
        print(f"=== 起点帧 {start_frame}（时间轴 {start_frame / FPS:.3f}s）===")
        for name, args in variant_commands(src, start_frame).items():
            target = OUT_DIR / f"f{start_frame}_{name}.mp4"
            verdict = run_variant(name, args, target, start_frame)
            print(f"  {name:34s} {verdict}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
