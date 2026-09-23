"""媒体探测与标记解码的测试。

这一层验证"工具能不能如实看见源片"——如果探测或标记解码本身不可靠，
后面所有关于切点正确性的断言都失去意义。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.core.probe import (
    LEVEL_BLOCK,
    LEVEL_INFO,
    LEVEL_WARN,
    check_compatibility,
    describe_media,
    probe_media,
)
from markers import BEEP_INTERVAL_FRAMES, FPS, MAX_FRAME_INDEX, encode_marker_bits
from verify import detect_beep_onsets, extract_audio_to_wav, probe_frame_count, probe_start_time
from verify import ffmpeg_binary, read_frame_indices, read_wav_mono

EXPECTED_FRAMES = 3000
EXPECTED_SECONDS = 120


class TestMarkerCodec:
    def test_bit_roundtrip_at_boundaries(self):
        from markers import decode_marker_bits

        for value in (0, 1, 2, 255, 256, 1023, MAX_FRAME_INDEX):
            assert decode_marker_bits(encode_marker_bits(value)) == value

    def test_msb_is_first(self):
        """MSB 在左：帧号 1 应为 00…01 而非 10…00。"""
        bits = encode_marker_bits(1)
        assert bits[-1] == 1 and all(b == 0 for b in bits[:-1])

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            encode_marker_bits(MAX_FRAME_INDEX + 1)
        with pytest.raises(ValueError):
            encode_marker_bits(-1)


class TestSourceFrameDecoding:
    """源片本身必须能被逐帧读回正确帧号，否则后续校验无意义。"""

    def test_every_frame_index_is_recovered_exactly(self, assets):
        indices = read_frame_indices(assets["cfr"])
        assert len(indices) == EXPECTED_FRAMES, (
            f"应读到 {EXPECTED_FRAMES} 帧，实际 {len(indices)} 帧"
        )
        assert indices == list(range(EXPECTED_FRAMES)), (
            f"帧号序列不正确，首个偏差位置："
            f"{next((i for i, v in enumerate(indices) if v != i), None)}"
        )

    def test_survives_lossy_encoding(self, assets):
        """条码方案必须对 CRF 18 有损编码鲁棒——这是它能替代像素哈希的前提。"""
        indices = read_frame_indices(assets["cfr"], max_frames=200)
        assert indices == list(range(200))


class TestAudioMarkers:
    def test_beep_onsets_land_on_expected_grid(self, assets, tmp_path):
        """§14.5 音频层：声音标记必须落在已知时间点上，误差远小于一帧。"""
        wav = extract_audio_to_wav(assets["cfr"], tmp_path / "src.wav")
        samples, rate = read_wav_mono(wav)
        onsets = detect_beep_onsets(samples, rate)

        interval = BEEP_INTERVAL_FRAMES / FPS  # 5.0 秒
        expected_count = int(EXPECTED_SECONDS / interval)  # 0,5,…,115 共 24 个
        assert len(onsets) == expected_count, f"应有 {expected_count} 个提示音，实际 {len(onsets)}"

        frame_duration = 1.0 / FPS
        for index, onset in enumerate(onsets):
            assert abs(onset - index * interval) < frame_duration, (
                f"第 {index} 个提示音位置 {onset:.4f}s，"
                f"期望 {index * interval:.4f}s，偏差超过一帧"
            )


class TestMediaProbe:
    def test_cfr_asset_metadata(self, binaries, assets):
        info = probe_media(binaries, assets["cfr"])
        assert info.video is not None
        assert (info.video.width, info.video.height) == (320, 180)
        assert info.video.nominal_fps == Fraction(25)
        assert info.video.nb_frames == EXPECTED_FRAMES
        assert info.timeline_duration == Fraction(EXPECTED_SECONDS)
        assert not info.is_vfr
        assert len(info.audio_tracks) == 1
        assert info.audio_tracks[0].sample_rate == 48000

    def test_video_time_base_is_not_frame_rate(self, binaries, assets):
        """§9.1 源视频时间基准必须与帧率区分开，不能混成"1/25"。"""
        info = probe_media(binaries, assets["cfr"])
        tb = info.video_time_base
        assert tb != info.video.nominal_fps, (
            f"时间基准 {tb.to_string()} 不应等于帧率 {info.video.nominal_fps}"
        )
        # 30 秒必须能被时间基准精确表示
        assert tb.ticks_to_seconds(tb.seconds_to_ticks(Fraction(30))) == Fraction(30)

    def test_frame_index_semantics_differ_by_direction(self, binaries, assets):
        """floor 与 ceil 语义必须区分：前者用于预览，后者用于边界。"""
        info = probe_media(binaries, assets["cfr"])
        one_and_a_half_frames = Fraction(3, 50)  # 1.5 帧 = 0.06s
        assert info.frame_index_at(one_and_a_half_frames) == 1
        assert info.frame_index_at_or_after(one_and_a_half_frames) == 2
        # 片尾必须映射到"最后一帧之后"
        assert info.frame_index_at_or_after(info.timeline_duration) == EXPECTED_FRAMES

    def test_snap_prefers_nearest_frame(self, binaries, assets):
        info = probe_media(binaries, assets["cfr"])
        # 30.11s 距帧 752（30.08s）0.03s、距帧 753（30.12s）0.01s → 取 753
        index, seconds = info.snap_to_frame(Fraction(3011, 100))
        assert index == 753
        assert seconds == Fraction(753, 25)

    def test_snap_tie_breaks_towards_earlier_frame(self, binaries, assets):
        """等距时必须取靠前的帧，避免把切点往后推、挤压下一集。"""
        info = probe_media(binaries, assets["cfr"])
        # 30.10s 与帧 752（30.08s）和帧 753（30.12s）等距
        index, seconds = info.snap_to_frame(Fraction(3010, 100))
        assert index == 752
        assert seconds == Fraction(752, 25)

    def test_no_audio_asset_is_reported(self, binaries, assets):
        info = probe_media(binaries, assets["no_audio"])
        assert not info.audio_tracks
        codes = {issue.code for issue in check_compatibility(info, binaries)}
        assert "NO_AUDIO_STREAM" in codes

    def test_multi_audio_asset_requires_explicit_choice(self, binaries, assets):
        info = probe_media(binaries, assets["multi_audio"])
        assert len(info.audio_tracks) == 2
        codes = {issue.code for issue in check_compatibility(info, binaries)}
        assert "MULTI_AUDIO_TRACK" in codes
        # 描述文本要能让人区分两条轨
        assert info.audio_tracks[0].describe() != info.audio_tracks[1].describe()

    def test_nonzero_start_pts_is_normalized(self, binaries, assets):
        """§9.3 非零起始 PTS 必须被识别，并换算成正确的 `-ss` 偏移。

        该素材的容器起始时间由音轨决定（比视频早一个 AAC 帧），
        因此 seek_offset 应为 1024/48000 ≈ 21.3ms，而不是 0。
        真正的正确性由 test_export_e2e 的帧身份断言证明：
        偏移算对了，切出来的首帧才是源片第 0 帧。
        """
        info = probe_media(binaries, assets["nonzero_pts"])
        start = probe_start_time(assets["nonzero_pts"])
        assert start is not None and abs(start - 5.0) < 0.05, f"容器起始时间应为 5s，实际 {start}"

        expected_offset = Fraction(1024, 48000)
        assert abs(info.seek_offset_seconds - expected_offset) < Fraction(1, 1000), (
            f"偏移应约 {float(expected_offset)*1000:.2f}ms，"
            f"实际 {float(info.seek_offset_seconds)*1000:.2f}ms"
        )
        # 偏移必须小于一帧，否则说明起始时间解读有误
        assert info.seek_offset_seconds < Fraction(1, 25)
        # 时间轴长度仍是 120 秒，不受起始偏移污染
        assert info.timeline_duration == Fraction(EXPECTED_SECONDS)

    def test_vfr_asset_is_detected(self, binaries, assets):
        """§9.3 VFR 必须被识别并给出明确处理路径，而不是静默按 CFR 处理。"""
        info = probe_media(binaries, assets["vfr"])
        assert info.is_vfr, "VFR 素材未被识别"
        assert info.vfr_evidence
        issues = check_compatibility(info, binaries)
        assert any(issue.code == "VFR_SOURCE" for issue in issues)

    def test_cfr_asset_is_not_flagged_as_vfr(self, binaries, assets):
        info = probe_media(binaries, assets["cfr"])
        issues = check_compatibility(info, binaries)
        assert not any(issue.code == "VFR_SOURCE" for issue in issues)

    def test_encoder_availability_is_checked(self, binaries, assets):
        info = probe_media(binaries, assets["cfr"])
        issues = check_compatibility(info, binaries)
        assert not any(issue.code == "ENCODER_MISSING" for issue in issues), (
            "当前 FFmpeg 缺少 libx264 或 aac，精确导出不可用"
        )

    def test_summary_is_human_readable(self, binaries, assets):
        info = probe_media(binaries, assets["cfr"])
        text = describe_media(info)
        assert "320×180" in text and "25.000fps" in text and "00:02:00.000" in text

    def test_deep_frame_count_matches(self, assets):
        """独立于容器头部信息，真实解码计数应一致。"""
        assert probe_frame_count(assets["cfr"], deep=True) == EXPECTED_FRAMES
