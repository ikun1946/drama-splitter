"""OpenAI 兼容适配器：假网关跑通请求形态、解析、错误映射与探测分支。

**绝不碰真厂商接口**：这里的"网关"是本机一个假 HTTP 服务器，请求与响应都由
测试脚本控制。真机核实在手工关卡（哪些厂商真的给词级时间戳只能实测）。

这里断言的是几条"错了会静默出事"的语义：

- 有文本却一个时间戳都没有 → **报错**（绝不静默当成静音，否则有对白的片段
  会被判成"没有语音"，下游会把它当静音处理）；
- 只有 segments → 返回空 words，交给既有的分句级降级；
- 缺配置 / 格式不支持 → **一个请求都不发**；
- 探测的四种分支：通、鉴权失败、路径写错（提示 /v1）、静音样片被拒但
  /models 可兜底（并在 probe_note 里如实说明"没验证模型"）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.cloud_openai import OpenAiAsrTransport, OpenAiChatTransport
from app.core.providers import ProviderError

API_KEY = "sk-test-key"

CHAT_OK = {
    "choices": [{"message": {"role": "assistant", "content": '{"verdict": "good_cut"}'}}]
}

ASR_WITH_WORDS = {
    "task": "transcribe",
    "language": "zh",
    "duration": 3.5,
    "text": "你好世界",
    "words": [
        {"word": "你好", "start": 0.0, "end": 1.5},
        {"word": "世界", "start": 1.5, "end": 3.4},
    ],
    "segments": [{"id": 0, "start": 0.0, "end": 3.4, "text": "你好世界"}],
}

ASR_SEGMENTS_ONLY = {
    "text": "只有分句",
    "duration": 2.0,
    "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "只有分句"}],
}

ASR_SILENT = {"text": ""}

ASR_TEXT_NO_TIMESTAMPS = {"text": "有文本但没有时间戳"}


@dataclass
class Recorded:
    path: str
    body: str
    authorization: str
    content_type: str


class FakeGateway:
    """最小的本地假网关：/chat/completions + /audio/transcriptions + /models。"""

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.chat_status = 200
        self.chat_body: object = CHAT_OK
        self.asr_status = 200
        self.asr_body: object = ASR_WITH_WORDS
        self.asr_body_text: str | None = None      # 给"返回体不是 JSON"用
        self.models_status = 200
        self.models_body: object = {"data": [{"id": "whisper-1"}]}
        self.error_body: object = {"error": {"message": "invalid api key"}}
        self.api_status_message = ""
        self.logid = "logid-1234"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _record(self) -> str:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(
                    Recorded(
                        path=self.path,
                        body=raw.decode("utf-8", "replace"),
                        authorization=self.headers.get("Authorization", ""),
                        content_type=self.headers.get("Content-Type", ""),
                    )
                )
                return self.headers.get("X-Request-Id") or outer.logid

            def _respond(self, status: int, payload: object, logid: str) -> None:
                body = (
                    payload.encode("utf-8")
                    if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Request-Id", logid)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - http.server 命名
                logid = self._record()
                if self.path.endswith("/chat/completions"):
                    if outer.chat_status == 200:
                        self._respond(200, outer.chat_body, logid)
                    else:
                        self._respond(outer.chat_status, outer.error_body, logid)
                    return
                if self.path.endswith("/audio/transcriptions"):
                    if outer.asr_status == 200:
                        payload = (
                            outer.asr_body_text
                            if outer.asr_body_text is not None
                            else outer.asr_body
                        )
                        self._respond(200, payload, logid)
                    else:
                        message = outer.api_status_message or outer.error_body
                        self._respond(
                            outer.asr_status,
                            {"error": {"message": message}}
                            if isinstance(message, str)
                            else message,
                            logid,
                        )
                    return
                self._respond(404, {"error": {"message": "not found"}}, logid)

            def do_GET(self):  # noqa: N802 - http.server 命名
                logid = self._record()
                if self.path.endswith("/models"):
                    if outer.models_status == 200:
                        self._respond(200, outer.models_body, logid)
                    else:
                        self._respond(outer.models_status, outer.error_body, logid)
                    return
                self._respond(404, {"error": {"message": "no such endpoint"}}, logid)

            def log_message(self, *args):  # noqa: A003 - 静音 http.server 的日志
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    fake = FakeGateway()
    yield fake
    fake.close()


def make_chat(gateway: FakeGateway, **overrides) -> OpenAiChatTransport:
    options = dict(
        base_url=gateway.base_url, api_key=API_KEY, model="gpt-x", label="测试厂商"
    )
    options.update(overrides)
    return OpenAiChatTransport(**options)


def make_asr(gateway: FakeGateway, **overrides) -> OpenAiAsrTransport:
    options = dict(
        base_url=gateway.base_url, api_key=API_KEY, model="whisper-1", label="测试厂商"
    )
    options.update(overrides)
    return OpenAiAsrTransport(**options)


# ---------------------------------------------------------------------------
# 对话补全
# ---------------------------------------------------------------------------


class TestOpenAiChat:
    def test_sends_bearer_model_and_parses_content(self, gateway, tmp_path):
        transport = make_chat(gateway)
        assert transport.complete("判断这个切点") == '{"verdict": "good_cut"}'

        request = gateway.requests[0]
        assert request.path.endswith("/chat/completions")
        assert request.authorization == f"Bearer {API_KEY}"
        payload = json.loads(request.body)
        assert payload["model"] == "gpt-x"
        assert payload["messages"] == [{"role": "user", "content": "判断这个切点"}]
        assert payload["max_tokens"] == 160 and payload["temperature"] == 0.1

    def test_probe_uses_one_token(self, gateway):
        make_chat(gateway).probe()
        payload = json.loads(gateway.requests[0].body)
        assert payload["max_tokens"] == 1, "自检只要证明「能通」，不该真花一次判断的额度"
        assert payload["temperature"] == 0.0

    def test_without_model_info_the_payload_is_unchanged(self, gateway):
        """没配参数 = 不干预：请求里既没有 reasoning_effort，输入也没被裁。"""
        make_chat(gateway).complete("判断这个切点")
        payload = json.loads(gateway.requests[0].body)
        assert "reasoning_effort" not in payload
        assert payload["messages"][0]["content"] == "判断这个切点"

    def test_reasoning_effort_is_sent_when_set(self, gateway):
        from app.core.model_info import ModelInfo

        make_chat(gateway, info=ModelInfo(name="o3", reasoning="high")).complete("x")
        payload = json.loads(gateway.requests[0].body)
        assert payload["reasoning_effort"] == "high"

    def test_probe_never_sends_reasoning_effort(self, gateway):
        """自检只证明"能通"：带着思考等级去探测会平白多花一次推理额度。"""
        from app.core.model_info import ModelInfo

        make_chat(gateway, info=ModelInfo(name="o3", reasoning="high")).probe()
        payload = json.loads(gateway.requests[0].body)
        assert "reasoning_effort" not in payload

    def test_long_prompt_is_trimmed_to_the_context_window(self, gateway):
        from app.core.model_info import ModelInfo

        transport = make_chat(gateway, info=ModelInfo(name="m", context_window=1_000))
        transport.complete("头" * 5_000)
        payload = json.loads(gateway.requests[0].body)
        content = payload["messages"][0]["content"]
        assert len(content) < 5_000, "超窗必须裁"
        assert "被截断" in content, "裁过要说明，否则用户以为台词是完整的"
        assert content.endswith("头"), "保住结尾（系统提示与最近台词在末尾）"

    def test_prompt_within_window_is_untouched(self, gateway):
        from app.core.model_info import ModelInfo

        transport = make_chat(gateway, info=ModelInfo(name="m", context_window=100_000))
        transport.complete("短提示")
        payload = json.loads(gateway.requests[0].body)
        assert payload["messages"][0]["content"] == "短提示"

    def test_info_reaches_the_transport_through_the_profile(self, gateway):
        """档案里配了思考等级 → 经 build_judge_transport 一路传到请求。"""
        from app.core.cloud import build_judge_transport
        from app.core.model_info import ModelInfo
        from app.core.providers import PROTO_OPENAI, ProviderProfile

        profile = ProviderProfile(
            id="p", label="厂", base_url=gateway.base_url, api_key=API_KEY,
            judge_protocol=PROTO_OPENAI, judge_model="gpt-x", enabled=True,
            allow_upload=True,
            model_info=(ModelInfo(name="gpt-x", reasoning="low"),),
        )
        transport = build_judge_transport(profile)
        assert transport is not None
        transport.complete("判断")
        payload = json.loads(gateway.requests[0].body)
        assert payload["reasoning_effort"] == "low"

    def test_describe_carries_label_and_host(self, gateway):
        text = make_chat(gateway).describe()
        assert "测试厂商" in text
        assert "127.0.0.1" in text

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(401, "鉴权失败"), (429, "被限流"), (500, "厂商服务异常"), (400, "请求被拒绝")],
    )
    def test_http_errors_become_readable_reasons(self, gateway, status, expected):
        gateway.chat_status = status
        with pytest.raises(ProviderError) as info:
            make_chat(gateway).complete("x")
        assert expected in str(info.value)
        assert info.value.status == status
        assert info.value.vendor_message == "invalid api key"
        assert info.value.logid == "logid-1234"

    def test_missing_config_sends_nothing(self, gateway):
        with pytest.raises(ProviderError):
            make_chat(gateway, api_key="").complete("x")
        with pytest.raises(ProviderError):
            make_chat(gateway, model="").complete("x")
        with pytest.raises(ProviderError):
            make_chat(gateway, base_url="").complete("x")
        assert gateway.requests == [], "本地就能判定的缺失，不该发出请求"

    def test_content_parts_are_joined(self, gateway):
        gateway.chat_body = {
            "choices": [{"message": {"content": [{"text": "abc"}, {"text": "def"}]}}]
        }
        assert make_chat(gateway).complete("x") == "abcdef"


# ---------------------------------------------------------------------------
# 音频转写
# ---------------------------------------------------------------------------


class TestListModels:
    """按厂商拉模型清单：界面里「刷新模型列表」走的就是这条路径。"""

    def test_parses_openai_shape(self, gateway):
        from app.core.cloud_openai import list_models

        gateway.models_body = {
            "data": [{"id": "whisper-1"}, {"id": "gpt-4o-mini"}, {"id": "GLM-4"}]
        }
        assert list_models(gateway.base_url, API_KEY) == ["GLM-4", "gpt-4o-mini", "whisper-1"]
        assert gateway.requests[0].path.endswith("/models")
        assert gateway.requests[0].authorization == f"Bearer {API_KEY}"

    def test_tolerates_other_shapes_and_dedupes(self, gateway):
        from app.core.cloud_openai import list_models

        gateway.models_body = {"models": [{"name": "b"}, {"model": "a"}, "b", {"nope": 1}]}
        assert list_models(gateway.base_url, API_KEY) == ["a", "b"]

    def test_reports_auth_failure(self, gateway):
        from app.core.cloud_openai import list_models

        gateway.models_status = 401
        with pytest.raises(ProviderError) as info:
            list_models(gateway.base_url, API_KEY)
        assert "鉴权失败" in str(info.value)

    def test_reports_missing_endpoint(self, gateway):
        from app.core.cloud_openai import list_models

        gateway.models_status = 404
        with pytest.raises(ProviderError):
            list_models(gateway.base_url, API_KEY)

    def test_empty_payload_is_reported(self, gateway):
        from app.core.cloud_openai import list_models

        gateway.models_body = {"object": "list"}
        with pytest.raises(ProviderError) as info:
            list_models(gateway.base_url, API_KEY)
        assert "/models" in str(info.value)

    def test_missing_config_sends_nothing(self, gateway):
        from app.core.cloud_openai import list_models

        with pytest.raises(ProviderError):
            list_models(gateway.base_url, "")
        with pytest.raises(ProviderError):
            list_models("", API_KEY)
        assert gateway.requests == [], "本地就能判定的缺失，不该发出请求"


class TestOpenAiAsrRequest:
    def test_multipart_carries_fixed_protocol_fields(self, gateway, tmp_path):
        audio = tmp_path / "chunk.mp3"
        audio.write_bytes(b"ID3fake-mp3-bytes")
        make_asr(gateway).transcribe_file(audio, language="zh")

        request = gateway.requests[0]
        assert request.path.endswith("/audio/transcriptions")
        assert request.authorization == f"Bearer {API_KEY}"
        assert request.content_type.startswith("multipart/form-data; boundary=")
        assert 'name="model"' in request.body and "whisper-1" in request.body
        assert 'name="response_format"' in request.body and "verbose_json" in request.body
        assert "timestamp_granularities" in request.body, "词级时间戳必须显式索取"
        assert 'name="file"; filename="chunk.mp3"' in request.body
        assert "audio/mpeg" in request.body, "按后缀给 content-type"

    def test_language_is_mapped_and_can_be_omitted(self, gateway, tmp_path):
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        make_asr(gateway).transcribe_file(audio, language="yue")
        assert 'name="language"' in gateway.requests[0].body
        assert "\r\n\r\nzh\r\n" in gateway.requests[0].body, "粤语要映射成 zh"

        gateway.requests.clear()
        make_asr(gateway).transcribe_file(audio, language="")
        assert 'name="language"' not in gateway.requests[0].body, "空语言就该整字段省略"

    def test_rejects_unsupported_format_without_request(self, gateway, tmp_path):
        audio = tmp_path / "movie.mp4"
        audio.write_bytes(b"\x00\x00")
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).transcribe_file(audio, language="zh")
        assert "不支持的音频格式" in str(info.value)
        assert gateway.requests == [], "格式不对要在本地拦下，不该把 mp4 传上云"

    def test_missing_config_sends_nothing(self, gateway, tmp_path):
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        with pytest.raises(ProviderError):
            make_asr(gateway, model="").transcribe_file(audio, language="zh")
        with pytest.raises(ProviderError):
            make_asr(gateway, api_key="").transcribe_file(audio, language="zh")
        assert gateway.requests == []


class TestOpenAiAsrParsing:
    def test_words_seconds_become_milliseconds(self, gateway, tmp_path):
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        result = make_asr(gateway).transcribe_file(audio, language="zh")

        assert [(w.start_ms, w.end_ms, w.text) for w in result.words] == [
            (0, 1500, "你好"),
            (1500, 3400, "世界"),
        ]
        assert result.duration_ms == 3500, "小数秒必须精确换算（1.5s → 1500ms）"
        assert [seg.text for seg in result.segments] == ["你好世界"]

    def test_segments_only_returns_empty_words(self, gateway, tmp_path):
        """只有分句时间戳：返回空 words，由上层走既有的分句级降级。"""
        gateway.asr_body = ASR_SEGMENTS_ONLY
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        result = make_asr(gateway).transcribe_file(audio, language="zh")
        assert result.words == []
        assert [seg.text for seg in result.segments] == ["只有分句"]

    def test_silence_is_not_an_error(self, gateway, tmp_path):
        """静音：厂商给 2xx + 空文本（与火山 20000003 同义），不是失败。"""
        gateway.asr_body = ASR_SILENT
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        result = make_asr(gateway).transcribe_file(audio, language="zh")
        assert result.words == [] and result.segments == []

    def test_text_without_timestamps_is_an_error(self, gateway, tmp_path):
        """有文本却没时间戳必须报错——静默当静音会让有对白的片段被判成没语音。"""
        gateway.asr_body = ASR_TEXT_NO_TIMESTAMPS
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).transcribe_file(audio, language="zh")
        assert "verbose_json" in str(info.value)

    def test_non_json_body_is_reported(self, gateway, tmp_path):
        gateway.asr_body_text = "plain text, not json"
        audio = tmp_path / "chunk.wav"
        audio.write_bytes(b"RIFFfake")
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).transcribe_file(audio, language="zh")
        assert "不是合法 JSON" in str(info.value)


class TestOpenAiAsrProbe:
    def test_probe_passes_on_2xx(self, gateway):
        make_asr(gateway).probe()
        assert gateway.requests[0].path.endswith("/audio/transcriptions")
        assert 'name="file"; filename="probe.wav"' in gateway.requests[0].body

    def test_probe_reports_auth_failure(self, gateway):
        gateway.asr_status = 401
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).probe()
        assert "鉴权失败" in str(info.value)

    def test_probe_hints_base_url_on_404(self, gateway):
        gateway.asr_status = 404
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).probe()
        assert "/v1" in str(info.value)

    def test_probe_falls_back_to_models_when_sample_is_refused(self, gateway):
        """静音样片被拒（合规问题）→ 用 /models 验鉴权，并如实说明没验证模型。"""
        gateway.asr_status = 400
        gateway.api_status_message = "audio too short"
        transport = make_asr(gateway)
        transport.probe()
        assert transport.probe_note, "必须留下「只验证了鉴权」的说明，不能假装全测过"
        assert any(request.path.endswith("/models") for request in gateway.requests)

    def test_probe_fails_when_fallback_also_unavailable(self, gateway):
        gateway.asr_status = 400
        gateway.api_status_message = "audio too short"
        gateway.models_status = 404
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).probe()
        assert "连通性测试未通过" in str(info.value)

    def test_probe_does_not_swallow_other_400s(self, gateway):
        """400 但厂商说明与"样片不合规"无关（例如模型名写错）→ 原样报出。"""
        gateway.asr_status = 400
        gateway.api_status_message = "model whisper-9 not found"
        with pytest.raises(ProviderError) as info:
            make_asr(gateway).probe()
        assert "not found" in info.value.vendor_message