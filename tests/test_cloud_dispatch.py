"""按协议分派适配器：**加一家厂商 = 加一份数据**的机器化证明。

这一层的语义很具体：

- dispatch 的键是**协议**（不是厂商 id）——因此 `cloud.py` 里不允许出现任何厂商
  id 字面量，新造一个谁也没注册过的 id 也必须能造出 transport；
- 协议停用 → 返回 None（调用方据此回退本地），不静默换一家；
- 未知协议 → 也是 None，绝不猜。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pathlib

import pytest

from app.core import cloud
from app.core.cloud_anthropic import AnthropicChatTransport
from app.core.cloud_gemini import GeminiChatTransport
from app.core.cloud_openai import OpenAiAsrTransport, OpenAiChatTransport
from app.core.cloud_volc import VolcFlashAsrTransport
from app.core.providers import (
    DEFAULT_PROFILES,
    PROTO_ANTHROPIC,
    PROTO_DISABLED,
    PROTO_GEMINI,
    PROTO_OPENAI,
    PROTO_VOLC_ARK,
    ProviderProfile,
)


class _Gateway:
    """最小本地网关：记录请求并按 OpenAI / Anthropic / Gemini 形状回包。"""

    def __init__(self) -> None:
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": json.loads(body or b"{}"),
                    }
                )
                if self.path.endswith("/messages"):
                    payload = {
                        "content": [
                            {"type": "text", "text": '{"verdict":'},
                            {"type": "text", "text": ' "good_cut"}'},
                        ]
                    }
                else:
                    payload = {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": '{"verdict":'},
                                        {"text": ' "good_cut"}'},
                                    ]
                                }
                            }
                        ]
                    }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # noqa: A003 - 静音测试日志
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
    fake = _Gateway()
    yield fake
    fake.close()


def _profile(**changes) -> ProviderProfile:
    base = ProviderProfile(
        id="never-registered-anywhere",
        label="谁也没注册过的厂商",
        base_url="https://api.example.com/v1",
        asr_endpoint="https://asr.example.com/api",
        asr_resource_id="res-1",
        asr_model_name="whisper-1",
        judge_model="m",
        api_key="sk-x",
        judge_protocol=PROTO_OPENAI,
        asr_protocol=PROTO_OPENAI,
    )
    return base.with_updates(**changes)


class TestJudgeDispatch:
    def test_openai_protocol_builds_chat_transport(self):
        transport = cloud.build_judge_transport(_profile())
        assert isinstance(transport, OpenAiChatTransport)
        assert "谁也没注册过的厂商" in transport.describe(), "显示名要来自档案 label"

    def test_disabled_protocol_returns_none(self):
        assert cloud.build_judge_transport(_profile(judge_protocol=PROTO_DISABLED)) is None

    def test_unknown_protocol_returns_none(self):
        """协议写了个不认识的字符串：不许猜，回退本地由调用方处理。"""
        assert cloud.build_judge_transport(_profile(judge_protocol="magic")) is None

    def test_anthropic_protocol_builds_messages_transport(self):
        transport = cloud.build_judge_transport(_profile(judge_protocol=PROTO_ANTHROPIC))
        assert isinstance(transport, AnthropicChatTransport)

    def test_gemini_protocol_builds_generate_content_transport(self):
        transport = cloud.build_judge_transport(_profile(judge_protocol=PROTO_GEMINI))
        assert isinstance(transport, GeminiChatTransport)

    def test_anthropic_request_uses_messages_auth_and_parses_text(self, gateway):
        profile = _profile(
            base_url=gateway.base_url,
            judge_protocol=PROTO_ANTHROPIC,
            judge_model="claude-test",
        )
        transport = cloud.build_judge_transport(profile)
        assert transport is not None
        assert transport.complete("判断这个切点") == '{"verdict": "good_cut"}'
        request = gateway.requests[-1]
        assert request["path"].endswith("/v1/messages")
        assert request["headers"]["x-api-key"] == "sk-x"
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert request["body"]["model"] == "claude-test"
        assert request["body"]["messages"][0]["content"] == "判断这个切点"

    def test_gemini_request_uses_model_path_and_parses_parts(self, gateway):
        profile = _profile(
            base_url=gateway.base_url,
            judge_protocol=PROTO_GEMINI,
            judge_model="models/gemini-test",
        )
        transport = cloud.build_judge_transport(profile)
        assert transport is not None
        assert transport.complete("判断这个切点") == '{"verdict": "good_cut"}'
        request = gateway.requests[-1]
        assert request["path"].endswith("/models/gemini-test:generateContent")
        assert request["headers"]["x-goog-api-key"] == "sk-x"
        assert request["body"]["contents"][0]["parts"][0]["text"] == "判断这个切点"


class TestAsrDispatch:
    def test_openai_protocol_builds_openai_asr(self):
        transport = cloud.build_asr_transport(_profile())
        assert isinstance(transport, OpenAiAsrTransport)

    def test_volc_ark_protocol_builds_volc_asr(self):
        transport = cloud.build_asr_transport(_profile(asr_protocol=PROTO_VOLC_ARK))
        assert isinstance(transport, VolcFlashAsrTransport)

    def test_disabled_protocol_returns_none(self):
        assert cloud.build_asr_transport(_profile(asr_protocol=PROTO_DISABLED)) is None

    def test_unknown_protocol_returns_none(self):
        assert cloud.build_asr_transport(_profile(asr_protocol="magic")) is None


class TestModelListDispatch:
    def test_volc_ark_only_profile_says_manual(self):
        """方舟只认推理接入点 ID（不在 /models 里）：要说清"手动填写"，别假装没模型。"""
        from app.core.providers import ProviderError

        profile = _profile(
            id="volc", judge_protocol=PROTO_DISABLED, asr_protocol=PROTO_VOLC_ARK
        )
        with pytest.raises(ProviderError) as info:
            cloud.list_models(profile)
        assert "手动填写" in str(info.value)

    def test_openai_protocol_dispatches_to_listing(self, monkeypatch):
        """OpenAI 兼容协议要真的去拉清单（这里把底层的网络调用换掉）。"""
        seen = {}

        def fake_list(base_url, api_key, *, use_system_proxy=False):
            seen.update(base_url=base_url, api_key=api_key)
            return ["m1"]

        monkeypatch.setattr(cloud, "_list_openai_models", fake_list)
        profile = _profile(judge_protocol=PROTO_OPENAI, asr_protocol=PROTO_DISABLED)
        assert cloud.list_models(profile) == ["m1"]
        assert seen["base_url"] == "https://api.example.com/v1"
        assert seen["api_key"] == "sk-x"


class TestDispatchKnowsNoVendor:
    def test_cloud_module_has_no_vendor_id_literals(self):
        """`cloud.py` 里出现厂商 id 字面量，就意味着"加厂商要改代码"又回来了。"""
        source = pathlib.Path(cloud.__file__).read_text(encoding="utf-8")
        for profile in DEFAULT_PROFILES:
            assert f'"{profile.id}"' not in source, (
                f"cloud.py 里出现了厂商 id「{profile.id}」——分派必须按协议，不按厂商"
            )

    def test_every_preset_can_be_built(self):
        """预置目录里的每一家都要能按自己的协议造出适配器（或明确为"该能力停用"）。"""
        for profile in DEFAULT_PROFILES:
            judge = cloud.build_judge_transport(profile)
            asr = cloud.build_asr_transport(profile)
            assert judge is not None or profile.judge_protocol == PROTO_DISABLED, profile.id
            assert asr is not None or profile.asr_protocol == PROTO_DISABLED, profile.id
