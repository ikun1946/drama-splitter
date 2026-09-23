"""诊断 v4：非零起始 PTS 素材上第二集为何跑到片尾。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.core.export import ExportPreset, Exporter  # noqa: E402
from app.core.ffmpeg import locate_ffmpeg  # noqa: E402
from app.core.plan import BoundaryPlan  # noqa: E402
from app.core.probe import probe_media  # noqa: E402
from verify import read_frame_indices  # noqa: E402

OUT = ROOT / "testdata" / "_diag4"
OUT.mkdir(parents=True, exist_ok=True)
NO_WINDOW = 0x08000000


def count_frames(path: Path) -> int | None:
    import json

    b = locate_ffmpeg()
    cmd = [
        str(b.ffprobe), "-hide_banner", "-loglevel", "error", "-print_format", "json",
        "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    raw = ((json.loads(r.stdout or "{}").get("streams") or [{}])[0]).get("nb_read_frames")
    return int(raw) if raw not in (None, "", "N/A") else None


def main() -> int:
    binaries = locate_ffmpeg()
    media = probe_media(binaries, ROOT / "testdata" / "nonzero_pts_120s.mp4")
    print(f"时间基准={media.video_time_base} 总时长={float(media.timeline_duration)}s "
          f"帧数={media.video.nb_frames}")
    print(f"视频起始={float(media.video_start_time)} 容器起始={float(media.format_start_time or 0)} "
          f"seek_offset={float(media.seek_offset_seconds)*1000:.3f}ms")

    boundaries = [0, 500, 1000]
    seconds = [media.frame_start_seconds(i) for i in boundaries]
    plan = BoundaryPlan.from_seconds(seconds, media.video_time_base, media.duration_ticks())
    plan.snap_to_frames(media)
    print(f"边界ticks={plan.boundary_ticks}")
    print(f"边界秒={[float(s) for s in (plan.time_base.ticks_to_seconds(t) for t in plan.boundary_ticks)]}")
    print(f"计划帧数={plan.frame_counts(media)}")

    exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
    jobs = exporter.prepare_jobs(plan, OUT, audio_stream_index=media.audio_tracks[0].index)
    for job in jobs:
        print(f"\n--- 第{job.episode.index:02d}集 期望帧数={job.frame_count} ---")
        cmd = exporter.build_command(job, OUT / f"tmp{job.episode.index}.mp4")
        print("  " + " ".join(str(c) for c in cmd[cmd.index("-ss"):]))

        target = OUT / f"ep{job.episode.index:02d}.mp4"
        if target.exists():
            target.unlink()
        result = exporter.export_episode(job)
        print(f"  结果: success={result.success} msg={result.message}")
        if result.output_path:
            print(f"  ffprobe真实帧数={count_frames(result.output_path)}")
            indices = read_frame_indices(result.output_path)
            print(f"  条码首尾={indices[0]}..{indices[-1]} 共{len(indices)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
