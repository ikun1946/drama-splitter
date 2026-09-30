"""模型参数档案：自动推断、序列往返、以及它在真实请求里的作用。

重点覆盖三条硬规矩（见 `app/core/model_info.py` 的模块说明）：

1. **未知就留空** —— 认不出的模型不猜窗口，不猜思考等级；
2. **留空 = 不干预** —— 老配置没有参数时，请求形态与改造前逐字节一致；
3. **填了就生效** —— 窗口用于截断输入、思考等级作为 ``reasoning_effort`` 下发。
"""

from __future__ import annotations

import json

from app.core.model_info import (
    ModelInfo,
    estimate_max_input_chars,
    format_context,
    infer_model_info,
)
from app.core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    PROTO_OPENAI,
    ProviderProfile,
)


# ---------------------------------------------------------------------------
# 推断
# ---------------------------------------------------------------------------


class TestInference:
    def test_known_series_gets_window_and_modalities(self):
        info = infer_model_info("gpt-4o")
        assert info.context_window == 128_000
        assert "text" in info.modalities and "image" in info.modalities
        assert info.reasoning == "", "普通对话模型不该被填上 reasoning_effort"

    def test_longer_fragment_wins(self):
        """"gpt-4o-mini" 必须先于 "gpt-4o" 命中，否则小模型拿到大模型的规格。"""
        assert infer_model_info("gpt-4o-mini").context_window == 128_000
        assert infer_model_info("gpt-4.1-mini").context_window == 1_000_000
        assert infer_model_info("gpt-4.1").context_window == 1_000_000

    def test_reasoning_family_gets_effort(self):
        """.o 系列 / gpt-5 确实吃 reasoning_effort，才允许自动填。"""
        assert infer_model_info("o3-mini").reasoning == "medium"
        assert infer_model_info("gpt-5").reasoning == "medium"
        # deepseek-reasoner 自带思考，但不吃这个参数——填了可能 400
        assert infer_model_info("deepseek-reasoner").reasoning == ""

    def test_suffix_window_is_trusted(self):
        """名字里的 -128k / -1m 是厂商直接写的，最可信。"""
        assert infer_model_info("moonshot-v1-128k").context_window == 131_072
        assert infer_model_info("moonshot-v1-8k").context_window == 8_192
        assert infer_model_info("some-chat-1m").context_window == 1_000_000

    def test_audio_models_are_recognised(self):
        assert infer_model_info("whisper-1").can_transcribe()
        assert infer_model_info("Fun-ASR-Nano").can_transcribe()
        assert infer_model_info("bigmodel").can_transcribe(), "火山转写模型"

    def test_text_only_model_cannot_transcribe(self):
        info = infer_model_info("deepseek-chat")
        assert info.can_judge()
        assert not info.can_transcribe()

    def test_unknown_model_keeps_window_empty(self):
        """认不出的**不猜窗口**：猜大了没事，猜小了会静默丢掉台词证据。"""
        info = infer_model_info("some-vendor-internal-model-x9")
        assert info.context_window == 0
        assert info.reasoning == ""
        assert estimate_max_input_chars(info.context_window) == 0

    def test_embedding_model_is_flagged_as_unusable(self):
        info = infer_model_info("bge-large-zh-v1.5")
        assert info.can_judge(), "仍算文本模型"
        assert not info.can_transcribe()
        assert "嵌入" in info.note

    def test_blank_name(self):
        assert infer_model_info("").name == ""


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


class TestModelInfoJson:
    def test_round_trip(self):
        original = ModelInfo(
            name="gpt-5", context_window=400_000, reasoning="high",
            modalities=("text", "image"), source="manual", note="n",
        )
        assert ModelInfo.from_json(original.to_json()) == original

    def test_unknown_is_empty_and_inert(self):
        empty = ModelInfo(name="whatever")
        assert empty.is_empty()
        assert empty.supports_modality("audio"), "没填模态就不该拦"
        assert empty.can_transcribe() and empty.can_judge()

    # (说明, 输入, 期望值)；期望值 None 表示"解析不出档案"
    MALFORMED = (
        ("缺 name", {"context_window": 128_000}, None),
        ("不是 dict", ["gpt-4o"], None),
        ("负窗口", {"name": "m", "context_window": -5}, 0),
        ("非法思考等级", {"name": "m", "reasoning": "turbo"}, ""),
        ("非法模态", {"name": "m", "modalities": ["smell", "text"]}, ("text",)),
        ("模态去重", {"name": "m", "modalities": ["text", "text"]}, ("text",)),
    )

    def test_malformed_fields_fall_back(self):
        for label, payload, expected in self.MALFORMED:
            info = ModelInfo.from_json(payload)
            if expected is None:
                assert info is None, label
                continue
            assert info is not None, label
            if isinstance(expected, int):
                assert info.context_window == expected, label
            elif isinstance(expected, tuple):
                assert info.modalities == expected, label
            else:
                assert info.reasoning == expected, label

    def test_format_context(self):
        assert format_context(0) == "—"
        assert format_context(128_000) == "128k"
        assert format_context(1_000_000) == "1M"

    def test_max_input_chars_reserves_room_for_the_reply(self):
        limit = estimate_max_input_chars(1_000)
        assert 0 < limit < 1_000 * 2, "留了余量，且按字符/token 折算"


# ---------------------------------------------------------------------------
# 档案往返
# ---------------------------------------------------------------------------


def _profile(**changes) -> ProviderProfile:
    base = {
        "id": "p",
        "label": "厂",
        "base_url": "https://x.example.com/v1",
        "api_key": "k",
        "judge_protocol": PROTO_OPENAI,
        "asr_protocol": PROTO_OPENAI,
        "judge_model": "gpt-4o",
        "asr_model_name": "whisper-1",
    }
    return ProviderProfile(**{**base, **changes})


class TestProfileModelInfo:
    def test_old_config_without_model_info_is_inert(self):
        """老配置没有这个字段 → 空元组 → 能力判定与改造前一致。"""
        profile = ProviderProfile.from_json({
            "id": "p", "label": "厂", "base_url": "https://x.example.com/v1",
            "api_key": "k", "judge_protocol": PROTO_OPENAI,
            "judge_model": "某个没见过的模型",
        })
        assert profile.model_info == ()
        assert profile.info_for("某个没见过的模型").is_empty()
        assert profile.supports(CAP_JUDGE), "没配过参数就不该拦"

    def test_to_json_includes_model_info(self):
        payload = _profile(
            model_info=(ModelInfo(name="gpt-4o", context_window=128_000),)
        ).to_json()
        assert payload["model_info"][0]["name"] == "gpt-4o"
        assert json.dumps(payload, ensure_ascii=False)          # 必须可序列化

    def test_info_for_unknown_returns_empty(self):
        profile = _profile()
        assert profile.info_for("没配过的").is_empty()
        assert profile.info_for("").name == ""

    def test_with_model_info_replaces_and_prunes(self):
        profile = _profile()
        profile = profile.with_model_info(ModelInfo(name="gpt-4o", context_window=1))
        profile = profile.with_model_info(ModelInfo(name="gpt-4o", context_window=2))
        assert len(profile.model_info) == 1
        assert profile.info_for("gpt-4o").context_window == 2
        # 全空 = 用户清掉了，不留空记录
        profile = profile.with_model_info(ModelInfo(name="gpt-4o"))
        assert profile.model_info == ()

    def test_duplicate_names_in_config_last_wins(self):
        profile = ProviderProfile.from_json({
            "id": "p", "label": "厂",
            "model_info": [
                {"name": "m", "context_window": 100},
                {"name": "m", "context_window": 200},
            ],
        })
        assert len(profile.model_info) == 1
        assert profile.model_info[0].context_window == 200

    def test_modality_gates_capabilities_only_when_set(self):
        """模态只在意图明确时参与判定——空 = 不拦。"""
        permissive = _profile()
        assert permissive.supports(CAP_JUDGE) and permissive.supports(CAP_ASR)

        text_only = _profile(
            model_info=(ModelInfo(name="whisper-1", modalities=("text",)),)
        )
        assert not text_only.supports(CAP_ASR), "纯文本模型不能转写"
        assert text_only.supports(CAP_JUDGE)

        judge_blocked = _profile(
            model_info=(ModelInfo(name="gpt-4o", modalities=("audio",)),)
        )
        assert not judge_blocked.supports(CAP_JUDGE)
