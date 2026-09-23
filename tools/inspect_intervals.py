"""快速查看某个视频文件在给定时间窗内的帧间隔分布。"""

from __future__ import annotations

import collections
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.ffmpeg import locate_ffmpeg  # noqa: E402


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "testdata" / "vfr_120s.mp4")
    window = sys.argv[2] if len(sys.argv) > 2 else "0%+8"
    b = locate_ffmpeg()
    cmd = [
        str(b.ffprobe),
        "-hide_banner",
        "-loglevel", "error",
        "-select_streams", "v:0",
        "-read_intervals", window,
        "-show_entries", "frame=pts_time",
        "-print_format", "json",
        target,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("退出码:", proc.returncode)
    if proc.stderr.strip():
        print("stderr:", proc.stderr.strip()[:400])
    if not proc.stdout.strip():
        print("stdout 为空")
        return 1

    import json

    frames = json.loads(proc.stdout).get("frames") or []
    pts = []
    for frame in frames:
        value = frame.get("pts_time")
        if value not in (None, "", "N/A"):
            pts.append(Fraction(value))
    print(f"窗口 {window} 内帧数: {len(pts)}")
    if len(pts) < 2:
        return 0
    deltas = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    counter = collections.Counter(deltas)
    print("帧间隔分布（毫秒 → 次数）:")
    for key, count in sorted(counter.items(), key=lambda kv: float(kv[0])):
        print(f"  {float(key) * 1000:8.2f}ms → {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
