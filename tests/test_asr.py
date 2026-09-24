"""ASR 层测试。

分两类：
- **纯单元测试**：切句规则、分块规划、繁简转换、序列化——不需要模型，任何环境都能跑。
- **集成测试**：需要 whisper 模型与语音素材。缺任一即跳过并给出获取命令，
  不会静默假通过。

集成测试的阈值来自实测（small 模型，中文 10 句 TTS 素材）：
    路径        句数     CER     起点偏差均值   最大
    单次        10/10   3.30%     73ms        220ms
    分块 12s/3s  9/10   4.40%     83ms        220ms
阈值在此之上留了余量——它们的作用是抓回归，不是复现精确数值。
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from app.core.asr import (
    AsrSettings,
    AsrSegment,
    AsrWord,
    Transcript,
    build_sentences,
    estimate_runtime,
    plan_chunks,
    split_sentence_for_subtitles,
    to_simplified,
    traditional_char_ratio,
)
from app.core.cache import CacheKey, CacheStore, SourceFingerprint

from conftest import whisper_model_or_skip


def word(start: float, end: float, text: str, probability: float = 0.9) -> AsrWord:
    return AsrWord(
        start=Fraction(str(start)), end=Fraction(str(end)), text=text, probability=probability
    )


# ---------------------------------------------------------------------------
# 切句（候选点的基础）
# ---------------------------------------------------------------------------


class TestBuildSentences:
    def test_sentence_end_punctuation_splits(self):
        words = [word(0, 1, "你好。"), word(1.1, 2, "再见。")]
        sentences = build_sentences(words)
        assert [s.text for s in sentences] == ["你好。", "再见。"]
        assert all(s.end_reason == "punctuation" for s in sentences)

    def test_short_clause_gap_does_not_split(self):
        """逗号后的短停顿是句内语气停顿，不是句末。

        这一条直接来自实测：阈值 0.35s 时，实测素材里「你欠的钱，」后的
        0.64s 逗号停顿会被误判为句末，10 句被切成 15 句。
        """
        words = [word(0, 1, "你欠的钱，"), word(1.64, 3, "白纸黑字写得清清楚楚。")]
        sentences = build_sentences(words)
        assert len(sentences) == 1, "0.64s 的逗号停顿不应断句"
        assert sentences[0].text == "你欠的钱，白纸黑字写得清清楚楚。"

    def test_long_clause_gap_does_split(self):
        """逗号后停顿超过 1.5 秒才视为句末——实测该阈值正好还原 10 句。"""
        words = [word(0, 1, "那笔钱不是我借的，"), word(2.57, 4, "那是谁签的字？")]
        sentences = build_sentences(words)
        assert len(sentences) == 2
        assert sentences[0].end_reason == "gap"

    def test_non_clause_gap_uses_shorter_threshold(self):
        """无标点处 1.0 秒即断句，比逗号后的门槛低。"""
        words = [word(0, 1, "好"), word(2.1, 3, "我们走")]
        sentences = build_sentences(words)
        assert len(sentences) == 2

    def test_trailing_sentence_reason_is_eof(self):
        sentences = build_sentences([word(0, 1, "没有标点"), word(1.1, 2, "结尾")])
        assert sentences[-1].end_reason == "eof"

    def test_empty_and_blank_words_ignored(self):
        assert build_sentences([]) == []
        assert build_sentences([word(0, 1, "   ")]) == []

    def test_sentence_index_is_sequential(self):
        words = [word(0, 1, "一。"), word(1.1, 2, "二。"), word(2.1, 3, "三。")]
        sentences = build_sentences(words)
        assert [s.index for s in sentences] == [1, 2, 3]

    def test_sentence_span_covers_its_words(self):
        words = [word(0.5, 1.5, "甲"), word(1.5, 2.5, "乙。")]
        sentence = build_sentences(words)[0]
        assert sentence.start == Fraction(1, 2)
        assert sentence.end == Fraction(5, 2)
        assert sentence.word_count == 2


class TestSplitForSubtitles:
    def test_short_sentence_unchanged(self):
        from app.core.asr import TranscriptSentence

        sentence = TranscriptSentence(1, Fraction(0), Fraction(1), "短句。", 2, "punctuation")
        assert split_sentence_for_subtitles(sentence) == ["短句。"]

    def test_long_sentence_split_at_punctuation_only(self):
        """只在标点处切，不做硬断行——硬断会把词切两半。"""
        from app.core.asr import TranscriptSentence

        text = "这是第一句比较长的话，这是第二句也不短，这是第三句还要再长一些。"
        sentence = TranscriptSentence(1, Fraction(0), Fraction(5), text, 30, "punctuation")
        parts = split_sentence_for_subtitles(sentence, max_chars=12)
        assert len(parts) > 1
        for part in parts:
            assert part.endswith(("，", "。"))
            assert part in text, f"切出的片段 {part!r} 不是原文的连续子串"


# ---------------------------------------------------------------------------
# 繁简转换
# ---------------------------------------------------------------------------


class TestSimplifiedConversion:
    def test_traditional_is_converted(self):
        assert to_simplified("那我們就法庭上見。") == "那我们就法庭上见。"
        assert to_simplified("隨便你。") == "随便你。"

    def test_simplified_is_unchanged(self):
        text = "这房子你今天必须交出来。"
        assert to_simplified(text) == text

    def test_ratio_detects_traditional_text(self):
        assert traditional_char_ratio("這房子必須交出來") > 0.2
        assert traditional_char_ratio("这房子必须交出来") < 0.01

    def test_empty_is_safe(self):
        assert to_simplified("") == ""
        assert traditional_char_ratio("") == 0.0


# ---------------------------------------------------------------------------
# 分块规划（§7.3）
# ---------------------------------------------------------------------------


class TestPlanChunks:
    def test_short_audio_is_single_chunk(self):
        assert plan_chunks(30, chunk_seconds=600, overlap_seconds=15) == [(0.0, 30)]

    def test_no_chunk_exceeds_limit(self):
        """任何一块都不超过 chunk_seconds。

        早先的实现会把过短的尾块并入上一块，把末块撑到 22.9s（名义 12s）——
        单块耗时与内存翻倍且不可预期。
        """
        for total in (25, 40.884, 120, 600, 7260):
            chunks = plan_chunks(total, chunk_seconds=60, overlap_seconds=10)
            assert all(end - begin <= 60 + 1e-6 for begin, end in chunks), (
                f"总长 {total} 时出现超长块：{chunks}"
            )

    def test_coverage_is_continuous(self):
        for total in (25, 40.884, 120, 600, 7260):
            chunks = plan_chunks(total, chunk_seconds=60, overlap_seconds=10)
            assert chunks[0][0] == 0
            assert abs(chunks[-1][1] - total) < 1e-6
            for previous, current in zip(chunks, chunks[1:]):
                assert current[0] < previous[1], f"出现断档：{previous} → {current}"

    def test_overlap_is_at_least_requested(self):
        chunks = plan_chunks(600, chunk_seconds=60, overlap_seconds=10)
        for previous, current in zip(chunks, chunks[1:]):
            assert previous[1] - current[0] >= 10 - 1e-6

    def test_snap_point_moves_starts_without_breaking_invariants(self):
        """吸附后仍须守住"无超长块、覆盖到片尾、连续"三条不变量。

        早先只挪起点不重算终点，块长被撑到 12.65s（名义 12s）。
        """
        def snap(target: float) -> float:
            return target - 0.7  # 模拟吸附偏移

        chunks = plan_chunks(40.884, chunk_seconds=12, overlap_seconds=3, snap_point=snap)
        assert all(end - begin <= 12 + 1e-6 for begin, end in chunks), chunks
        assert abs(chunks[-1][1] - 40.884) < 1e-6
        assert chunks[0][0] == 0.0
        for previous, current in zip(chunks, chunks[1:]):
            assert current[0] < previous[1]

    def test_snap_point_cannot_reorder_chunks(self):
        """吸附量很大的极端情况下也不能出现反序或零长块。"""
        def snap(target: float) -> float:
            return 0.0 if target > 0 else target

        chunks = plan_chunks(120, chunk_seconds=20, overlap_seconds=5, snap_point=snap)
        for begin, end in chunks:
            assert end > begin
        for previous, current in zip(chunks, chunks[1:]):
            assert current[0] >= previous[0]


# ---------------------------------------------------------------------------
# 序列化与估算
# ---------------------------------------------------------------------------


class TestTranscriptSerialization:
    def make_transcript(self) -> Transcript:
        transcript = Transcript(
            language="zh",
            language_probability=0.99,
            model_tier="small",
            prompt="提示",
            duration=Fraction(120),
            chunk_count=3,
            elapsed_seconds=12.5,
        )
        transcript.words = [word(0, 1, "你好"), word(1, 2, "世界")]
        transcript.segments = [AsrSegment(Fraction(0), Fraction(2), "你好世界")]
        transcript.sentences = build_sentences(transcript.words)
        return transcript

    def test_roundtrip_preserves_everything(self):
        original = self.make_transcript()
        restored = Transcript.from_json(json.loads(json.dumps(original.to_json())))
        assert restored.text == original.text
        assert len(restored.words) == len(original.words)
        assert len(restored.sentences) == len(original.sentences)
        assert restored.duration == original.duration
        assert restored.chunk_count == original.chunk_count
        assert restored.sentences[0].start == original.sentences[0].start
        assert restored.model_tier == "small"

    def test_describe_reports_counts(self):
        text = self.make_transcript().describe()
        assert "2 词" in text and "small" in text and "3 段" in text

    def test_empty_transcript_is_flagged(self):
        assert Transcript().is_empty


class TestRuntimeEstimate:
    def test_estimate_scales_with_duration(self):
        short = estimate_runtime(60, "small")
        long = estimate_runtime(600, "small")
        assert long > short > 0

    def test_larger_model_estimates_slower(self):
        assert estimate_runtime(600, "large-v3") > estimate_runtime(600, "tiny")

    def test_unknown_tier_falls_back_instead_of_crashing(self):
        assert estimate_runtime(600, "不存在的档位") > 0


class TestCacheParts:
    def test_settings_parts_cover_every_result_affecting_field(self):
        """任何影响转写结果的参数都必须进缓存键（§12.2）。"""
        parts = AsrSettings().cache_parts()
        for key in (
            "model_tier",
            "language",
            "prompt",
            "beam_size",
            "vad",
            "word_timestamps",
            "simplify",
        ):
            assert key in parts, f"{key} 未纳入缓存键"

    def test_changing_model_changes_cache_key(self):
        source = SourceFingerprint(1, 2, "src")
        a = CacheKey.build("asr", source, **AsrSettings(model_tier="small").cache_parts())
        b = CacheKey.build("asr", source, **AsrSettings(model_tier="medium").cache_parts())
        assert a.digest() != b.digest()


# ---------------------------------------------------------------------------
# 集成：真实转写（需要模型与语音素材）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def model_info():
    return whisper_model_or_skip("small")


@pytest.fixture(scope="module")
def single_pass(model_info, speech_asset, speech_truth):
    from app.core.asr import AsrEngine

    engine = AsrEngine(AsrSettings(model_tier="small", chunk_seconds=600))
    return engine.transcribe(
        speech_asset,
        duration_seconds=Fraction(str(speech_truth["total_seconds"])),
    )


@pytest.fixture(scope="module")
def chunked(model_info, speech_asset, speech_truth):
    from app.core.asr import AsrEngine

    engine = AsrEngine(AsrSettings(model_tier="small", chunk_seconds=12, chunk_overlap_seconds=3))
    return engine.transcribe(
        speech_asset,
        duration_seconds=Fraction(str(speech_truth["total_seconds"])),
    )


def character_error_rate(reference: str, hypothesis: str) -> float:
    """字符错误率（只比较字，不比较标点）。"""
    import sys
    from pathlib import Path as _Path

    tools = _Path(__file__).resolve().parent.parent / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from eval_asr import cer

    return cer(reference, hypothesis)[0]


class TestSinglePassTranscription:
    def test_produces_words_and_sentences(self, single_pass):
        assert not single_pass.is_empty
        assert single_pass.words, "应产出词级时间戳"
        assert single_pass.sentences, "应重建出句子"

    def test_sentence_count_matches_ground_truth(self, single_pass, speech_truth):
        """实测 10/10。这是候选点质量的前提。"""
        assert len(single_pass.sentences) == len(speech_truth["lines"]), (
            f"重建 {len(single_pass.sentences)} 句，标准答案 {len(speech_truth['lines'])} 句"
        )

    def test_character_error_rate_within_budget(self, single_pass, speech_truth):
        reference = "".join(line["text"] for line in speech_truth["lines"])
        rate = character_error_rate(reference, single_pass.text)
        assert rate <= 0.08, f"CER {rate:.2%} 超出预算（实测基线 3.30%）"

    def test_sentence_starts_are_accurate(self, single_pass, speech_truth):
        """每句起点与真值的偏差。实测均值 73ms、最大 220ms。"""
        errors = []
        for line in speech_truth["lines"]:
            nearest = min(
                single_pass.sentences, key=lambda s: abs(float(s.start) - line["start"])
            )
            error = abs(float(nearest.start) - line["start"])
            if error <= 1.5:
                errors.append(error)
        assert errors
        assert max(errors) <= 0.4, f"最大起点偏差 {max(errors):.3f}s"
        assert sum(errors) / len(errors) <= 0.2, f"平均起点偏差 {sum(errors)/len(errors):.3f}s"

    def test_output_is_simplified_chinese(self, single_pass):
        """提示词只能减弱繁体输出，必须靠转换兜底（实测句尾出现过繁体）。"""
        assert traditional_char_ratio(single_pass.text) <= 0.02, (
            f"繁体占比 {traditional_char_ratio(single_pass.text):.1%}，转换未生效"
        )

    def test_language_is_detected_as_chinese(self, single_pass):
        assert single_pass.language == "zh"


class TestChunkedTranscription:
    """§7.3：分块必须加回块起点偏移，且不得产生重复或崩坏文本。"""

    def test_uses_multiple_chunks(self, chunked):
        assert chunked.chunk_count >= 3

    def test_no_corrupted_or_duplicated_text(self, chunked, speech_truth):
        """实测的崩坏症状是「不是你你借记得」「那我们我们就就法庭上上见」。

        原因是块起点未吸附静音、落在词中间。这里既查词数不膨胀，
        也查 CER 不劣化太厉害。
        """
        reference = "".join(line["text"] for line in speech_truth["lines"])
        rate = character_error_rate(reference, chunked.text)
        assert rate <= 0.09, f"CER {rate:.2%} 超出预算（实测基线 4.40%）"

        expected_words = len(reference)
        actual_words = len(chunked.text)
        assert actual_words <= expected_words * 1.15, (
            f"词数膨胀到 {actual_words}（参考 {expected_words}），疑似重复转写"
        )

    def test_no_spurious_adjacent_duplicate_characters(self, chunked, speech_truth):
        """相邻重复字是块边界截断的典型症状。

        注意不能简单地"出现相邻同字就报错"：中文本来就有叠字与叠词
        （「清清楚楚」「看看」「想想」），实测标准答案里就含「楚楚」。
        因此只报**标准答案中不存在**的重复对。
        """
        reference = "".join(line["text"] for line in speech_truth["lines"])
        legitimate = {
            reference[i : i + 2] for i in range(len(reference) - 1) if reference[i] == reference[i + 1]
        }
        text = chunked.text
        suspicious = [
            text[i : i + 2]
            for i in range(len(text) - 1)
            if text[i] == text[i + 1]
            and text[i] not in "，。！？"
            and text[i : i + 2] not in legitimate
        ]
        assert not suspicious, f"出现标准答案中不存在的重复字：{suspicious[:5]}"

    def test_sentence_starts_close_to_ground_truth(self, chunked, speech_truth):
        errors = []
        for line in speech_truth["lines"]:
            nearest = min(chunked.sentences, key=lambda s: abs(float(s.start) - line["start"]))
            error = abs(float(nearest.start) - line["start"])
            if error <= 1.5:
                errors.append(error)
        assert max(errors) <= 0.4, f"最大起点偏差 {max(errors):.3f}s"

    def test_overlap_sufficiency_is_checked(self, chunked):
        """重叠不足必须留下风险记录，而不是静默丢字。"""
        codes = {issue.code for issue in chunked.issues}
        assert "ASR_OVERLAP_TOO_SMALL" not in codes, (
            "3 秒重叠对最长词应当充足；若报此风险说明吸附把重叠压得过小"
        )


class TestTranscriptionCache:
    def test_second_call_hits_cache(self, model_info, speech_asset, speech_truth, tmp_path):
        """§12.1 改分集参数不应重跑转写——缓存必须能复用。"""
        from app.core.asr import AsrEngine

        store = CacheStore(tmp_path / "cache")
        fingerprint = SourceFingerprint.of(speech_asset)
        engine = AsrEngine(AsrSettings(model_tier="small", chunk_seconds=600))

        first, hit = engine.transcribe_cached(
            speech_asset, fingerprint, store,
            duration_seconds=Fraction(str(speech_truth["total_seconds"])),
        )
        assert not hit
        second, hit = engine.transcribe_cached(
            speech_asset, fingerprint, store,
            duration_seconds=Fraction(str(speech_truth["total_seconds"])),
        )
        assert hit, "第二次应命中缓存"
        assert second.text == first.text
        assert len(second.sentences) == len(first.sentences)

    def test_different_settings_miss_cache(self, model_info, speech_asset, speech_truth, tmp_path):
        from app.core.asr import AsrEngine

        store = CacheStore(tmp_path / "cache")
        fingerprint = SourceFingerprint.of(speech_asset)
        duration = Fraction(str(speech_truth["total_seconds"]))

        AsrEngine(AsrSettings(model_tier="small", initial_prompt=None)).transcribe_cached(
            speech_asset, fingerprint, store, duration_seconds=duration
        )
        _, hit = AsrEngine(
            AsrSettings(model_tier="small", initial_prompt="不同提示词")
        ).transcribe_cached(speech_asset, fingerprint, store, duration_seconds=duration)
        assert not hit, "参数不同不应命中同一缓存"
