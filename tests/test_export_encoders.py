"""导出编码方式：NVENC 命令、关键帧吸附、零损失快速导出链路。

背景（用户诉求）：导出时 CPU 满载、耗时长。两条提效路线：
1) GPU 硬件编码（NVENC）——实测同保真度下快 5 倍以上；
2) 零损失快速复制（``-c copy``）——不重编码、逐帧原样，但切点必须在关键帧上，
   因此配套"吸附到最近关键帧"的前置步骤（本文件盯住它的正确性）。
"""

from __future__ import annotations

from fractions import Fraction

from app.core.export import (
    CutMode,
    ExportPreset,
    Exporter,
    nvenc_available,
    snap_plan_to_keyframes,
)
from app.core.plan import BoundaryPlan
from app.core.probe import probe_keyframe_frames, probe_media


def make_plan(media, cuts_seconds):
    """用秒数构造方案（与 test_phase5 同一写法）。"""
    tb = media.video_time_base
    ticks = [0] + [tb.seconds_to_ticks(Fraction(str(c))) for c in cuts_seconds]
    ticks.append(media.duration_ticks())
    return BoundaryPlan(boundary_ticks=ticks, time_base=tb, total_ticks=media.duration_ticks())


class TestNvencCommand:
    def test_nvenc_command_uses_gpu_parameters(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30, 60, 90])
        preset = ExportPreset(video_codec="h264_nvenc", crf=18)
        exporter = Exporter(binaries, media, preset, max_parallel=1)
        jobs = exporter.prepare_jobs(plan, tmp_path, audio_stream_index=1)
        text = " ".join(str(c) for c in exporter.build_command(jobs[0], tmp_path / "x.mp4"))
        assert "h264_nvenc" in text
        assert "-preset p7" in text and "-tune hq" in text
        assert "-rc vbr" in text and "-cq 18" in text and "-b:v 0" in text
        assert "libx264" not in text

    def test_x264_command_unchanged(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30, 60, 90])
        exporter = Exporter(binaries, media, ExportPreset(), max_parallel=1)
        jobs = exporter.prepare_jobs(plan, tmp_path, audio_stream_index=1)
        text = " ".join(str(c) for c in exporter.build_command(jobs[0], tmp_path / "x.mp4"))
        assert "libx264" in text and "-crf 18" in text
        assert "nvenc" not in text

    def test_describe_marks_gpu(self):
        assert "GPU" in ExportPreset(video_codec="h264_nvenc").describe()

    def test_nvenc_probe_never_raises(self, binaries):
        """探测必须给出布尔结论（本机是否有 N 卡都要能跑）。"""
        assert nvenc_available(binaries) in (True, False)


class TestFastCopyAudioMapping:
    def test_track_index_is_mapped_to_ordinal(self, binaries, assets, tmp_path):
        """容器流索引（真实素材常为 1）必须映射成 ``0:a:0``。

        曾经直接把索引当序号用（``0:a:1``）——合成素材传 0 侥幸能跑，
        真实素材（音轨流索引 1）整个导出失败：Stream map matches no streams。
        """
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30])
        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY, max_parallel=1
        )
        jobs = exporter.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        text = " ".join(str(c) for c in exporter.build_command(jobs[0], tmp_path / "x.mp4"))
        assert "0:a:0" in text

    def test_ffmpeg_failure_is_not_reported_as_cancelled(
        self, binaries, assets, tmp_path, monkeypatch
    ):
        """FfmpegError 是 RuntimeError 的子类——except 顺序错了会把一切失败
        误报成「已取消」（真实踩过：导出失败却提示已取消，无从排查）。"""
        from app.core import export as ex

        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30])
        exporter = Exporter(binaries, media, ExportPreset(), max_parallel=1)
        jobs = exporter.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )

        def boom(*args, **kwargs):
            raise ex.FfmpegError("模拟编码失败", -22, "boom")

        monkeypatch.setattr(ex, "run_ffmpeg", boom)
        result = exporter.export_episode(jobs[0])
        assert not result.success
        assert result.message != "已取消"
        assert "模拟编码失败" in result.message


class TestKeyframeSnap:
    """合成素材关键帧每 250 帧（10 秒）一个：0 / 250 / 500 / 750 …"""

    def test_snaps_internal_boundaries_to_nearest_keyframe(self, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        assert keyframes[:4] == [0, 250, 500, 750]

        plan = make_plan(media, [28, 60, 90])    # 700 / 1500 / 2250 帧
        new_plan, snaps, problems = snap_plan_to_keyframes(plan, media, keyframes)
        assert problems == ()
        by_index = {s.index: s for s in snaps}
        assert by_index[1].moved, "700 帧应吸附到 750"
        assert by_index[1].snapped_seconds == Fraction(30)
        assert by_index[1].offset_seconds == Fraction(2)
        assert not by_index[2].moved and not by_index[3].moved

        # 首尾边界永不动
        assert new_plan.boundary_ticks[0] == plan.boundary_ticks[0]
        assert new_plan.boundary_ticks[-1] == plan.boundary_ticks[-1]
        # 每个内部边界都落在关键帧上
        keyframe_set = set(keyframes)
        for ticks in new_plan.boundary_ticks[1:-1]:
            frame = media.frame_index_at_or_after(new_plan.time_base.ticks_to_seconds(ticks))
            assert frame in keyframe_set

    def test_frame_conservation_after_snap(self, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        plan = make_plan(media, [28, 60, 90])
        new_plan, _, _ = snap_plan_to_keyframes(plan, media, keyframes)
        counts = new_plan.frame_counts(media)
        assert sum(counts) == media.video.nb_frames
        assert all(c > 0 for c in counts)

    def test_single_episode_is_noop(self, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        plan = make_plan(media, [])
        new_plan, snaps, problems = snap_plan_to_keyframes(plan, media, keyframes)
        assert new_plan is plan
        assert snaps == () and problems == ()

    def test_end_to_end_fast_copy_after_snap(self, binaries, assets, tmp_path):
        """吸附后的方案必须能真的以流复制导出：全成功且帧数守恒。"""
        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        plan = make_plan(media, [28, 60, 90])
        new_plan, _, problems = snap_plan_to_keyframes(plan, media, keyframes)
        assert problems == ()

        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY, max_parallel=1
        )
        batch = exporter.export_plan(
            new_plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        assert not batch.plan_layer_problems, batch.plan_layer_problems
        assert len(batch.results) == 4
        assert all(r.success for r in batch.results), [r.message for r in batch.results]
        total = sum(r.actual_frame_count or 0 for r in batch.results)
        assert total == media.video.nb_frames
