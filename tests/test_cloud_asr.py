"""云端转写：适配器映射、连通性判定、流水线复用与回退。

**不碰真厂商**：适配器测试打本机假服务器，引擎测试用假 transport（不发网络）。
真机验证是手工关卡（需用户 Key），不放在 CI。

这里盯住的是几条"错了会静默出错"的语义：

- 毫秒 → 精确秒（不经过 float 累加），且**块起点偏移必须加回**（§7.3）；
- 响应头 ``X-Api-Status-Code`` 才是成败依据（HTTP 200 也可能是失败）；
- 静音（20000003）是"鉴权与权限都通"的正常结果，不是失败；
- 没有词级时间戳 → 记为**质量降级**（句子退化到分句级），不编造词边界；
- 云端失败**不做半云端半本地**：记下"第几段"，由上层整体回退本地。
"""

from __future__ import annotations

import base64
import json
import threading
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.app_config import LOCAL_BACKEND, AppConfig
from app.core.asr import AsrSettings
from app.core.cache import CacheStore, SourceFingerprint
from app.core.cloud_asr import CloudAsrEngine, select_asr_engine
from app.core.cloud_volc import ASR_STATUS_OK, ASR_STATUS_SILENT, VolcFlashAsrTransport
from app.core.providers import (
    DEFAULT_PROFILES,
    ProviderError,
    ProviderSegment,
    ProviderTranscript,
    ProviderWord,
)

PORT = 0


class FakeSpeech:
    """最小的本地假「录音文件识别极速版」：记录请求，按脚本回响应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status_code = ASR_STATUS_OK
        self.message = "OK"
        self.body: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server 命名
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    payload = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                outer.requests.append(
                    {
                        "payload": payload,
                        "api_key": self.headers.get("X-Api-Key", ""),
                        "resource_id": self.headers.get("X-Api-Resource-Id", ""),
                    }
                )
                body = json.dumps(outer.body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Api-Status-Code", outer.status_code)
                self.send_header("X-Api-Message", outer.message)
                self.send_header("X-Tt-Logid", "logid-asr")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 静音
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    @property
    def endpoint(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/api/v3/auc/bigmodel/recognize/flash"

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def fake_speech():
    server = FakeSpeech()
    server.start()
    yield server
    server.stop()


def volc_response(utterances: list[dict], duration_ms: int = 2499) -> dict:
    return {"audio_info": {"duration": duration_ms}, "result": {"utterances": utterances}}


SAMPLE_UTTERANCE = {
    "start_time": 450,
    "end_time": 1530,
    "text": "关闭透传。",
    "words": [
        {"start_time": 450, "end_time": 770, "text": "关", "confidence": 0.9},
        {"start_time": 770, "end_time": 970, "text": "闭", "confidence": 0.8},
        {"start_time": 1130, "end_time": 1530, "text": "透传", "confidence": 0.7},
    ],
}


def make_transport(server: FakeSpeech, **overrides) -> VolcFlashAsrTransport:
    profile = DEFAULT_PROFILES[0].with_updates(api_key="sk-test", **overrides)
    return VolcFlashAsrTransport(
        server.endpoint, profile.resolved_api_key(), profile.asr_resource_id,
        profile.asr_model_name,
    )


class TestTransport:
    def test_uploads_base64_with_word_timestamps_on(self, fake_speech, tmp_path):
        fake_speech.body = volc_response([SAMPLE_UTTERANCE])
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 4096)

        result = make_transport(fake_speech).transcribe_file(audio, language="zh")

        assert fake_speech.requests, "应发出一次请求"
        sent = fake_speech.requests[0]
        assert sent["resource_id"] == "volc.bigasr.auc_turbo"
        request = sent["payload"]["request"]
        assert request["show_utterances"] is True, "不开这个开关就没有词级时间戳"
        assert request["enable_punc"] is True
        audio_field = sent["payload"]["audio"]
        assert audio_field["format"] == "wav"
        assert audio_field["language"] == "zh-CN", "内部语言码要换成厂商形式"
        assert base64.b64decode(audio_field["data"]) == audio.read_bytes()

        assert [w.text for w in result.words] == ["关", "闭", "透传"]
        assert result.words[0].start_ms == 450
        assert result.segments[0].text == "关闭透传。"
        assert result.duration_ms == 2499

    def test_status_header_decides_success_not_http(self, fake_speech, tmp_path):
        """HTTP 200 也可能是业务失败：成败只看响应头状态码。"""
        fake_speech.status_code = "45000151"
        # HTTP 头只能是 latin-1，真实厂商的错误文案同样走 ASCII（中文细节看响应体）
        fake_speech.message = "InvalidAudioFormat"
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 4096)

        with pytest.raises(ProviderError) as excinfo:
            make_transport(fake_speech).transcribe_file(audio, language="zh")
        text = excinfo.value.describe()
        assert "45000151" in text and "InvalidAudioFormat" in text and "logid-asr" in text

    def test_silent_audio_is_not_an_error(self, fake_speech, tmp_path):
        fake_speech.status_code = ASR_STATUS_SILENT
        fake_speech.body = {"audio_info": {"duration": 1000}}
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 4096)

        result = make_transport(fake_speech).transcribe_file(audio, language="zh")
        assert result.words == [] and result.segments == []

    def test_probe_accepts_silent_status(self, fake_speech):
        """1 秒静音样片：20000003 同样证明 Key 与资源权限是通的。"""
        fake_speech.status_code = ASR_STATUS_SILENT
        make_transport(fake_speech).probe()          # 不抛异常即通过
        assert fake_speech.requests, "探测也要真发一次请求"
        assert fake_speech.requests[0]["api_key"] == "sk-test"

    def test_probe_rejects_business_failure(self, fake_speech):
        fake_speech.status_code = "45000001"
        fake_speech.message = "InvalidParameter"
        with pytest.raises(ProviderError) as excinfo:
            make_transport(fake_speech).probe()
        assert "45000001" in excinfo.value.describe()

    def test_unsupported_container_is_refused_locally(self, fake_speech, tmp_path):
        """云端不收 mp4：本地就拒绝，不要白上传一个 100MB 的片子。"""
        video = tmp_path / "source.mp4"
        video.write_bytes(b"\x00" * 2048)
        with pytest.raises(ProviderError) as excinfo:
            make_transport(fake_speech).transcribe_file(video, language="zh")
        assert "不支持的音频格式" in str(excinfo.value)
        assert fake_speech.requests == [], "本地就该拦下，不该发请求"

    def test_missing_words_degrades_instead_of_faking(self, fake_speech, tmp_path):
        """只给分句、不给词：如实返回空 words，由引擎记为质量降级。"""
        utterance = {k: v for k, v in SAMPLE_UTTERANCE.items() if k != "words"}
        fake_speech.body = volc_response([utterance])
        audio = tmp_path / "chunk.mp3"
        audio.write_bytes(b"ID3" + b"\x00" * 4096)

        result = make_transport(fake_speech).transcribe_file(audio, language="zh")
        assert result.words == [], "没有词级时间戳就不能编造"
        assert [s.text for s in result.segments] == ["关闭透传。"]
        assert fake_speech.requests[0]["payload"]["audio"]["format"] == "mp3"


class StubTransport:
    """假 transport：按块返回固定词，并记录每次收到的音频路径。"""

    def __init__(self, *, words=("甲", "乙"), segment_text="甲乙。", fail_at: int | None = None):
        self.calls: list[Path] = []
        self._words = words
        self._segment_text = segment_text
        self._fail_at = fail_at

    def describe(self) -> str:
        return "假云端转写"

    def probe(self, *, timeout: float = 8.0) -> None:
        return None

    def transcribe_file(self, audio: Path, *, language: str, timeout: float = 60.0):
        index = len(self.calls)
        self.calls.append(Path(audio))
        if self._fail_at is not None and index == self._fail_at:
            raise ProviderError("识别失败（55000031）", vendor_message="服务器繁忙")
        span = 2000
        words = [
            ProviderWord(start_ms=i * 300, end_ms=i * 300 + 250, text=w)
            for i, w in enumerate(self._words)
        ]
        return ProviderTranscript(
            words=words,
            segments=[ProviderSegment(start_ms=0, end_ms=span, text=self._segment_text)],
            duration_ms=span,
        )


def make_engine(transport: StubTransport, *, chunk_seconds: int = 600) -> CloudAsrEngine:
    settings = AsrSettings(chunk_seconds=chunk_seconds, chunk_overlap_seconds=5)
    return CloudAsrEngine(transport, DEFAULT_PROFILES[0].with_updates(api_key="sk-test"),
                          settings=settings)


class TestEnginePipeline:
    def test_extracts_audio_and_adds_chunk_offsets(self, binaries, assets):
        """复用分块流水线：源片先抽音轨（不改传 mp4），块起点偏移必须加回。"""
        transport = StubTransport()
        engine = make_engine(transport, chunk_seconds=30)
        transcript = engine.transcribe(
            assets["cfr"], duration_seconds=None, ffmpeg=str(binaries.ffmpeg)
        )

        assert transport.calls, "应当逐块调用云端"
        assert all(p.suffix in (".mp3", ".wav") for p in transport.calls), (
            f"上传的必须是纯音频：{[p.name for p in transport.calls]}"
        )
        assert all(p.exists() is False for p in transport.calls), (
            "上传用的临时文件应在结束后清理干净"
        )
        assert transcript.words, "词必须被解析出来"
        assert transcript.model_tier.startswith("cloud:volc/"), transcript.model_tier
        assert transcript.chunk_count > 1, "30 秒分块下 120 秒素材应切成多块"

        # 每个块的词都从块起点往后 0.3 / 0.6 秒 —— 偏移没加回就会挤在 0 附近
        starts = sorted(float(w.start) for w in transcript.words)
        assert starts[0] == pytest.approx(0.0, abs=0.01), f"首块起点附近应有词：{starts[:3]}"
        assert max(starts) > 20, f"后续块的偏移没有加回：{starts}"

    def test_short_media_is_single_chunk_and_offset_zero(self, binaries, assets):
        transport = StubTransport()
        transcript = make_engine(transport).transcribe(
            assets["cfr"], duration_seconds=None, ffmpeg=str(binaries.ffmpeg)
        )
        assert len(transport.calls) == 1, "短于块长的素材只发一次"
        assert transcript.chunk_count == 1
        assert float(transcript.words[0].start) == pytest.approx(0.0, abs=0.01)

    def test_missing_words_are_recorded_as_quality_degradation(self, binaries, assets):
        transport = StubTransport(words=())
        transcript = make_engine(transport).transcribe(
            assets["cfr"], duration_seconds=None, ffmpeg=str(binaries.ffmpeg)
        )
        codes = [issue.code for issue in transcript.issues]
        assert "ASR_NO_WORD_TIMESTAMPS" in codes, codes
        assert transcript.sentences, "降级到分句级也要有句子，不能静默变空"

    def test_chunk_failure_reports_which_chunk(self, binaries, assets):
        """块级失败要说清是第几段——上层靠它决定整体回退本地。"""
        transport = StubTransport(fail_at=1)
        engine = make_engine(transport, chunk_seconds=30)
        with pytest.raises(ProviderError):
            engine.transcribe(
                assets["cfr"], duration_seconds=None, ffmpeg=str(binaries.ffmpeg)
            )
        assert engine.last_chunk_failure and "第 2 段" in engine.last_chunk_failure
        assert "55000031" in engine.last_chunk_failure

    def test_cache_is_backend_scoped(self, binaries, assets, tmp_path):
        """云端缓存与本地缓存必须分开：混用会让时间戳分布对不上。"""
        store = CacheStore(tmp_path / "cache")
        fingerprint = SourceFingerprint.of(assets["cfr"])
        transport = StubTransport()
        engine = make_engine(transport)

        first, hit_first = engine.transcribe_cached(
            assets["cfr"], fingerprint, store,
            duration_seconds=None, ffmpeg=str(binaries.ffmpeg),
        )
        second, hit_second = engine.transcribe_cached(
            assets["cfr"], fingerprint, store,
            duration_seconds=None, ffmpeg=str(binaries.ffmpeg),
        )
        assert hit_first is False and hit_second is True
        assert len(transport.calls) == 1, "第二次应命中缓存，不再外发"
        assert second.model_tier == first.model_tier


class TestSelection:
    def test_default_is_local(self):
        engine, notes = select_asr_engine(AppConfig(), AsrSettings())
        assert not getattr(engine, "is_cloud", False)
        assert notes == []

    def test_cloud_requires_both_switches(self):
        config = AppConfig()
        config.providers = [{"id": "volc", "api_key": "sk-x"}]     # 两个开关都没开
        config.backend_prefs = {"asr": "volc", "judge": LOCAL_BACKEND}

        engine, notes = select_asr_engine(config, AsrSettings())
        assert not getattr(engine, "is_cloud", False), "没开开关却切了云端"
        assert any("未启用" in note for note in notes), notes

    def test_cloud_selected_when_ready(self):
        config = AppConfig()
        config.providers = [
            {"id": "volc", "api_key": "sk-x", "enabled": True, "allow_upload": True}
        ]
        config.backend_prefs = {"asr": "volc", "judge": LOCAL_BACKEND}

        engine, notes = select_asr_engine(config, AsrSettings())
        assert getattr(engine, "is_cloud", False)
        joined = "\n".join(notes)
        assert "转写后端：云端转写" in joined
        assert "上传整条音轨" in joined and "openspeech.bytedance.com" in joined


class TestFallbackInWorker:
    """云端转写失败 → 整体回退本地重跑，且失败原因如实上日志（设计 §7）。"""

    def test_cloud_failure_falls_back_to_local_and_says_why(self, binaries, assets, tmp_path):
        from fractions import Fraction

        from app.core.probe import probe_media
        from app.core.settings import RangeMode, RangeSpec, SplitMode, SplitSettings
        from app.gui.workers import AnalysisWorker

        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )

        config = AppConfig()
        # 指向一个必然连不上的端口：模拟"档案配好了但云端不可用"
        config.providers = [{
            "id": "volc",
            "asr_endpoint": "http://127.0.0.1:1/api/v3/auc/bigmodel/recognize/flash",
            "api_key": "sk-test",
            "enabled": True,
            "allow_upload": True,
        }]
        config.backend_prefs = {"asr": "volc", "judge": LOCAL_BACKEND}
        config.fallback_to_local = True

        worker = AnalysisWorker(
            binaries, media, 0, settings, config=config, cache_dir=tmp_path / "cache"
        )
        lines: list[str] = []
        outcome: dict = {}
        worker.stage_log.connect(lines.append)
        worker.failed.connect(lambda text: outcome.setdefault("failed", text))
        worker.completed.connect(lambda plan, *_: outcome.update(plan=plan))
        worker.run()

        assert "failed" not in outcome, outcome.get("failed")
        assert outcome.get("plan") is not None, "云端转写失败不该让整轮分析失败"
        joined = "\n".join(lines)
        assert "云端转写失败" in joined and "已回退本地模型重跑" in joined, joined
        assert "第 1 段" in joined, "应说清是哪一段失败的"
        assert "跳过语音转写" in joined, "本地模型也没装时，应如实说明并继续（§7.2 可人工审核）"


class TestSegmentOnlyDegradation:
    """只给分句时间戳的后端（硅基流动 SenseVoice 这类）：降级要被记下来。

    这是 OpenAI 兼容协议里最常见的形态之一：厂商认 verbose_json 但不返回
    `words[]`。此时不能失败、也不能编造词边界，而是合成"分句级"的词并记一条
    风险——否则候选点会以为"这段没有词级信息"，审核页也就失去了粒度依据。
    """

    class SegmentsOnlyTransport(StubTransport):
        def transcribe_file(self, audio: Path, *, language: str, timeout: float = 60.0):
            result = super().transcribe_file(audio, language=language, timeout=timeout)
            return ProviderTranscript(
                words=[], segments=result.segments, duration_ms=result.duration_ms
            )

    def test_engine_records_quality_degradation(self, binaries, assets):
        engine = make_engine(self.SegmentsOnlyTransport())
        transcript = engine.transcribe(
            assets["cfr"], duration_seconds=None, ffmpeg=str(binaries.ffmpeg)
        )

        codes = {issue.code for issue in transcript.issues}
        assert "ASR_NO_WORD_TIMESTAMPS" in codes, "降级必须明说，不能静默"
        assert transcript.words, "分句会被合成为粗粒度词，句子重建仍要能工作"