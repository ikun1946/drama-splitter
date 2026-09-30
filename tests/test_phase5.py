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
        # 模式C 的自动判定要读总时长；用精确 Fraction，与时间轴其余部分一致
        self.timeline_duration = Fraction(self.total_frames, fps)

    def duration_ticks(self) -> int:
        return self.total_frames * (12800 // self.fps)

    def frame_start_seconds(self, index: int) -> Fraction:
        return Fraction(index, self.fps)
class TestRecommendationWiring:
    """「推荐方案」接进分析流程：自动判定集长与集数，再在该集数下比较三种策略。

    直接调 ``AnalysisWorker._solve_recommended``——它只依赖媒体对象、设置与
    候选点，不需要 ffmpeg/ASR，所以这条分支可以在离屏环境里真跑起来
    （整条 ``_analyze`` 需要语音素材与转写模型，本地常年是 skip）。
    """

    @staticmethod
    def _candidates(times=(3, 6, 9, 12, 15, 18, 21, 24, 27, 30)):
        from app.core.candidates import CandidatePoint, CandidateSet, SOURCE_SENTENCE_END

        return CandidateSet(
            points=[
                CandidatePoint(
                    time=Fraction(t), frame_index=t * 25,
                    sources=[SOURCE_SENTENCE_END], score=0.8,
                )
                for t in times
            ]
        )

    @staticmethod
    def _worker(**settings_fields):
        from app.gui.workers import AnalysisWorker

        # 注意：**不填** target_duration_seconds —— 模式C 的时长与集数由分析
        # 后自动判定，用户事先不需要给（这也是这次改动的核心）。
        settings = SplitSettings(split_mode=SplitMode.RECOMMEND, **settings_fields)
        return AnalysisWorker(None, FakeMedia(), None, settings)

    def test_decides_timing_then_strategy(self):
        worker = self._worker()
        logs: list[str] = []
        failures: list[str] = []
        worker.stage_log.connect(logs.append)
        worker.failed.connect(failures.append)

        plan, solution, problems, _notes = worker._solve_recommended(self._candidates(), {})

        assert not failures, f"不应报错：{failures}"
        assert plan is not None and solution is not None, "推荐模式必须交出方案"
        assert [p for p in problems if p.is_blocking] == []
        assert plan.episode_count >= 2, "至少要切出两集"
        assert plan.episode_count == solution.episode_count

    def test_logs_beat_anchor_and_every_candidate(self):
        """节拍依据与落选者都要写进日志——推荐不隐藏比较记录。"""
        worker = self._worker()
        logs: list[str] = []
        worker.stage_log.connect(logs.append)

        worker._solve_recommended(self._candidates(), {})
        text = "\n".join(logs)

        assert "内容节拍" in text, f"要说清时长是怎么来的：{text}"
        assert "自动判定：每集" in text, "要把结论写在一行里"
        assert "集数 / 策略比较" in text
        assert "落选：" in text, "落选者不能隐藏"

    def test_decision_is_written_back_to_settings(self):
        """判定结果回填到设置：随项目快照保存，重开项目仍看得到判成了什么。"""
        worker = self._worker()
        worker._solve_recommended(self._candidates(), {})

        settings = worker._settings
        assert settings.decided_episode_count and settings.decided_episode_count >= 2
        assert settings.decided_target_seconds is not None
        assert settings.decided_strategy in ("story", "suspense", "duration")
        # 关键：不能写回 target_duration_seconds —— 用户从没填过那个值
        assert settings.target_duration_seconds is None

    def test_nothing_feasible_reports_a_cause(self):
        """一个候选点都切不动时：报错要能照着改，不要只说"无解"。"""
        worker = self._worker()
        failures: list[str] = []
        worker.failed.connect(failures.append)

        plan, *_ = worker._solve_recommended(self._candidates(times=(10,)), {})

        assert plan is None, "无解时不得交出方案"
        assert failures, "无解必须报错"
        assert "自动判定失败" in failures[0]

    def test_recommend_needs_no_target_duration(self):
        """模式C 下 ``check_feasibility`` 不能再要求用户先填时长。"""
        from app.core.settings import check_feasibility

        settings = SplitSettings(split_mode=SplitMode.RECOMMEND)
        report = check_feasibility(settings, Fraction(100))

        assert report.auto_pending, "应标记为待自动判定"
        assert not report.blocking, "待判定不等于无解"
        assert report.derived is None
        # 界面不能把默认的 0 打印成「0 – 0 集」那种假信息
        assert report.min_episodes == 0 and report.max_episodes == 0
