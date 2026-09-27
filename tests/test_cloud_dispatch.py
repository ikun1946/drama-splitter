"""按协议分派适配器：**加一家厂商 = 加一份数据**的机器化证明。

这一层的语义很具体：

- dispatch 的键是**协议**（不是厂商 id）——因此 `cloud.py` 里不允许出现任何厂商
  id 字面量，新造一个谁也没注册过的 id 也必须能造出 transport；
- 协议停用 → 返回 None（调用方据此回退本地），不静默换一家；
- 未知协议 → 也是 None，绝不猜。
"""

from __future__ import annotations

import pathlib

from app.core import cloud
from app.core.cloud_openai import OpenAiAsrTransport, OpenAiChatTransport
from app.core.cloud_volc import VolcFlashAsrTransport
from app.core.providers import (
    DEFAULT_PROFILES,
    PROTO_DISABLED,
    PROTO_OPENAI,
    PROTO_VOLC_ARK,
    ProviderProfile,
)


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