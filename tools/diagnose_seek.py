"""诊断：非零起始 PTS 容器上 `-ss` 输入定位的帧产出行为。

对同一切点尝试多种命令变体，输出各自的前几帧 PTS 与帧数，
据此判断哪一种能保证"首帧不重复、帧数精确"。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.ffmpeg import locate_ffmpeg  # noqa: E402

BIN = locate_ffmpeg()
SRC = ROOT / "testdata" / "nonzero_pts_120s.mp4"
OUT_DIR = ROOT / "testdata" / "_diag"
OUT_DIR.mkdir(parents=True, exist_ok=True)

NO_WINDOW = 0x08000000


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [str(BIN.ffmpeg), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    return proc.returncode, (proc.stderr or "").strip()[-400:]


def frames_info(path: Path, limit: int = 6) -> tuple[int | None, list[str]]:
    cmd = [
        str(BIN.ffprobe),
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=pts,pts_time,n",
        "-show_entries",
        "stream=nb_frames,start_time,time_base",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    import json

    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    count = stream.get("nb_frames")
    if count in (None, "", "N/A"):
        count = None
        try:
            count = int(count) if count else None
        except ValueError:
            count = None
    head = [f"pts={f.get('pts')} t={f.get('pts_time')}" for f in (data.get("frames") or [])[:limit]]
    print(f"    容器起始时间 start_time={stream.get('start_time')}  time_base={stream.get('time_base')}")
    print(f"    头部帧数声明 nb_frames={count}")
    for line in head:
        print(f"      {line}")
    return count, head


def count_real(path: Path) -> int | None:
    cmd = [
        str(BIN.ffprobe),
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    import json

    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    raw = stream.get("nb_read_frames")
    return int(raw) if raw not in (None, "", "N/A") else None


VARIANTS: dict[str, list[str]] = {
    "A_当前实现": [
        "-ss", "0.000000", "-i", str(SRC),
        "-map", "0:v:0", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-avoid_negative_ts", "make_zero", "-muxpreload", "0", "-muxdelay", "0",
    ],
    "B_去掉avoid_negative_ts": [
        "-ss", "0.000000", "-i", str(SRC),
        "-map", "0:v:0", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    ],
    "C_副本归零时间戳": [
        "-ss", "0.000000", "-i", str(SRC),
        "-map", "0:v:0", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-fflags", "+genpts",
    ],
    "D_seek_timestamp绝对定位": [
        "-ss", "5.000000", "-seek_timestamp", "1", "-i", str(SRC),
        "-map", "0:v:0", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-avoid_negative_ts", "make_zero",
    ],
    "E_输出侧ss": [
        "-i", str(SRC),
        "-map", "0:v:0", "-ss", "0.000000", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    ],
    "F_输入ss加reset_timestamps": [
        "-ss", "0.000000", "-i", str(SRC),
        "-map", "0:v:0", "-frames:v", "500", "-t", "20.000000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-reset_timestamps", "1", "-avoid_negative_ts", "make_zero",
    ],
}


def main() -> int:
    print(f"源文件: {SRC.name}")
    print("源文件容器信息：")
    frames_info(SRC)
    print(f"源文件真实帧数（解码计数）: {count_real(SRC)}")
    print(f"期望：切 500 帧，首帧时间戳归零且不重复\n")

    for name, args in VARIANTS.items():
        target = OUT_DIR / f"{name}.mp4"
        code, err = run([*args, "-y", str(target)])
        if code != 0:
            print(f"{name}: 失败 {err}")
            continue
        real = count_real(target)
        print(f"{name}:")
        frames_info(target)
        print(f"    真实帧数={real}  {'OK' if real == 500 else '<<< 不符'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
