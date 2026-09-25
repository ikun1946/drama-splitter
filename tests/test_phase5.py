"""阶段5 测试：单集字幕、快速复制、帧表、硬件探测、任务恢复、推荐方案。

其中快速复制的验收标准是**帧内容一致**（条码读回逐帧核验），不是"帧数对"——
帧数对只说明长度对，不能说明拿到的就是那批帧。
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from app.core.cache import CacheStore, SourceFingerprint
from app.core.episode_files import export_episode_subtitles
from app.core.export import CutMode, ExportPreset, Exporter
from app.core.ffmpeg import probe_hw_encoders
from app.core.plan import BoundaryPlan
from app.core.probe import build_frame_table, probe_keyframe_frames
from app.core.recommend import recommend_plan
from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
    Strategy,
)

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def make_plan(media, cuts_seconds: list[float]) -> BoundaryPlan:
    """用秒数构造方案（内部转成 tick）。"""
    time_base = media.video_time_base
    ticks = [0]
    ticks += [time_base.seconds_to_ticks(Fraction(str(c))) for c in cuts_seconds]
    ticks.append(media.duration_ticks())
    return BoundaryPlan(
        boundary_ticks=ticks, time_base=time_base, total_ticks=media.duration_ticks()
    )


# ---------------------------------------------------------------------------
# 单集字幕（§7.2）
# ---------------------------------------------------------------------------


class TestEpisodeSubtitles:
    def build_transcript(self):
        from app.core.asr import AsrWord, Transcript, build_sentences

        transcript = Transcript(duration=Fraction(60))
        transcript.words = [
            AsrWord(start=Fraction(1), end=Fraction(9, 2), text="第一句。", probability=0.9),
            AsrWord(start=Fraction(11), end=Fraction(29, 2), text="第二句。", probability=0.9),
            AsrWord(start=Fraction(21), end=Fraction(24), text="第三句。", probability=0.9),
        ]
        transcript.sentences = build_sentences(transcript.words)
        return transcript

    def test_writes_one_srt_per_episode(self, binaries, tmp_path):
        from app.core.probe import probe_media

        media = probe_media(binaries, Path("testdata/cfr_120s_25fps.mp4"))
        # 60s 素材切两集：第一集 0-10s 含第一句，第二集 10-60s 含其余
        plan = make_plan(media, [10, 50])
        results = export_episode_subtitles(plan, self.build_transcript(), tmp_path)
        assert len(results) == 3

    def test_subtitle_time_is_relative_to_episode(self, binaries, tmp_path):
        """导出字幕的时间必须相对该集开头——观众从 0 开始看。"""
        from app.core.probe import probe_media

        media = probe_media(binaries, Path("testdata/cfr_120s_25fps.mp4"))
        plan = make_plan(media, [10, 50])
        results = export_episode_subtitles(plan, self.build_transcript(), tmp_path)
        content = results[0].path.read_text(encoding="utf-8")
        assert "00:00:01,000 --> 00:00:04,500" in content, content

    def test_subtitle_belongs_to_episode_containing_its_start(self, binaries, tmp_path):
        from app.core.probe import probe_media

        media = probe_media(binaries, Path("testdata/cfr_120s_25fps.mp4"))
        plan = make_plan(media, [10, 50])
        results = export_episode_subtitles(plan, self.build_transcript(), tmp_path)
        # 第二句起点 11s 属于第二集（10-50s）→ 相对时间 1s
        second = results[1].path.read_text(encoding="utf-8")
        assert "00:00:01,000" in second
        assert "第一句" not in second

    def test_cross_boundary_sentence_is_clipped_and_reported(self, binaries, tmp_path):
        """句尾越过集尾时必须截断并如实记录，否则下一集开头会凭空出现半句话。"""
        from app.core.asr import AsrWord, Transcript, build_sentences
        from app.core.probe import probe_media

        media = probe_media(binaries, Path("testdata/cfr_120s_25fps.mp4"))
        transcript = Transcript(duration=Fraction(60))
        transcript.words = [
            AsrWord(start=Fraction(8), end=Fraction(14), text="这句跨越切点。", probability=0.9)
        ]
        transcript.sentences = build_sentences(transcript.words)

        plan = make_plan(media, [10, 50])
        results = export_episode_subtitles(plan, transcript, tmp_path)
        assert results[0].clipped, "跨集句子必须被记录"
        assert "越界" in results[0].clipped[0] or "越过集尾" in results[0].clipped[0]
        content = results[0].path.read_text(encoding="utf-8")
        # 句子起点 8s 落在第一集（0-10s），集起点是 0 → 相对时间即 8s，
        # 结束时间被截到集尾 10s（原为 14s）
        assert "00:00:08,000 --> 00:00:10,000" in content, content
        # 不得产生零长或越过集尾的字幕
        assert "00:00:14" not in content

    def test_no_transcript_yields_explained_empty_results(self, binaries, tmp_path):
        from app.core.probe import probe_media

        media = probe_media(binaries, Path("testdata/cfr_120s_25fps.mp4"))
        plan = make_plan(media, [10, 50])
        results = export_episode_subtitles(plan, None, tmp_path)
        assert all(not r.success for r in results)
        assert all(r.notes for r in results), "必须说明为什么没有字幕"


# ---------------------------------------------------------------------------
# 快速复制（§14.2）
# ---------------------------------------------------------------------------


class TestFastCopy:
    def test_keyframe_probe_finds_first_frame(self, binaries, assets):
        """首帧那行在 ffprobe CSV 里带尾逗号，解析必须只取第一个字段。"""
        from app.core.probe import probe_media

        media = probe_media(binaries, assets["cfr"])
        keyframes = probe_keyframe_frames(binaries, media)
        assert 0 in keyframes, "首帧是关键帧，不能被解析丢掉"
        assert all(k < media.video.nb_frames for k in keyframes)

    def test_frame_exact_fast_copy(self, binaries, assets, tmp_path):
        """关键帧对齐时必须成功，且帧数精确。"""
        from app.core.probe import probe_media

        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30, 60, 90])  # 750/1500/2250 帧，均为关键帧
        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY, max_parallel=1
        )
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=0)

        assert not batch.plan_layer_problems, batch.plan_layer_problems
        assert len(batch.results) == 4
        assert all(r.success for r in batch.results), [r.message for r in batch.results]
        assert [r.actual_frame_count for r in batch.results] == [750] * 4

    def test_fast_copy_frames_are_identical_to_source(self, binaries, assets, tmp_path):
        """帧内容必须与源片逐帧一致——帧数对不等于帧对。"""
        from app.core.probe import probe_media
        from verify import read_frame_indices

        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [30, 60, 90])
        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY, max_parallel=1
        )
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=0)
        assert all(r.success for r in batch.results)

        sequence: list[int] = []
        for result in sorted(batch.results, key=lambda r: r.episode_index):
            frames = read_frame_indices(result.output_path)
            read_back = [n for n in frames if n >= 0]
            assert len(read_back) == len(frames), "有条码未能读回"
            sequence.extend(read_back)
        assert sequence == list(range(media.video.nb_frames)), (
            "各集拼接后必须正好覆盖源片全部帧")
        assert sequence[:3] == [0, 1, 2] and sequence[-1] == media.video.nb_frames - 1

    def test_non_keyframe_boundary_is_refused_not_downgraded(self, binaries, assets, tmp_path):
        """切点不是关键帧时必须明确拒绝，绝不静默降级为重新编码。"""
        from app.core.probe import probe_media

        media = probe_media(binaries, assets["cfr"])
        plan = make_plan(media, [28, 60, 90])  # 第 700 帧不是关键帧
        exporter = Exporter(
            binaries, media, ExportPreset(), cut_mode=CutMode.FAST_COPY, max_parallel=1
        )
        batch = exporter.export_plan(plan, tmp_path, audio_stream_index=0)

        assert batch.plan_layer_problems, "必须报出关键帧不满足"
        assert "FAST_COPY_KEYFRAME" in batch.plan_layer_problems[0]
        assert not batch.results, "拒绝时不应产出任何集"


# ---------------------------------------------------------------------------
# 帧表（VFR 地基）
# ---------------------------------------------------------------------------


class TestFrameTable:
    def test_cfr_frame_table_matches_linear_mapping(self, binaries, assets):
        """CFR 素材的帧表必须与线性映射一致——这也是"素材确实是 CFR"的复核。"""
        from app.core.probe import probe_media

        media = probe_media(binaries, assets["cfr"])
        table = build_frame_table(binaries, media)
        assert len(table) == media.video.nb_frames

        fps = float(media.video.nominal_fps)
        for row in (table[0], table[1], table[500], table[-1]):
            expected = row["frame"] / fps
            assert abs(row["pts_seconds"] - expected) <= 1.0 / fps, (
                f"第 {row['frame']} 帧 pts={row['pts_seconds']} 与线性值 {expected} 不符")

    def test_frame_table_marks_keyframes(self, binaries, assets):
        from app.core.probe import probe_media

        media = probe_media(binaries, assets["cfr"])
        table = build_frame_table(binaries, media)
        marked = [row["frame"] for row in table if row["key_frame"]]
        assert marked == probe_keyframe_frames(binaries, media)


# ---------------------------------------------------------------------------
# 硬件编码器探测
# ---------------------------------------------------------------------------


class TestHardwareProbe:
    def test_probe_returns_dict_and_never_crashes(self, binaries):
        found = probe_hw_encoders(binaries)
        assert isinstance(found, dict)
        for codec in found:
            assert codec.startswith("h264_")

    def test_reports_only_available_codecs(self, binaries):
        """返回的键必须是 ffmpeg 真的列出了的编码器。"""
        import subprocess

        listing = subprocess.run(
            [str(binaries.ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        for codec in probe_hw_encoders(binaries):
            assert codec in listing


# ---------------------------------------------------------------------------
# 任务恢复（§ 任务恢复）
# ---------------------------------------------------------------------------


class TestProjectRecovery:
    @pytest.mark.parametrize(
        "settings",
        [
            SplitSettings(
                split_mode=SplitMode.TARGET_DURATION,
                target_duration_seconds=Fraction(487, 4),
                range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
                strategy=Strategy.SUSPENSE,
            ),
            SplitSettings(
                split_mode=SplitMode.TARGET_EPISODE_COUNT,
                target_episode_count=42,
                count_policy=CountPolicy.FLEXIBLE,
                allowed_count_min=40,
                allowed_count_max=44,
                range=RangeSpec(
                    mode=RangeMode.MANUAL,
                    manual_min=Fraction(60),
                    manual_max=Fraction(75),
                ),
            ),
        ],
    )
    def test_settings_roundtrip(self, settings):
        restored = SplitSettings.from_json(json.loads(json.dumps(settings.to_json())))
        assert restored.split_mode == settings.split_mode
        assert restored.count_policy == settings.count_policy
        assert restored.target_episode_count == settings.target_episode_count
        assert restored.target_duration_seconds == settings.target_duration_seconds
        assert restored.range.mode == settings.range.mode
        assert restored.range.tolerance == settings.range.tolerance
        assert restored.range.manual_min == settings.range.manual_min
        assert restored.range.manual_max == settings.range.manual_max
        assert restored.strategy == settings.strategy

    def test_settings_roundtrip_preserves_fraction_exactly(self):
        """时长必须保持精确有理数，不能经过 float（§4.1）。"""
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(1, 3) + Fraction(487, 4),
        )
        restored = SplitSettings.from_json(json.loads(json.dumps(settings.to_json())))
        assert restored.target_duration_seconds == settings.target_duration_seconds

    def test_snapshot_roundtrip(self, binaries, assets, tmp_path):
        from app.core.probe import probe_media
        from app.gui.state import ProjectState

        media = probe_media(binaries, assets["cfr"])
        state = ProjectState()
        state.media = media
        state.audio_stream_index = 0
        state.settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        plan = make_plan(media, [30, 60, 90])
        state.add_plan(plan)

        path = state.save_snapshot(tmp_path / "project.json")
        payload = ProjectState.load_snapshot(path)

        assert payload["media_path"] == str(media.path)
        assert payload["audio_stream_index"] == 0
        assert payload["current_plan_version"] == state.current_plan.version
        restored_settings = SplitSettings.from_json(payload["settings"])
        assert restored_settings.target_duration_seconds == Fraction(30)
        assert len(payload["plans"]) == len(state.plans)

    def test_corrupt_snapshot_is_rejected(self, tmp_path):
        from app.gui.state import ProjectState

        path = tmp_path / "bad.json"
        path.write_text('{"schema": 99}', encoding="utf-8")
        with pytest.raises(ValueError):
            ProjectState.load_snapshot(path)


# ---------------------------------------------------------------------------
# 缓存失效与重算
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_invalidate_only_target_source(self, tmp_path):
        """换片只失效该素材的缓存，不能把整个缓存目录清掉。"""
        from app.core.cache import CacheKey

        store = CacheStore(tmp_path)
        old = SourceFingerprint(100, 1, "old-source")
        new = SourceFingerprint(200, 2, "new-source")
        store.save(CacheKey.build("asr", old, model="small"), {"text": "旧"})
        store.save(CacheKey.build("shots", old), {"scenes": []})
        store.save(CacheKey.build("asr", new, model="small"), {"text": "新"})

        removed = store.invalidate_source(old)
        assert removed == 4  # 2 条 × (payload + meta)
        assert store.load(CacheKey.build("asr", new, model="small")) == {"text": "新"}, (
            "其他素材的缓存必须保留")

    def test_invalidate_unknown_source_is_noop(self, tmp_path):
        store = CacheStore(tmp_path)
        assert store.invalidate_source(SourceFingerprint(1, 1, "nope")) == 0


# ---------------------------------------------------------------------------
# 推荐方案（比较口径一致性）
# ---------------------------------------------------------------------------


class FakeMedia:
    def __init__(self, total_seconds: float = 100.0, fps: int = 25) -> None:
        from app.core.timebase import TimeBase

        self.fps = fps
        self.total_frames = int(total_seconds * fps)
        self.video = type(
            "V", (), {"nominal_fps": Fraction(fps), "nb_frames": self.total_frames}
        )()
        self.video_time_base = TimeBase(num=1, den=12800)

    def duration_ticks(self) -> int:
        return self.total_frames * (12800 // self.fps)

    def frame_start_seconds(self, index: int) -> Fraction:
        return Fraction(index, self.fps)


class TestRecommendation:
    def build_candidates(self):
        from app.core.candidates import CandidatePoint, CandidateSet, SOURCE_SENTENCE_END

        return CandidateSet(
            points=[
                CandidatePoint(time=Fraction(t), frame_index=t * 25,
                               sources=[SOURCE_SENTENCE_END], score=0.8)
                for t in (25, 50, 75)
            ]
        )

    def derived(self):
        from app.core.settings import DerivedParams

        return DerivedParams(
            target_duration=Fraction(25),
            min_duration=Fraction(20),
            max_duration=Fraction(30),
            total_seconds=Fraction(100),
        )

    def test_reports_all_strategies_and_picks_lowest_cost(self):
        result = recommend_plan(
            self.build_candidates(),
            FakeMedia(),
            self.derived(),
            settings_count_exact=True,
            target_episode_count=4,
            allowed_min=4,
            allowed_max=4,
        )
        assert len(result.comparisons) == 3, "三个策略都要有记录"
        assert result.chosen is not None
        usable = [c for c in result.comparisons if c.ok]
        assert result.chosen.total_cost == min(c.total_cost for c in usable)
        assert any("落选" in note for note in result.notes), "落选者不能隐藏"

    def test_reports_failure_when_no_path(self):
        from app.core.candidates import CandidatePoint, CandidateSet, SOURCE_SENTENCE_END

        candidates = CandidateSet(
            points=[
                CandidatePoint(time=Fraction(10), frame_index=250,
                               sources=[SOURCE_SENTENCE_END], score=0.8)
            ]
        )
        result = recommend_plan(
            candidates,
            FakeMedia(),
            self.derived(),
            settings_count_exact=True,
            target_episode_count=4,
            allowed_min=4,
            allowed_max=4,
        )
        assert result.chosen is None
        assert all(not c.ok for c in result.comparisons)
        assert "无可用方案" in result.describe()

    def test_describe_lists_every_strategy(self):
        result = recommend_plan(
            self.build_candidates(),
            FakeMedia(),
            self.derived(),
            settings_count_exact=True,
            target_episode_count=4,
            allowed_min=4,
            allowed_max=4,
        )
        text = result.describe()
        for strategy in ("story", "suspense", "duration"):
            assert strategy in text
