"""镜头检测与候选数据库测试。

候选数据库是阶段3 的交付物，也是阶段2 动态规划的输入，因此这里的断言集中在
两条容易被做错的硬要求上：
1. 合并相近候选必须**保留全部证据**，且代表点必须落在**真实帧边界**上，
   **不得取平均**（§8.2）——取平均会落进一句话中间。
2. 因限流丢弃候选时**必须记录限制**（§8.4）——不能宣称已找到最优解。
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction

import pytest

from app.core.candidates import (
    GRADE_PRIORITY,
    GRADE_REVIEW,
    GRADE_USABLE,
    SOURCE_BLACK,
    SOURCE_SENTENCE_END,
    SOURCE_SHOT_CUT,
    SOURCE_SPEECH_GAP,
    CandidatePoint,
    CandidateSet,
    RuleWeights,
    build_candidate_set,
    collect_candidates,
    limit_candidates,
    merge_candidates,
    score_candidates,
)
from app.core.shots import (
    BlackInterval,
    SceneBoundary,
    ShotDetectionResult,
    detect_all,
    detect_black_intervals,
    detect_scenes,
)

FPS = 25


# ---------------------------------------------------------------------------
# 供单元测试使用的假时间轴（不依赖媒体文件）
# ---------------------------------------------------------------------------


class FakeTimeline:
    """按固定帧率实现 ceiling 语义的帧号映射，与 probe.MediaInfo 一致。"""

    def __init__(self, fps: int = FPS) -> None:
        self.fps = fps

    def frame_index_at_or_after(self, seconds: Fraction) -> int:
        product = Fraction(seconds) * self.fps
        return -((-product.numerator) // product.denominator)

    def snap_to_frame(self, seconds: Fraction) -> tuple[int, Fraction]:
        index = self.frame_index_at_or_after(seconds)
        # 向最近的帧起点取整，等距时取靠前的帧（与 MediaInfo 一致）
        candidates = [index - 1, index]
        best = min(candidates, key=lambda i: (abs(Fraction(i, self.fps) - seconds), i))
        return best, Fraction(best, self.fps)

    def frame_start_seconds(self, index: int) -> Fraction:
        return Fraction(index, self.fps)


@pytest.fixture
def timeline() -> FakeTimeline:
    return FakeTimeline()


def make_transcript(specs: list[tuple[float, float, str]]) -> "Transcript":  # noqa: F821
    from app.core.asr import AsrWord, Transcript, build_sentences

    words = []
    for start, end, text in specs:
        # 一行一句：把整句挂在一个词上，便于精确控制时间
        words.append(
            AsrWord(start=Fraction(str(start)), end=Fraction(str(end)), text=text, probability=0.9)
        )
    transcript = Transcript(duration=Fraction(100))
    transcript.words = words
    transcript.sentences = build_sentences(words)
    return transcript


# ---------------------------------------------------------------------------
# 镜头检测
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scene_cut_clip(binaries, tmp_path_factory) -> "Path":  # noqa: F821
    """构造一段有**真实镜头切换**的片段。

    为什么不能直接用 testdata 里的合成素材：那套素材的画面变化很弱
    （只有一根缓慢移动的白条与每 100 帧一次的底边闪动），ContentDetector
    根本不会触发——用它测镜头检测等于测了个寂寞。

    这里拼接三段差异明显的画面（纯色 / 彩条 / 不同尺寸的方块图），
    切换点在 2.0s 与 4.0s。
    """
    from pathlib import Path

    target = Path(tmp_path_factory.mktemp("shots")) / "cuts.mp4"
    cmd = [
        str(binaries.ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=white:size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=2",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
        "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-y", str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    assert proc.returncode == 0, proc.stderr[-300:]
    return target


class TestSceneDetection:
    def test_detects_boundaries_on_scene_cut_clip(self, scene_cut_clip):
        """三段明显不同的画面，切换点应在 2.0s 与 4.0s。"""
        scenes, issues = detect_scenes(scene_cut_clip, min_scene_len_frames=5)
        assert scenes, f"未检出任何镜头切换，问题：{[i.message for i in issues]}"
        times = sorted(float(s.time) for s in scenes)
        assert any(abs(t - 2.0) <= 0.3 for t in times), f"未在 2.0s 附近检出切换：{times}"
        assert any(abs(t - 4.0) <= 0.3 for t in times), f"未在 4.0s 附近检出切换：{times}"

    def test_min_scene_len_reduces_detections(self, scene_cut_clip):
        """过滤短镜头应减少检出数量——用于确认参数确实生效。"""
        loose, _ = detect_scenes(scene_cut_clip, min_scene_len_frames=1)
        strict, _ = detect_scenes(scene_cut_clip, min_scene_len_frames=200)
        assert len(strict) <= len(loose)

    def test_weak_change_asset_may_yield_few_cuts(self, assets):
        """画面变化很弱的素材检不出切换属正常，不应报错也不应崩溃。"""
        scenes, issues = detect_scenes(assets["cfr"], min_scene_len_frames=5)
        assert isinstance(scenes, list)
        assert all(s.time >= 0 for s in scenes)

    def test_black_intervals_detected_on_synthetic_clip(self, binaries, tmp_path):
        """用一段含黑场的合成片段验证 blackdetect。

        选 FFmpeg blackdetect 而不是图像阈值自己实现：黑场判定是明确的像素阈值
        问题，工具直接给出起止时间，更可靠。
        """
        clip = tmp_path / "black.mp4"
        # 2 秒彩条 + 1.5 秒黑场 + 2 秒彩条
        cmd = [
            str(binaries.ffmpeg), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
            "-f", "lavfi", "-i", "color=c=black:size=160x120:rate=25:duration=1.5",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-y", str(clip),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        assert proc.returncode == 0, proc.stderr[-300:]

        intervals, issues = detect_black_intervals(str(binaries.ffmpeg), clip)
        assert intervals, f"未检出黑场，问题：{[i.message for i in issues]}"
        longest = max(intervals, key=lambda b: b.duration)
        assert abs(float(longest.start) - 2.0) <= 0.3, f"黑场起点 {float(longest.start):.2f}s"
        assert abs(float(longest.duration) - 1.5) <= 0.3, f"黑场时长 {float(longest.duration):.2f}s"

    def test_no_black_in_normal_asset(self, binaries, assets):
        intervals, _ = detect_black_intervals(str(binaries.ffmpeg), assets["cfr"])
        assert intervals == [], "普通素材不应检出黑场"

    def test_result_json_roundtrip(self):
        result = ShotDetectionResult(
            scenes=[SceneBoundary(Fraction(1), Fraction(2), 25, 30.0)],
            blacks=[BlackInterval(Fraction(5), Fraction(6), Fraction(1))],
            threshold=27.0,
        )
        restored = ShotDetectionResult.from_json(json.loads(json.dumps(result.to_json())))
        assert restored.scenes[0].time == Fraction(1)
        assert restored.scenes[0].start_frame == 25
        assert restored.blacks[0].duration == Fraction(1)
        assert restored.threshold == 27.0

    def test_missing_backend_is_warned_not_fatal(self, monkeypatch):
        """镜头检测是可选增强，缺失不应让整条链路失败。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "scenedetect":
                raise ImportError("模拟未安装")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        scenes, issues = detect_scenes("不存在的文件.mp4")
        assert scenes == []
        assert issues and issues[0].code == "SCENEDETECT_UNAVAILABLE"


class TestShotCache:
    def test_second_call_hits_cache(self, binaries, scene_cut_clip, tmp_path):
        """§12.1 镜头检测与分集参数无关，应能跨次复用。"""
        from app.core.cache import CacheStore, SourceFingerprint

        store = CacheStore(tmp_path / "cache")
        fingerprint = SourceFingerprint.of(scene_cut_clip)
        first = detect_all(scene_cut_clip, binaries.ffmpeg, cache=store, fingerprint=fingerprint)
        assert first.scenes or first.blacks, "该片段应有镜头切换或黑场"
        second = detect_all(scene_cut_clip, binaries.ffmpeg, cache=store, fingerprint=fingerprint)
        assert len(second.scenes) == len(first.scenes)
        assert len(second.blacks) == len(first.blacks)
        assert second.elapsed_seconds == 0.0, "命中缓存时应不耗时"

    def test_empty_result_is_still_cached(self, binaries, assets, tmp_path):
        """**跑完但没找到镜头**同样要缓存。

        判据是"后端是否可用"，不是"结果是否为空"。早先按空结果跳过缓存，
        会让画面变化弱的素材每次白跑一遍慢检测。
        """
        from app.core.cache import CacheStore, SourceFingerprint

        store = CacheStore(tmp_path / "cache")
        fingerprint = SourceFingerprint.of(assets["cfr"])
        first = detect_all(assets["cfr"], binaries.ffmpeg, cache=store, fingerprint=fingerprint)
        assert first.elapsed_seconds > 0
        second = detect_all(assets["cfr"], binaries.ffmpeg, cache=store, fingerprint=fingerprint)
        assert second.elapsed_seconds == 0.0, "空结果也应命中缓存"


# ---------------------------------------------------------------------------
# 候选收集
# ---------------------------------------------------------------------------


class TestCollectCandidates:
    def collect(self, timeline, specs, shots=None, blacks=None, duration=100):
        transcript = make_transcript(specs)
        return collect_candidates(
            transcript,
            shots=shots,
            blacks=blacks,
            timeline_duration=Fraction(duration),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
        )

    def test_sentence_ends_become_candidates(self, timeline):
        result = self.collect(timeline, [(1.0, 3.0, "第一句。"), (5.0, 7.0, "第二句。")])
        ends = [p for p in result.points if SOURCE_SENTENCE_END in p.sources]
        assert len(ends) == 2

    def test_gap_midpoints_also_collected(self, timeline):
        """句间停顿本身也是候选来源（§8.2 语音间隙）。"""
        result = self.collect(timeline, [(1.0, 3.0, "第一句。"), (5.0, 7.0, "第二句。")])
        gaps = [p for p in result.points if SOURCE_SPEECH_GAP in p.sources]
        assert gaps
        assert abs(float(gaps[0].time) - 4.0) <= 0.05, "间隙中点应在 4.0s 附近"

    def test_all_candidates_land_on_real_frame_boundaries(self, timeline):
        """每个候选的时间都必须落在帧起点上，否则无法精确切割。"""
        result = self.collect(
            timeline,
            [(1.0, 3.0, "甲。"), (5.0, 7.0, "乙。"), (9.0, 11.0, "丙。")],
            shots=[SceneBoundary(Fraction(4), Fraction(5), 100)],
            blacks=[BlackInterval(Fraction(8), Fraction(9), Fraction(1))],
        )
        for point in result.points:
            assert (point.time * FPS).denominator == 1, f"{point.time} 不在帧起点上"

    def test_candidates_outside_timeline_are_dropped(self, timeline):
        result = self.collect(timeline, [(1.0, 3.0, "甲。")], blacks=[
            BlackInterval(Fraction(200), Fraction(201), Fraction(1))
        ])
        assert all(p.time <= 100 for p in result.points)

    def test_empty_input_reports_issue(self, timeline):
        result = collect_candidates(
            None,
            timeline_duration=Fraction(100),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
        )
        assert result.is_empty
        assert any(i.code == "CANDIDATES_EMPTY" for i in result.issues)

    def test_shot_cuts_collected_as_evidence_only(self, timeline):
        result = self.collect(
            timeline, [(1.0, 3.0, "甲。")],
            shots=[SceneBoundary(Fraction(4), Fraction(5), 100), SceneBoundary(Fraction(6), Fraction(7), 150)],
        )
        cuts = [p for p in result.points if SOURCE_SHOT_CUT in p.sources]
        assert len(cuts) == 2


# ---------------------------------------------------------------------------
# 合并（§8.2）
# ---------------------------------------------------------------------------


class TestMerge:
    def build(self, timeline, times: list[tuple[float, str]]) -> CandidateSet:
        candidates = CandidateSet()
        for time, source in times:
            index, snapped = timeline.snap_to_frame(Fraction(str(time)))
            candidates.points.append(
                CandidatePoint(
                    time=snapped,
                    frame_index=index,
                    sources=[source],
                    evidence=[f"{source} 在 {time}"],
                    merged_from=[Fraction(str(time))],
                )
            )
        return candidates

    def test_single_point_untouched(self, timeline):
        candidates = self.build(timeline, [(3.0, SOURCE_SENTENCE_END)])
        merged = merge_candidates(candidates, distance_seconds=Fraction(1), snap_to_frame=timeline.snap_to_frame)
        assert len(merged) == 1

    def test_nearby_points_merge_and_keep_all_evidence(self, timeline):
        """§8.2：合并后必须保留全部证据。"""
        candidates = self.build(
            timeline,
            [(10.0, SOURCE_SENTENCE_END), (10.4, SOURCE_SHOT_CUT), (10.8, SOURCE_BLACK)],
        )
        merged = merge_candidates(candidates, distance_seconds=Fraction(1), snap_to_frame=timeline.snap_to_frame)
        assert len(merged) == 1
        point = merged.points[0]
        assert set(point.sources) == {SOURCE_SENTENCE_END, SOURCE_SHOT_CUT, SOURCE_BLACK}
        assert len(point.evidence) == 3, "三条证据都要保留"
        assert len(point.merged_from) == 3, "要记录每个成员的原始时间以便回溯"

    def test_points_beyond_distance_stay_separate(self, timeline):
        candidates = self.build(
            timeline, [(10.0, SOURCE_SENTENCE_END), (13.0, SOURCE_SENTENCE_END)]
        )
        merged = merge_candidates(candidates, distance_seconds=Fraction(1), snap_to_frame=timeline.snap_to_frame)
        assert len(merged) == 2

    def test_representative_is_a_real_frame_boundary_not_an_average(self, timeline):
        """§8.2 明确禁止取平均——平均会落进一句话中间。

        构造：句末在 10.00s（第 250 帧，帧起点），镜头切换在 10.44s（第 261 帧，
        帧起点）。两者平均是 10.22s，而 10.22×25 = 255.5 **不是帧起点**。
        因此"代表点是否为帧起点"这一条能直接区分"选代表"与"取平均"。
        """
        candidates = self.build(
            timeline, [(10.0, SOURCE_SENTENCE_END), (10.44, SOURCE_SHOT_CUT)]
        )
        merged = merge_candidates(
            candidates, distance_seconds=Fraction(1), snap_to_frame=timeline.snap_to_frame
        )
        point = merged.points[0]
        assert (point.time * FPS).denominator == 1, (
            f"代表点 {point.time} 不是帧起点，说明取了平均值"
        )
        assert point.time in {Fraction(10), Fraction(1044, 100)}, (
            f"代表点必须是某个真实输入边界，实际 {point.time}"
        )
        assert point.time != Fraction(1022, 100), "不得落在两个输入的平均值上"

    def test_representative_priority_prefers_hard_evidence(self, timeline):
        """代表点应取证据更强的那个：黑场/句末优先于镜头切换（§8.1）。"""
        candidates = self.build(
            timeline, [(20.0, SOURCE_SHOT_CUT), (20.3, SOURCE_SENTENCE_END)]
        )
        merged = merge_candidates(candidates, distance_seconds=Fraction(1), snap_to_frame=timeline.snap_to_frame)
        point = merged.points[0]
        # 10.3 附近：句末优先，因此代表点时间应接近 20.3
        assert abs(float(point.time) - 20.3) <= 0.05, f"代表点落在 {point.time}"

    def test_merge_keeps_speech_context(self, timeline):
        transcript = make_transcript([(1.0, 3.0, "前一句。"), (5.0, 7.0, "后一句。")])
        collected = collect_candidates(
            transcript,
            timeline_duration=Fraction(100),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
            gap_min_seconds=Fraction(1, 2),
        )
        merged = merge_candidates(collected, distance_seconds=Fraction(2), snap_to_frame=timeline.snap_to_frame)
        assert merged.points
        point = merged.points[0]
        assert point.speech_before or point.speech_after


# ---------------------------------------------------------------------------
# 评分与分级（§11.2 / §11.3）
# ---------------------------------------------------------------------------


class TestScoring:
    def point(self, sources: list[str], **kwargs) -> CandidatePoint:
        return CandidatePoint(time=Fraction(10), frame_index=250, sources=list(sources), **kwargs)

    def test_sentence_end_scores_higher_than_shot_cut_alone(self):
        candidates = CandidateSet(
            points=[self.point([SOURCE_SENTENCE_END]), self.point([SOURCE_SHOT_CUT])]
        )
        score_candidates(candidates, timeline_duration=Fraction(100))
        by_source = {tuple(p.sources): p.score for p in candidates.points}
        assert by_source[(SOURCE_SENTENCE_END,)] > by_source[(SOURCE_SHOT_CUT,)]

    def test_black_and_sentence_end_reaches_priority_grade(self):
        candidates = CandidateSet(
            points=[self.point([SOURCE_SENTENCE_END, SOURCE_BLACK], gap_after=Fraction(1))]
        )
        score_candidates(candidates, timeline_duration=Fraction(100))
        assert candidates.points[0].grade == GRADE_PRIORITY
        assert not candidates.points[0].risks

    def test_shot_cut_alone_is_not_priority(self):
        """§8.1 镜头边界不可直接批量当作集尾。"""
        candidates = CandidateSet(points=[self.point([SOURCE_SHOT_CUT])])
        score_candidates(candidates, timeline_duration=Fraction(100))
        assert candidates.points[0].grade != GRADE_PRIORITY

    def test_too_close_next_speech_adds_risk_and_penalty(self):
        candidates = CandidateSet(
            points=[
                self.point([SOURCE_SENTENCE_END], gap_after=Fraction(15, 100)),
                self.point([SOURCE_SENTENCE_END], gap_after=Fraction(12, 10)),
            ]
        )
        score_candidates(candidates, timeline_duration=Fraction(100))
        risky = [p for p in candidates.points if p.risks]
        assert risky, "间隔过近的候选必须记录截断风险"
        assert any("截断对白" in risk for risk in risky[0].risks)

    def test_edge_positions_penalised(self):
        candidates = CandidateSet(
            points=[
                CandidatePoint(time=Fraction(1, 10), frame_index=2, sources=[SOURCE_SENTENCE_END]),
                CandidatePoint(time=Fraction(50), frame_index=1250, sources=[SOURCE_SENTENCE_END]),
                CandidatePoint(time=Fraction(997, 10), frame_index=2492, sources=[SOURCE_SENTENCE_END]),
            ]
        )
        score_candidates(candidates, timeline_duration=Fraction(100))
        middle = next(p for p in candidates.points if p.time == Fraction(50))
        assert middle.score > max(
            p.score for p in candidates.points if p is not middle
        ), "靠近片头片尾的候选应当被扣分"

    def test_grades_are_ordering_labels_not_probabilities(self):
        """§11.3：不得把排序分包装成正确率概率。"""
        candidates = CandidateSet(
            points=[
                CandidatePoint(time=Fraction(i), frame_index=i * 25, sources=[SOURCE_SENTENCE_END])
                for i in range(1, 6)
            ]
        )
        score_candidates(candidates, timeline_duration=Fraction(100))
        for point in candidates.points:
            assert point.grade in {GRADE_PRIORITY, GRADE_USABLE, GRADE_REVIEW}
            assert 0.0 <= point.score <= 1.0

    def test_card_states_score_is_not_a_probability(self):
        point = CandidatePoint(time=Fraction(10), frame_index=250, sources=[SOURCE_SENTENCE_END])
        score_candidates(CandidateSet(points=[point]), timeline_duration=Fraction(100))
        card = point.review_card()
        assert "不是正确率概率" in card
        assert "尚未接入剧情判断" in card

    def test_custom_weights_change_ranking(self):
        """权重应可配置（§11.2 权重保存在策略配置中）。"""
        heavy_gap = RuleWeights(sentence_end=0.1, speech_gap=0.9)
        candidates = CandidateSet(points=[self.point([SOURCE_SENTENCE_END])])
        score_candidates(candidates, weights=heavy_gap, timeline_duration=Fraction(100))
        assert candidates.points[0].score <= 0.2


# ---------------------------------------------------------------------------
# 限流（§8.4）
# ---------------------------------------------------------------------------


class TestLimiting:
    def build(self, count: int, step: float = 2.0) -> CandidateSet:
        points = []
        for index in range(count):
            time = Fraction(str(round(index * step, 3)))
            points.append(
                CandidatePoint(
                    time=time,
                    frame_index=int(time * FPS),
                    sources=[SOURCE_SENTENCE_END],
                    score=index / max(1, count),
                )
            )
        return CandidateSet(points=points)

    def test_under_limit_is_untouched(self):
        candidates = self.build(5)
        result = limit_candidates(candidates, per_window=10, window_seconds=Fraction(60))
        assert len(result.points) == 5
        assert not result.limitations

    def test_over_limit_drops_and_records_limitation(self):
        """§8.4：为限流缩小搜索范围时必须记录限制，不能宣称已找到最优解。"""
        candidates = self.build(30)
        result = limit_candidates(candidates, per_window=5, window_seconds=Fraction(60))
        assert len(result.points) < 30
        assert result.limitations, "限流必须留下记录"
        assert any("丢弃" in note for note in result.limitations)

    def test_window_edges_are_preserved(self):
        """窗口首尾各保留一个，避免整段失去可行边界（§8.4）。"""
        candidates = self.build(30)
        result = limit_candidates(candidates, per_window=3, window_seconds=Fraction(20))
        times = {p.time for p in result.points}
        assert Fraction(0) in times, "窗口首个候选应被保留"
        kept_times = sorted(times)
        assert kept_times[0] == Fraction(0)

    def test_budget_truncation_is_recorded(self):
        candidates = self.build(30)
        result = limit_candidates(
            candidates, per_window=30, window_seconds=Fraction(600), budget=10
        )
        assert len(result.points) == 10
        assert any("预算" in note for note in result.limitations)

    def test_limiting_preserves_sorted_order(self):
        candidates = self.build(30)
        result = limit_candidates(candidates, per_window=4, window_seconds=Fraction(20))
        times = [p.time for p in result.points]
        assert times == sorted(times)


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


class TestBuildCandidateSet:
    def test_full_pipeline(self, timeline, binaries, assets):
        """收集 → 合并 → 评分 → 限流。"""
        from app.core.asr import AsrSettings, AsrEngine

        transcript = make_transcript(
            [(1.0, 3.0, "第一句。"), (5.0, 7.0, "第二句。"), (9.0, 12.0, "第三句。")]
        )
        scenes, _ = detect_scenes(assets["cfr"], min_scene_len_frames=200)

        result = build_candidate_set(
            transcript=transcript,
            shots=scenes,
            blacks=[],
            timeline_duration=Fraction(100),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
            merge_distance=Fraction(1),
            per_window=8,
            window_seconds=Fraction(30),
        )
        assert not result.is_empty
        assert all((p.time * FPS).denominator == 1 for p in result.points)
        assert any(p.grade in {GRADE_PRIORITY, GRADE_USABLE} for p in result.points)

    def test_json_roundtrip(self, timeline):
        transcript = make_transcript([(1.0, 3.0, "第一句。"), (5.0, 7.0, "第二句。")])
        result = build_candidate_set(
            transcript=transcript,
            shots=None,
            blacks=[BlackInterval(Fraction(8), Fraction(9), Fraction(1))],
            timeline_duration=Fraction(100),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
            merge_distance=Fraction(1),
        )
        restored = CandidateSet.from_json(json.loads(json.dumps(result.to_json())))
        assert len(restored.points) == len(result.points)
        assert restored.points[0].time == result.points[0].time
        assert restored.points[0].sources == result.points[0].sources
        # 分数按 4 位小数落盘，往返后只保证到该精度——浮点等值是无效断言
        assert restored.points[0].score == pytest.approx(result.points[0].score, abs=1e-4)
        assert restored.points[0].grade == result.points[0].grade

    def test_describe_reports_sources_and_merging(self, timeline):
        transcript = make_transcript([(1.0, 3.0, "甲。"), (5.0, 7.0, "乙。")])
        result = build_candidate_set(
            transcript=transcript,
            shots=None,
            blacks=None,
            timeline_duration=Fraction(100),
            frame_index_at_or_after=timeline.frame_index_at_or_after,
            snap_to_frame=timeline.snap_to_frame,
            merge_distance=Fraction(1),
        )
        text = result.describe()
        assert "候选点" in text and "对白句末" in text
