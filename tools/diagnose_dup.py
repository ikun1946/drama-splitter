"""诊断 v3：定位"首帧被复制一次"的成因。

线索：只切视频时帧序列干净；一旦 `-map` 上音轨，输出就变成 [0,0,1,2,…]。
本脚本固定切点，只切换音视频组合与时间戳处理选项，逐一定位。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.core.ffmpeg import locate_ffmpeg  # noqa: E402
from verify import measure_av_sync, read_frame_indices  # noqa: E402

BIN = locate_ffmpeg()
OUT = ROOT / "testdata" / "_diag3"
OUT.mkdir(parents=True, exist_ok=True)
NO_WINDOW = 0x08000000

N = 750
SPAN = "30.000000"
TIMELINE = "0.000000"


def enc() -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def audio_filter() -> list[str]:
    return [
        "-af",
        f"asetpts=PTS-STARTPTS,atrim=end={SPAN},asetpts=PTS-STARTPTS",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
    ]


def variants(src: Path, seek: str) -> dict[str, list[str]]:
    base = ["-ss", seek, "-i", str(src)]
    v: dict[str, list[str]] = {}

    v["a_只视频"] = [*base, "-map", "0:v:0", "-frames:v", str(N), *enc()]

    v["b_视频+音频_用t截断"] = [
        *base, "-map", "0:v:0", "-map", "0:a:0",
        "-frames:v", str(N), "-t", SPAN, "-c:a", "aac", *enc(),
    ]

    v["c_视频+音频_af_无附加选项"] = [
        *base, "-map", "0:v:0", "-map", "0:a:0",
        "-frames:v", str(N), *audio_filter(), *enc(),
    ]

    v["d_视频+音频_af_仅avoid_negative"] = [
        *base, "-map", "0:v:0", "-map", "0:a:0",
        "-frames:v", str(N), *audio_filter(), *enc(),
        "-avoid_negative_ts", "make_zero",
    ]

    v["e_视频+音频_af_含muxpreload"] = [
        *base, "-map", "0:v:0", "-map", "0:a:0",
        "-frames:v", str(N), *audio_filter(), *enc(),
        "-avoid_negative_ts", "make_zero", "-muxpreload", "0", "-muxdelay", "0",
    ]

    v["f_视频+音频_af_全选项"] = [
        *base, "-map", "0:v:0", "-map", "0:a:0",
        "-frames:v", str(N), *audio_filter(), *enc(),
        "-avoid_negative_ts", "make_zero", "-muxpreload", "0", "-muxdelay", "0",
        "-movflags", "+faststart",
    ]

    v["g_音频先map"] = [
        *base, "-map", "0:a:0", "-map", "0:v:0",
        "-frames:v", str(N), *audio_filter(), *enc(),
    ]

    return v


def count_frames(path: Path) -> int | None:
    cmd = [
        str(BIN.ffprobe), "-hide_banner", "-loglevel", "error", "-print_format", "json",
        "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    import json

    raw = ((json.loads(r.stdout or "{}").get("streams") or [{}])[0]).get("nb_read_frames")
    return int(raw) if raw not in (None, "", "N/A") else None


def head_pts(path: Path, limit: int = 4) -> list[str]:
    cmd = [
        str(BIN.ffprobe), "-hide_banner", "-loglevel", "error", "-print_format", "json",
        "-select_streams", "v:0", "-show_entries", "frame=pts_time", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    import json

    frames = json.loads(r.stdout or "{}").get("frames") or []
    return [str(f.get("pts_time")) for f in frames[:limit]]


def run(name: str, args: list[str], target: Path) -> None:
    if target.exists():
        target.unlink()
    proc = subprocess.run(
        [str(BIN.ffmpeg), "-hide_banner", "-loglevel", "error", *args, "-y", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        print(f"  {name:34s} 失败 {(proc.stderr or '').strip()[-150:]}")
        return
    real = count_frames(target)
    try:
        indices = read_frame_indices(target)
        seq_ok = indices == list(range(indices[0], indices[0] + len(indices)))
        head = (
            f"条码 {indices[0]}..{indices[-1]} 共{len(indices)}"
            f"{' 连续' if seq_ok else ' **错乱**'}"
        )
    except Exception as exc:  # noqa: BLE001
        head = f"读条码失败 {exc}"

    sync = "无音频"
    try:
        measurement = measure_av_sync(target, OUT / "_sync_tmp")
        if measurement.offsets:
            sync = (
                f"音画偏差最大 {measurement.max_abs_offset_seconds * 1000:5.1f}ms"
                f"（{measurement.beep_count} 个标记）"
            )
    except Exception as exc:  # noqa: BLE001
        sync = f"同步测量失败 {exc}"

    head_ok = "OK " if real == N else "!! "
    print(f"  {name:30s} {head_ok}ffprobe={real} {head:34s} {sync}")


def main() -> int:
    cfr = ROOT / "testdata" / "cfr_120s_25fps.mp4"
    print("=== 基准素材（容器起始 0）===")
    for name, args in variants(cfr, TIMELINE).items():
        run(name, args, OUT / f"cfr_{name}.mp4")

    nonzero = ROOT / "testdata" / "nonzero_pts_120s.mp4"
    print("\n=== 非零起始 PTS 素材（容器起始 5，视频起始 5.021333）===")
    for name, args in variants(nonzero, TIMELINE).items():
        run(name, args, OUT / f"nz_{name}.mp4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
