"""阶段 1 闭环的端到端测试：导入 → 真实时间轴 → 边界导出 → 帧级校验。

核心思想：不靠目测，而是把帧号从成片里**读回来**。
每个断言都能定位到具体是第几帧错了，而不是"看起来还行"。

对应方案 §20.1 的客观检查与 §14.5 的五层校验。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.core.export import (
    ExportPreset,
    Exporter,
    verify_episode_output,
    write_episode_plan_csv,
    write_episode_plan_json,
)
from app.core.plan import BoundaryPlan
from app.core.probe import probe_media
from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
)
from verify import ffmpeg_binary, probe_frame_count, read_frame_indices
from verify import detect_beep_onsets, extract_audio_to_wav, read_wav_mono, measure_av_sync

FPS = 25
SOURCE_FRAMES = 3000
EPISODE_FRAMES = 750  # 30 秒 × 25fps
EPISODE_COUNT = 4


def build_frame_aligned_plan(media, cuts_in_frames: list[int], version: int = 1) -> BoundaryPlan:
    """按**中间切点**（帧号，不含片头片尾）构造方案，自动补全整片覆盖。"""
    seconds = [media.frame_start_seconds(index) for index in cuts_in_frames]
    return BoundaryPlan.from_interior_cuts(
        seconds,
        media.video_time_base,
        media.duration_ticks(),
        version=version,
    )


@pytest.fixture(scope="module")
def cfr_media(binaries, assets):
    return probe_media(binaries, assets["cfr"])


@pytest.fixture(scope="module")
def exported(tmp_path_factory, binaries, assets):
    """导出 4 集方案（每集 750 帧），供多个断言复用，避免重复编码。"""
    media = probe_media(binaries, assets["cfr"])
    cuts = [i * EPISODE_FRAMES for i in range(1, EPISODE_COUNT)]
    plan = build_frame_aligned_plan(media, cuts)
    output_root = tmp_path_factory.mktemp("export")

    exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
    batch = exporter.export_plan(plan, output_root, audio_stream_index=media.audio_tracks[0].index)
    return {"media": media, "plan": plan, "batch": batch, "root": output_root}


class TestPlanLayer:
    def test_plan_covers_whole_source(self, cfr_media):
        cuts = [i * EPISODE_FRAMES for i in range(1, EPISODE_COUNT)]
        plan = build_frame_aligned_plan(cfr_media, cuts)
        assert plan.coverage_valid()
        assert plan.episode_count == EPISODE_COUNT
        assert not [p for p in plan.validate() if p.is_blocking]

    def test_frame_counts_sum_to_source(self, cfr_media):
        """§14.5 视频层：各集帧数之和应等于源帧数。"""
        cuts = [i * EPISODE_FRAMES for i in range(1, EPISODE_COUNT)]
        plan = build_frame_aligned_plan(cfr_media, cuts)
        counts = plan.frame_counts(cfr_media)
        assert counts == [EPISODE_FRAMES] * EPISODE_COUNT
        assert sum(counts) == cfr_media.video.nb_frames == SOURCE_FRAMES

    def test_unaligned_boundary_snaps_and_still_sums(self, cfr_media):
        """非帧对齐边界必须被吸附，且吸附后帧数之和仍等于源帧数。"""
        plan = BoundaryPlan.from_seconds(
            [Fraction(0), Fraction(3001, 100), Fraction(6003, 100), Fraction(120)],
            cfr_media.video_time_base,
            cfr_media.duration_ticks(),
        )
        plan.snap_to_frames(cfr_media)
        counts = plan.frame_counts(cfr_media)
        assert sum(counts) == SOURCE_FRAMES
        assert all(c > 0 for c in counts)
        aligned, offenders = plan.is_frame_aligned(cfr_media)
        assert aligned, f"吸附后仍有未对齐边界：{offenders}"


class TestExportedArtifacts:
    def test_export_succeeded_for_every_episode(self, exported):
        batch = exported["batch"]
        assert not batch.plan_layer_problems
        assert batch.all_succeeded, f"导出未全部成功：{batch.summary()}；失败项：" + "; ".join(
            f"第{r.episode_index:02d}集 {r.message}" for r in batch.failed
        )

    def test_frame_counts_are_exact(self, exported):
        """每一集的帧数必须精确等于计划值——这是 -frames:v 钉死帧数的意义。"""
        for result in exported["batch"].succeeded:
            actual = probe_frame_count(result.output_path)
            assert actual == EPISODE_FRAMES, (
                f"第{result.episode_index:02d}集帧数为 {actual}，应为 {EPISODE_FRAMES}"
            )

    def test_frame_counts_sum_equals_source(self, exported):
        total = sum(probe_frame_count(r.output_path) for r in exported["batch"].succeeded)
        assert total == SOURCE_FRAMES, f"各集帧数之和 {total} ≠ 源帧数 {SOURCE_FRAMES}"

    def test_first_and_last_frame_identity(self, exported):
        """§14.5 边界层：每集首尾帧必须是源片里对应的那一帧，不多不少不重复。

        这是整个阶段 1 最关键的断言：它同时证明三件事——
        切点位置正确、没有多帧、没有少帧。
        """
        for order, result in enumerate(exported["batch"].succeeded):
            indices = read_frame_indices(result.output_path)
            expected_start = order * EPISODE_FRAMES
            assert len(indices) == EPISODE_FRAMES
            assert indices[0] == expected_start, (
                f"第{result.episode_index:02d}集首帧为 {indices[0]}，应为 {expected_start}"
            )
            assert indices[-1] == expected_start + EPISODE_FRAMES - 1
            assert indices == list(range(expected_start, expected_start + EPISODE_FRAMES)), (
                f"第{result.episode_index:02d}集帧序列不连续，首个偏差位置 "
                f"{next(i for i, v in enumerate(indices) if v != expected_start + i)}"
            )

    def test_no_frame_duplicated_or_skipped_across_episodes(self, exported):
        """把所有集的帧号拼起来，必须正好是源的连续序列。"""
        concatenated: list[int] = []
        for result in exported["batch"].succeeded:
            concatenated.extend(read_frame_indices(result.output_path))
        assert concatenated == list(range(SOURCE_FRAMES))

    def test_episode_count_is_exactly_as_requested(self, exported):
        assert len(exported["batch"].succeeded) == EPISODE_COUNT


class TestAvSync:
    def test_beep_grid_survives_cutting(self, exported, tmp_path):
        """§14.5 音频层：成片里的声音标记必须保持 5 秒间隔，误差在一帧以内。

        绝对起点允许存在 AAC 编码器延迟造成的固定偏移（这是封装行为，
        不是音画错位）；真正要守的是**间隔**与**相对位置**不能漂移。
        """
        first = exported["batch"].succeeded[0]
        wav = extract_audio_to_wav(first.output_path, tmp_path / "ep01.wav")
        samples, rate = read_wav_mono(wav)
        onsets = detect_beep_onsets(samples, rate)

        frame_duration = 1.0 / FPS
        assert len(onsets) >= 6, f"第 01 集应至少有 6 个提示音，实际 {len(onsets)}"

        # 绝对起点：容忍 AAC 编码器延迟
        assert abs(onsets[0] - 0.0) < 0.12, f"首个提示音在 {onsets[0]:.4f}s，偏离起点过多"

        # 间隔：必须严格保持 5 秒，误差不超过一帧
        for i in range(1, len(onsets)):
            gap = onsets[i] - onsets[i - 1]
            assert abs(gap - 5.0) < frame_duration, (
                f"第 {i} 与第 {i-1} 个提示音间隔 {gap:.4f}s，应为 5.000s"
            )

    def test_measured_sync_within_one_frame(self, exported, tmp_path):
        """§14.5 音频层：用标记直接量化音画偏差，必须在一个视频帧以内。

        这是最硬的同步判据：条码告诉我们每个输出帧是源片第几帧，
        提示音告诉我们音频时刻，两者相减就是真实偏差。
        它不依赖容器报的 start_time，因此能抓住
        `-avoid_negative_ts make_zero` 那类"元数据看起来正常、实际偏移一帧"的问题。
        """
        for result in exported["batch"].succeeded:
            measurement = measure_av_sync(result.output_path, tmp_path, fps=FPS)
            assert measurement.beep_count >= 6, (
                f"第{result.episode_index:02d}集只找到 {measurement.beep_count} 个声音标记"
            )
            assert measurement.within(1.0 / FPS), (
                f"第{result.episode_index:02d}集 {measurement.describe(FPS)}"
            )

    def test_audio_duration_matches_video_span(self, binaries, exported):
        result = exported["batch"].succeeded[0]
        verification = verify_episode_output(
            binaries,
            result.output_path,
            expected_frames=EPISODE_FRAMES,
            expected_duration=Fraction(30),
        )
        assert verification.ok, verification.blocking_problem
        assert verification.frame_count == EPISODE_FRAMES

    def test_audio_sample_count_matches_span(self, exported, tmp_path):
        """音频必须严格截到与视频同跨度，不能拖长到片尾（§14.3）。

        用 atrim 截断后，样本数与 30 秒 × 48000Hz 的偏差只应来自
        AAC 编码器的填充，量级在几千个采样点以内；若漏了截断，
        音频会长出几十秒，这里会立刻暴露。
        """
        result = exported["batch"].succeeded[0]
        wav = extract_audio_to_wav(result.output_path, tmp_path / "audio_check.wav")
        samples, rate = read_wav_mono(wav)
        assert rate == 48000
        expected = int(30 * rate)
        tolerance = 8 * (rate // FPS)  # 8 帧的容差，远超编码器填充
        assert abs(len(samples) - expected) <= tolerance, (
            f"音频样本数 {len(samples)}，期望约 {expected}"
            f"（偏差 {len(samples) - expected} 个采样点）"
        )


class TestFrameAlignment:
    """帧对齐是正确性前提，不是可选优化。

    若不强制对齐，边界落在两帧之间时 `-ss` 会顺延到下一帧，
    该集与下一集就重叠若干帧，且"各集帧数之和 = 源帧数"不再成立。
    """

    def test_unaligned_boundary_is_rejected_by_exporter(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["cfr"])
        plan = BoundaryPlan.from_seconds(
            [Fraction(0), Fraction(3001, 100), Fraction(120)],
            media.video_time_base,
            media.duration_ticks(),
        )
        aligned, offenders = plan.is_frame_aligned(media)
        assert not aligned and offenders == [1]

        exporter = Exporter(binaries, media, ExportPreset(crf=22, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert batch.plan_layer_problems
        assert any("FRAME_ALIGNMENT" in text for text in batch.plan_layer_problems)
        assert not batch.results

    def test_snap_to_frames_repairs_and_preserves_coverage(self, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        plan = BoundaryPlan.from_seconds(
            [Fraction(0), Fraction(3001, 100), Fraction(6003, 100), Fraction(120)],
            media.video_time_base,
            media.duration_ticks(),
        )
        issues = plan.snap_to_frames(media)
        assert not issues
        aligned, offenders = plan.is_frame_aligned(media)
        assert aligned, f"吸附后仍有未对齐边界：{offenders}"
        assert plan.coverage_valid()
        assert sum(plan.frame_counts(media)) == SOURCE_FRAMES
        assert all(count > 0 for count in plan.frame_counts(media))

    def test_snap_never_produces_zero_length_episode(self, binaries, assets):
        """吸附后必须保持严格递增；重合的边界要被推开而不是塌成零时长集。"""
        media = probe_media(binaries, assets["cfr"])
        # 两个边界都贴在 30 秒附近，吸附到同一帧后必须被修复
        plan = BoundaryPlan.from_seconds(
            [Fraction(0), Fraction(300001, 10000), Fraction(300009, 10000), Fraction(120)],
            media.video_time_base,
            media.duration_ticks(),
        )
        issues = plan.snap_to_frames(media)
        assert not issues
        ticks = plan.boundary_ticks
        assert all(ticks[i] > ticks[i - 1] for i in range(1, len(ticks)))
        assert len(set(ticks)) == len(ticks)


class TestNonZeroStartPts:
    def test_export_from_offset_container_keeps_frame_identity(self, binaries, assets, tmp_path):
        """容器起始时间为 5 秒时，`-ss` 仍须按时间轴定位。

        若误用绝对时间戳，切出来的首帧会偏离 125 帧（5 秒×25fps），
        这个断言专门捕捉那类错误。
        """
        media = probe_media(binaries, assets["nonzero_pts"])
        plan = build_frame_aligned_plan(media, [500, 1000])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=media.audio_tracks[0].index)
        assert batch.all_succeeded, batch.summary()

        assert len(batch.succeeded) == 3
        first, second, tail = batch.succeeded
        assert read_frame_indices(first.output_path) == list(range(0, 500))
        assert read_frame_indices(second.output_path) == list(range(500, 1000))
        # 末集覆盖余下全部素材，首帧必须是源片第 1000 帧
        tail_indices = read_frame_indices(tail.output_path)
        assert tail_indices[0] == 1000
        assert len(tail_indices) == SOURCE_FRAMES - 1000

    def test_offset_container_sync_within_one_frame(self, binaries, assets, tmp_path):
        """非零起始 PTS 素材上的音画偏差同样必须在一帧以内。

        这正是 `-avoid_negative_ts make_zero` 会踩坑的场景：实测它会引入
        约 21ms（一个 AAC 帧）的额外偏移，把偏差从 9.9ms 推到 31.2ms。
        """
        media = probe_media(binaries, assets["nonzero_pts"])
        plan = build_frame_aligned_plan(media, [500])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=media.audio_tracks[0].index)
        assert batch.all_succeeded, batch.summary()

        measurement = measure_av_sync(batch.succeeded[0].output_path, tmp_path, fps=FPS)
        assert measurement.beep_count >= 3
        assert measurement.within(1.0 / FPS), measurement.describe(FPS)


class TestTrackHandling:
    def test_no_audio_source_exports_video_only(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["no_audio"])
        plan = build_frame_aligned_plan(media, [250, 500])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert batch.all_succeeded, batch.summary()
        assert read_frame_indices(batch.succeeded[0].output_path)[0] == 0

    def test_second_audio_track_can_be_selected(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["multi_audio"])
        assert len(media.audio_tracks) == 2
        plan = build_frame_aligned_plan(media, [250])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(
            plan, tmp_path, audio_stream_index=media.audio_tracks[1].index
        )
        assert batch.all_succeeded, batch.summary()

    def test_unknown_audio_track_is_rejected_loudly(self, binaries, assets, tmp_path):
        media = probe_media(binaries, assets["cfr"])
        plan = build_frame_aligned_plan(media, [250])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=99)
        assert not batch.all_succeeded
        assert any("不在已识别的音轨列表" in r.message for r in batch.failed)


class TestTransactionAndRecovery:
    def test_existing_output_is_not_silently_overwritten(self, binaries, assets, tmp_path):
        """§14.4 已存在结果不静默覆盖。"""
        media = probe_media(binaries, assets["cfr"])
        plan = build_frame_aligned_plan(media, [250])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        first = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert first.all_succeeded

        target = first.succeeded[0].output_path
        size_before = target.stat().st_size

        second = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert not second.all_succeeded
        assert "已存在" in second.failed[0].message
        assert target.stat().st_size == size_before

    def test_no_partial_files_left_after_failure(self, binaries, assets, tmp_path):
        """失败不得留下 .part 半成品。"""
        media = probe_media(binaries, assets["cfr"])
        plan = build_frame_aligned_plan(media, [250])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        exporter.export_plan(plan, tmp_path, audio_stream_index=99)  # 非法音轨 → 必然失败
        leftovers = list(tmp_path.rglob("*.part.*"))
        assert not leftovers, f"残留临时文件：{leftovers}"

    def test_midflight_cancel_keeps_completed_episodes(self, binaries, assets, tmp_path):
        """§14.4 失败或取消时保留成功集数。"""
        media = probe_media(binaries, assets["cfr"])
        plan = build_frame_aligned_plan(media, [250, 500, 750])
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))

        state = {"count": 0}

        def on_done(result):
            state["count"] += 1
            if state["count"] >= 2:
                exporter.cancel()

        batch = exporter.export_plan(
            plan, tmp_path, audio_stream_index=None, on_episode_done=on_done
        )
        assert batch.cancelled
        assert len(batch.succeeded) >= 1
        for result in batch.succeeded:
            assert result.output_path.exists()
        assert not list(tmp_path.rglob("*.part.*"))

    def test_bad_plan_is_rejected_before_encoding(self, binaries, assets, tmp_path):
        """计划层不合格时不得开始编码（§14.4 冻结方案版本）。"""
        media = probe_media(binaries, assets["cfr"])
        plan = build_frame_aligned_plan(media, [750, 1500, 2250])
        plan.boundary_ticks[-1] = 2500  # 尾部没覆盖到片尾
        exporter = Exporter(binaries, media, ExportPreset(crf=20, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert batch.plan_layer_problems
        assert not batch.results
        assert not list(tmp_path.rglob("*.mp4"))

    def test_stale_marking_renames_affected_episodes(self, exported):
        """§14.4 切点改变后，相邻受影响集的既有产物必须标记过期。"""
        directory = exported["batch"].plan_directory
        marked = Exporter.mark_stale(directory, [2, 3])
        assert len(marked) == 2
        assert all(path.name.endswith(".stale") for path in marked)
        assert all(path.exists() for path in marked)


class TestStrictCountContract:
    def test_strict_count_plan_exports_exactly_n_episodes(self, binaries, assets, tmp_path):
        """§20.1 严格 N 集：输出恰好 N 集，或明确返回无解。"""
        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            count_policy=CountPolicy.EXACT,
            target_episode_count=5,
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        derived = settings.derive(media.timeline_duration)

        plan = BoundaryPlan.uniform(5, media.video_time_base, media.duration_ticks())
        plan.snap_to_frames(media)
        problems = [p for p in plan.validate(settings, derived) if p.is_blocking]
        assert not problems, [p.describe() for p in problems]

        exporter = Exporter(binaries, media, ExportPreset(crf=22, preset="veryfast"))
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=None)
        assert batch.all_succeeded, batch.summary()
        assert len(batch.succeeded) == 5

        indices = []
        for result in batch.succeeded:
            indices.extend(read_frame_indices(result.output_path))
        assert indices == list(range(SOURCE_FRAMES))


class TestArtifacts:
    def test_plan_json_and_csv_are_written(self, exported, tmp_path):
        """§18.2 data/episode_plan.json 与 .csv。"""
        plan = exported["plan"]
        json_path = tmp_path / "data" / "episode_plan.json"
        csv_path = tmp_path / "data" / "episode_plan.csv"
        write_episode_plan_json(plan, json_path)
        write_episode_plan_csv(plan, csv_path)

        import json

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["boundary_ticks"] == plan.boundary_ticks
        assert payload["coverage_valid"] is True
        assert payload["time_base"] == plan.time_base.to_string()
        assert len(payload["episodes"]) == EPISODE_COUNT

        text = csv_path.read_text(encoding="utf-8-sig")
        assert "集号" in text and "第" not in text.splitlines()[0]
        assert len(text.strip().splitlines()) == EPISODE_COUNT + 1
