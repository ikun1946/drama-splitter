"""音频包络、缓存层与字幕解析的测试。

这三层都不依赖模型，因此可以在任何环境下完整运行。
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from app.core.audio import (
    DEFAULT_HOP_SECONDS,
    audio_rms_envelope,
    low_energy_point,
    silence_intervals,
    summarize_silence,
)
from app.core.cache import CacheKey, CacheStore, SourceFingerprint
from app.core.subtitles import (
    SubtitleCue,
    SubtitleTrack,
    apply_offset,
    estimate_offset,
    find_sidecar_subtitles,
    load_subtitle_file,
    validate_timing,
)


# ---------------------------------------------------------------------------
# 缓存层（§12.2）
# ---------------------------------------------------------------------------


class TestSourceFingerprint:
    def test_same_file_same_fingerprint(self, tmp_path):
        target = tmp_path / "a.bin"
        target.write_bytes(b"x" * 4096)
        first = SourceFingerprint.of(target)
        second = SourceFingerprint.of(target)
        assert first.digest == second.digest

    def test_content_change_changes_fingerprint(self, tmp_path):
        """内容变了必须换指纹，否则会误用旧缓存（§12.2 文件名相同不等于素材相同）。"""
        target = tmp_path / "a.bin"
        target.write_bytes(b"x" * 4096)
        before = SourceFingerprint.of(target)
        target.write_bytes(b"y" * 4096)
        after = SourceFingerprint.of(target)
        assert before.digest != after.digest

    def test_same_name_different_content_is_detected(self, tmp_path):
        """同名不同内容——正是 §12.2 要求识别的情形。"""
        one = tmp_path / "dir1" / "video.mp4"
        two = tmp_path / "dir2" / "video.mp4"
        one.parent.mkdir()
        two.parent.mkdir()
        one.write_bytes(b"A" * 8192)
        two.write_bytes(b"B" * 8192)
        assert SourceFingerprint.of(one).digest != SourceFingerprint.of(two).digest

    def test_sampling_does_not_read_whole_file(self, tmp_path):
        """大文件抽样哈希：不应读取全部内容。"""
        target = tmp_path / "big.bin"
        target.write_bytes(b"0" * (8 * 1024 * 1024))
        fingerprint = SourceFingerprint.of(target, sample_bytes=4096, samples=3)
        assert fingerprint.size_bytes == 8 * 1024 * 1024
        assert len(fingerprint.digest) == 64

    def test_json_roundtrip(self, tmp_path):
        target = tmp_path / "a.bin"
        target.write_bytes(b"data")
        original = SourceFingerprint.of(target)
        restored = SourceFingerprint.from_json(original.to_json())
        assert restored == original


class TestCacheKey:
    def make_source(self) -> SourceFingerprint:
        return SourceFingerprint(size_bytes=100, mtime_ns=1, digest="abc")

    def test_unknown_namespace_rejected(self):
        with pytest.raises(ValueError):
            CacheKey.build("nope", self.make_source())

    def test_parts_order_does_not_matter(self):
        source = self.make_source()
        a = CacheKey.build("asr", source, model="small", beam=5)
        b = CacheKey.build("asr", source, beam=5, model="small")
        assert a.digest() == b.digest()

    def test_part_change_changes_key(self):
        """任何影响结果的版本或参数变化都必须命中不同的键（§12.2）。"""
        source = self.make_source()
        base = CacheKey.build("asr", source, model="small")
        assert CacheKey.build("asr", source, model="medium").digest() != base.digest()

    def test_namespace_separates_keys(self):
        source = self.make_source()
        assert (
            CacheKey.build("asr", source).digest()
            != CacheKey.build("shots", source).digest()
        )

    def test_float_normalisation_is_stable(self):
        """0.1 与 0.10000000000000001 不应产生两个键。"""
        source = self.make_source()
        a = CacheKey.build("shots", source, threshold=0.1)
        b = CacheKey.build("shots", source, threshold=0.10000000000000001)
        assert a.digest() == b.digest()


class TestCacheStore:
    def make_key(self, namespace: str = "shots", **parts) -> CacheKey:
        return CacheKey.build(namespace, SourceFingerprint(1, 2, "src"), **parts)

    def test_save_load_roundtrip(self, tmp_path):
        store = CacheStore(tmp_path)
        key = self.make_key(a=1)
        store.save(key, {"shots": [1, 2, 3]}, note="测试")
        assert store.load(key) == {"shots": [1, 2, 3]}
        assert store.has(key)

    def test_missing_key_returns_none(self, tmp_path):
        store = CacheStore(tmp_path)
        assert store.load(self.make_key(missing=True)) is None

    def test_get_or_compute_reports_hit(self, tmp_path):
        store = CacheStore(tmp_path)
        key = self.make_key()
        calls = {"count": 0}

        def compute():
            calls["count"] += 1
            return {"value": 7}

        payload, hit = store.get_or_compute(key, compute)
        assert not hit and payload == {"value": 7} and calls["count"] == 1

        payload, hit = store.get_or_compute(key, compute)
        assert hit and payload == {"value": 7} and calls["count"] == 1, "第二次不应重算"

    def test_corrupt_entry_is_quarantined_not_fatal(self, tmp_path):
        """缓存损坏只应导致重算，绝不能让整个任务失败。"""
        store = CacheStore(tmp_path)
        key = self.make_key(a=1)
        store.save(key, {"value": 1})
        store.payload_path(key).write_text("{ this is not json", encoding="utf-8")

        assert store.load(key) is None
        quarantined = list(store.entry_dir(key).glob("*.corrupt-*"))
        assert quarantined, "损坏条目应被隔离保留现场，而不是直接删除"

    def test_key_mismatch_is_treated_as_corrupt(self, tmp_path):
        """文件被外部改动或误放时按损坏处理，不返回错误数据。"""
        store = CacheStore(tmp_path)
        key = self.make_key(a=1)
        store.save(key, {"value": 1})
        store.payload_path(key).write_text(
            json.dumps({"__key__": "someone-else", "payload": {"value": 999}}),
            encoding="utf-8",
        )
        assert store.load(key) is None

    def test_describe_reports_entry_count(self, tmp_path):
        store = CacheStore(tmp_path)
        store.save(self.make_key(a=1), {"v": 1})
        store.save(self.make_key(a=2), {"v": 2})
        assert "2 个缓存条目" in store.describe()

    def test_clear_removes_entries(self, tmp_path):
        store = CacheStore(tmp_path)
        store.save(self.make_key(a=1), {"v": 1})
        removed = store.clear()
        assert removed >= 1
        assert store.load(self.make_key(a=1)) is None


# ---------------------------------------------------------------------------
# 音频包络（供字幕对齐与分块吸附）——需要 FFmpeg 与语音素材
# ---------------------------------------------------------------------------


class TestAudioEnvelope:
    def test_envelope_length_matches_duration(self, binaries, speech_asset, assets):
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        expected = int(float(speech_asset_seconds(speech_asset)) / float(DEFAULT_HOP_SECONDS))
        assert abs(len(envelope) - expected) <= 2, (
            f"包络格数 {len(envelope)} 与按时长推算的 {expected} 不符"
        )

    def test_silence_is_detected_between_lines(self, binaries, speech_asset, speech_truth):
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        intervals = silence_intervals(envelope)
        summary = summarize_silence(intervals)
        # 素材有 9 处句间静音（0.6s）+ 首尾，检出数量应接近
        assert summary.count >= 8, summary.describe()
        assert summary.longest_seconds >= Fraction(1, 2)

    def test_low_energy_point_lands_in_silence(self, binaries, speech_asset, speech_truth):
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        # 第 2 句开始于 5.457s，其前的静音在 3.8–5.5s 之间
        point = low_energy_point(envelope, Fraction(45, 10))
        assert point is not None
        assert Fraction(35, 10) <= point <= Fraction(56, 10), f"吸附到 {point}，不在静音区间内"
        # 吸附点所在格的能量应显著低于语音段
        index = int(point / DEFAULT_HOP_SECONDS)
        speech_index = int(Fraction(10) / DEFAULT_HOP_SECONDS)  # 第 1 句中部
        assert envelope[index] < envelope[speech_index]

    def test_energy_is_higher_inside_speech(self, binaries, speech_asset):
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        speech_energy = sum(envelope[20:80]) / 60
        silence_energy = sum(envelope[80:100]) / 20
        assert speech_energy > silence_energy * 3, "语音段能量应明显高于静音段"


def speech_asset_seconds(path: Path) -> Fraction:
    import wave

    with wave.open(str(path), "rb") as handle:
        return Fraction(handle.getnframes(), handle.getframerate())


# ---------------------------------------------------------------------------
# 字幕解析与偏移（§7.2）
# ---------------------------------------------------------------------------


SRT_SAMPLE = """1
00:00:05,457 --> 00:00:07,825
如果我不交呢？

2
00:00:08,425 --> 00:00:13,433
你欠的钱，白纸黑字写得清清楚楚。

3
00:01:00,000 --> 00:01:02,000
<i>样式标签应被剥掉</i>
"""


class TestSubtitleParsing:
    def test_parse_srt_exactly(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text(SRT_SAMPLE, encoding="utf-8")
        track = load_subtitle_file(path)
        assert len(track.cues) == 3
        assert track.cues[0].start == Fraction(5457, 1000)
        assert track.cues[0].end == Fraction(7825, 1000)
        assert track.cues[0].text == "如果我不交呢？"

    def test_style_tags_are_stripped(self, tmp_path):
        """§7.2 样式信息不作为剧情文本。"""
        path = tmp_path / "a.srt"
        path.write_text(SRT_SAMPLE, encoding="utf-8")
        track = load_subtitle_file(path)
        assert track.cues[2].text == "样式标签应被剥掉"

    def test_vtt_centisecond_precision(self, tmp_path):
        path = tmp_path / "a.vtt"
        path.write_text(
            "WEBVTT\n\n00:00:01.50 --> 00:00:03.25\n简体测试\n", encoding="utf-8"
        )
        track = load_subtitle_file(path)
        assert track.cues[0].start == Fraction(150, 100)
        assert track.cues[0].end == Fraction(325, 100)

    def test_unsupported_extension_reports_issue(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("hello", encoding="utf-8")
        track = load_subtitle_file(path)
        assert track.issues and track.issues[0].code == "SUBTITLE_FORMAT_UNSUPPORTED"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_subtitle_file(tmp_path / "nope.srt")

    def test_srt_output_is_well_formed(self, tmp_path):
        path = tmp_path / "a.srt"
        path.write_text(SRT_SAMPLE, encoding="utf-8")
        track = load_subtitle_file(path)
        output = track.to_srt()
        assert "00:00:05,457 --> 00:00:07,825" in output
        # 往返可解析
        again = tmp_path / "b.srt"
        again.write_text(output, encoding="utf-8")
        assert len(load_subtitle_file(again).cues) == len(track.cues)

    def test_sidecar_discovery(self, tmp_path, assets):
        video = tmp_path / "剧集.mp4"
        video.write_bytes(b"fake")
        (tmp_path / "剧集.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        (tmp_path / "剧集.chs.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        found = find_sidecar_subtitles(video)
        assert len(found) == 2


class TestTimingValidation:
    def cue(self, index: int, start: Fraction, end: Fraction, text: str = "字") -> SubtitleCue:
        return SubtitleCue(index=index, start=start, end=end, text=text)

    def test_negative_time_reported(self):
        issues = validate_timing([self.cue(1, Fraction(-1), Fraction(1))])
        assert any(i.code == "SUBTITLE_NEGATIVE_TIME" for i in issues)

    def test_zero_duration_reported(self):
        issues = validate_timing([self.cue(1, Fraction(2), Fraction(2))])
        assert any(i.code == "SUBTITLE_ZERO_DURATION" for i in issues)

    def test_absurd_duration_reported(self):
        """把毫秒当秒写入是最常见的字幕错误，必须能抓到。"""
        issues = validate_timing([self.cue(1, Fraction(0), Fraction(300))])
        assert any(i.code == "SUBTITLE_ABSURD_DURATION" for i in issues)

    def test_overlap_is_info_not_blocking(self):
        """抢话与多人重叠属需人工留意的风险，不是错误（§7.2）。"""
        issues = validate_timing(
            [self.cue(1, Fraction(0), Fraction(5)), self.cue(2, Fraction(3), Fraction(8))]
        )
        overlap = [i for i in issues if i.code == "SUBTITLE_OVERLAP"]
        assert overlap and overlap[0].level == "info"

    def test_beyond_video_reported(self):
        issues = validate_timing(
            [self.cue(1, Fraction(0), Fraction(1)), self.cue(2, Fraction(100), Fraction(101))],
            timeline_duration=Fraction(60),
        )
        assert any(i.code == "SUBTITLE_BEYOND_VIDEO" for i in issues)

    def test_sparse_subtitle_reported(self):
        issues = validate_timing(
            [self.cue(1, Fraction(0), Fraction(2))], timeline_duration=Fraction(600)
        )
        assert any(i.code == "SUBTITLE_SPARSE" for i in issues)

    def test_clean_subtitle_has_no_issues(self):
        cues = [self.cue(i, Fraction(i * 3), Fraction(i * 3 + 2)) for i in range(1, 20)]
        assert validate_timing(cues, timeline_duration=Fraction(60)) == []


class TestSubtitleOffset:
    """偏移估计与校正（§7.2 检查与原声的时间偏移）。"""

    @staticmethod
    def build_cues(truth: dict) -> list[SubtitleCue]:
        return [
            SubtitleCue(
                index=line["index"],
                start=Fraction(str(line["start"])),
                end=Fraction(str(line["end"])),
                text=line["text"],
            )
            for line in truth["lines"]
        ]

    def test_aligned_subtitle_needs_no_correction(self, binaries, speech_asset, speech_truth):
        """本已对齐的字幕不应被"校正"。

        注意残留偏差约 0.22 秒是**起始点检测的系统性滞后**（见
        ONSET_DETECTION_LAG_SECONDS），不是随机误差；门槛设在该偏置之上，
        因此不会触发校正。
        """
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        result = estimate_offset(self.build_cues(speech_truth), envelope)
        assert abs(float(result.offset_seconds)) <= 0.3, (
            f"本已对齐的字幕被估计出 {float(result.offset_seconds):.3f}s 偏移"
        )
        assert not result.is_significant, "对齐的字幕不应被判为需要校正"

    def test_late_subtitle_is_detected_and_pulled_back(
        self, binaries, speech_asset, speech_truth
    ):
        """字幕整体晚 1.2 秒 → 应检出约 −1.2 秒并回拉，校正后残留 < 0.3 秒。"""
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        shift = Fraction(12, 10)
        shifted = [cue.shifted(shift) for cue in self.build_cues(speech_truth)]

        result = estimate_offset(shifted, envelope)
        assert result.is_significant, f"未被判为需要校正：{result.detail}"
        assert abs(float(result.offset_seconds) + 1.2) <= 0.3, (
            f"应检出约 −1.2s，实际 {float(result.offset_seconds):+.3f}s"
        )

        track = SubtitleTrack(path=None, fmt="srt", cues=list(shifted))
        corrected = apply_offset(track, result)
        assert corrected.offset_applied
        error = abs(float(corrected.cues[0].start) - float(self.build_cues(speech_truth)[0].start))
        assert error <= 0.5, f"校正后仍偏差 {error:.3f}s（校正前为 1.2s）"

    def test_early_subtitle_is_detected_and_pushed_back(
        self, binaries, speech_asset, speech_truth
    ):
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        shift = Fraction(-8, 10)
        shifted = [cue.shifted(shift) for cue in self.build_cues(speech_truth)]

        result = estimate_offset(shifted, envelope)
        assert result.is_significant
        assert abs(float(result.offset_seconds) - 0.8) <= 0.3, (
            f"应检出约 +0.8s，实际 {float(result.offset_seconds):+.3f}s"
        )

    def test_large_offset_is_recovered(self, binaries, speech_asset, speech_truth):
        """大偏移同样要还原——这是真实场景里最常见的帧率错配症状。"""
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        shift = Fraction(35, 10)
        shifted = [cue.shifted(shift) for cue in self.build_cues(speech_truth)]
        result = estimate_offset(shifted, envelope)
        assert abs(float(result.offset_seconds) + 3.5) <= 0.35, (
            f"应检出约 −3.5s，实际 {float(result.offset_seconds):+.3f}s"
        )

    def test_detector_lag_is_consistent_across_offsets(self, binaries, speech_asset, speech_truth):
        """残留偏差应稳定（是方法偏置而非随机噪声）——否则门槛无法设定。"""
        envelope = audio_rms_envelope(str(binaries.ffmpeg), speech_asset)
        base = self.build_cues(speech_truth)
        residuals = []
        for shift_value in (0.0, 1.2, -0.8, 3.5):
            cues = [cue.shifted(Fraction(str(shift_value))) for cue in base]
            result = estimate_offset(cues, envelope)
            residuals.append(float(result.offset_seconds) + shift_value)
        spread = max(residuals) - min(residuals)
        assert spread <= 0.25, f"残留偏差不稳定：{['%.3f' % r for r in residuals]}"

    def test_insignificant_offset_is_not_applied(self):
        """未达门槛的偏移宁可不动——把本来正确的字幕推歪代价更大。"""
        track = SubtitleTrack(
            path=None, fmt="srt", cues=[SubtitleCue(1, Fraction(0), Fraction(1), "字")]
        )
        from app.core.subtitles import SubtitleOffsetResult

        result = SubtitleOffsetResult(Fraction(1, 100), 0.9, "test")
        apply_offset(track, result)
        assert not track.offset_applied
        assert any(i.code == "SUBTITLE_OFFSET_NOT_APPLIED" for i in track.issues)

    def test_silent_audio_yields_no_offset(self):
        envelope = [0.0] * 200
        cues = [SubtitleCue(1, Fraction(1), Fraction(2), "字")]
        result = estimate_offset(cues, envelope)
        assert result.offset_seconds == 0
        assert result.confidence == 0.0

    def test_empty_input_is_safe(self):
        assert estimate_offset([], [0.1] * 10).confidence == 0.0
        assert estimate_offset([SubtitleCue(1, Fraction(0), Fraction(1), "字")], []).confidence == 0.0
