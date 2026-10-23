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

    def test_fast_copy_keeps_source_stream_parameters(
        self, binaries, assets, tmp_path
    ):
        """原生编码必须保持视频/音频编码、音轨参数不变，且命令里没有编码器。"""
        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        plan = make_plan(media, [28, 60])
        snapped, _, problems = snap_plan_to_keyframes(plan, media, keyframes)
        assert problems == ()

        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY
        )
        jobs = exporter.prepare_jobs(
            snapped, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        command = exporter.build_command(jobs[0], tmp_path / "native.mp4")
        text = " ".join(command)
        assert "-c copy" in text
        assert "-c:v" not in text and "-c:a" not in text
        assert "-crf" not in text and "-b:a" not in text

        batch = exporter.export_plan(
            snapped, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        assert batch.all_succeeded, batch.summary()
        for result in batch.succeeded:
            source = probe_media(binaries, result.output_path)
            assert source.video.codec_name == media.video.codec_name
            assert source.video.width == media.video.width
            assert source.video.height == media.video.height
            assert source.audio_tracks[0].codec_name == media.audio_tracks[0].codec_name
            assert source.audio_tracks[0].sample_rate == media.audio_tracks[0].sample_rate
            assert source.audio_tracks[0].channels == media.audio_tracks[0].channels

    def test_fast_copy_preserves_video_packet_payloads(
        self, binaries, assets, tmp_path
    ):
        """视频包内容逐字节一致：原生编码没有编码器参与。"""
        import subprocess

        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        plan = make_plan(media, [28, 60])
        snapped, _, _ = snap_plan_to_keyframes(plan, media, keyframes)
        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY
        )
        batch = exporter.export_plan(
            snapped, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        assert batch.all_succeeded, batch.summary()

        def video_packet_hashes(path, start=None, end=None):
            select = []
            if start is not None:
                select = ["-read_intervals", f"{start}%{end}"]
            proc = subprocess.run(
                [
                    str(binaries.ffprobe),
                    "-v", "error",
                    "-select_streams", "v:0",
                    *select,
                    "-show_entries", "packet=pts,size,flags,data_hash",
                    "-show_data_hash", "sha256",
                    "-of", "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            return [line for line in proc.stdout.splitlines() if line.strip()]

        # 第二集切点已吸附到关键帧。源片对应区间按 pts 过滤后，比较
        # 每个视频包的 data_hash（哈希的是包 payload，不是解码后画面）。
        second = snapped.episodes()[1]
        start = float(second.start_seconds)
        dur = float(second.duration_seconds)
        exported = video_packet_hashes(batch.succeeded[1].output_path)
        source = video_packet_hashes(media.path, start, start + dur)
        assert exported, "没有读到导出视频的包哈希"
        assert source, "没有读到源片对应区间的包哈希"
        exported_hashes = [line.rsplit(",", 1)[-1] for line in exported]
        source_hashes = [line.rsplit(",", 1)[-1] for line in source]
        assert exported_hashes == source_hashes[-len(exported_hashes):]


class TestRecap:
    """前情回顾：只在导出层重复上一集片尾，不改方案切点。"""

    def test_precise_recap_moves_start_but_keeps_episode_boundaries(
        self, binaries, assets, tmp_path
    ):
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [28, 60])
        before = list(plan.boundary_ticks)
        exporter = Exporter(
            binaries, media, ExportPreset(), recap_seconds=3
        )
        jobs = exporter.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )

        assert plan.boundary_ticks == before, "回顾不能修改方案本身"
        assert jobs[0].recap_seconds == 0, "第 1 集没有上一集可回顾"
        assert jobs[1].start_seconds == Fraction(25), "28 秒正片前带 3 秒回顾"
        assert jobs[1].episode.start_seconds == Fraction(28)
        assert jobs[1].recap_seconds == 3
        assert jobs[1].duration_seconds == Fraction(35)
        assert exporter.recap_by_episode == {
            2: Fraction(3),
            3: Fraction(3),
        }, "第 2、3 集都以各自上一集片尾作回顾"

    def test_recap_frame_count_includes_the_repeated_tail(
        self, binaries, assets, tmp_path
    ):
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [28, 60])
        base = Exporter(binaries, media, ExportPreset())
        recap = Exporter(binaries, media, ExportPreset(), recap_seconds=3)
        plain_jobs = base.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        recap_jobs = recap.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )

        assert recap_jobs[0].frame_count == plain_jobs[0].frame_count
        assert recap_jobs[1].frame_count == plain_jobs[1].frame_count + 3 * 25

    def test_native_recap_snaps_back_to_a_keyframe(
        self, binaries, assets, tmp_path
    ):
        """原生编码只能从关键帧开始：回顾起点向前吸附，不会从任意帧切。"""
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30, 60])
        exporter = Exporter(
            binaries,
            media,
            ExportPreset(),
            cut_mode=CutMode.FAST_COPY,
            recap_seconds=4,
        )
        jobs = exporter.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        keyframes = set(probe_keyframe_frames(binaries, media))
        start_frame = media.frame_index_at_or_after(jobs[1].start_seconds)
        assert start_frame in keyframes
        assert jobs[1].recap_seconds >= 4, "吸附只会让回顾更长，不会更短"
        command = " ".join(
            str(item)
            for item in exporter.build_command(jobs[1], tmp_path / "recap.mp4")
        )
        assert "-c copy" in command

    def test_recap_never_reaches_into_the_episode_before_previous(
        self, binaries, assets, tmp_path
    ):
        """回顾最多取到上一集片头，不能拖进更早的一集。"""
        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [5, 8])
        exporter = Exporter(binaries, media, ExportPreset(), recap_seconds=10)
        jobs = exporter.prepare_jobs(
            plan, tmp_path, audio_stream_index=media.audio_tracks[0].index
        )
        assert jobs[1].start_seconds == 0
        assert jobs[1].recap_seconds == 5, "上一集只有 5 秒，回顾不能更早"


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
