"""厂商档案与后端偏好的纯逻辑测试（不联网、不发任何请求）。

外发开关是这一层最要紧的语义：`enabled`（允许被选中）与 `allow_upload`（外发许可）
必须同时为真，否则宁可本地慢一点，也不能悄悄把素材发出去。

第二组语义是**协议**：厂商是数据、协议是信封。这里固化三件事——
① 旧配置零迁移（没有协议字段的 volc 档案仍然能用火山转写）；
② 能力判据按协议走（OpenAI 兼容转写只看 base_url，不需要 endpoint / resource_id）；
③ 预置目录自身合法（id 唯一且合法、volc 仍在第一位且 judge_model 为空）。
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from app.core.app_config import LOCAL_BACKEND, AppConfig
from app.core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    DEFAULT_PROFILES,
    PROTOCOLS_BY_CAP,
    PROTO_ANTHROPIC,
    PROTO_DISABLED,
    PROTO_GEMINI,
    PROTO_LABELS,
    PROTO_OPENAI,
    PROTO_VOLC_ARK,
    ProviderProfile,
    audio_transcriptions_url,
    block_reason,
    chat_completions_url,
    describe_egress,
    egress_host,
    gemini_generate_url,
    merge_profiles,
    messages_url,
    missing_config_reason,
    resolve_api_key,
    suggest_custom_id,
    validate_profile,
)


class TestDefaultProfile:
    def test_builtin_volc_profile_exists(self):
        profile = DEFAULT_PROFILES[0]
        assert profile.id == "volc"
        assert "openspeech" in profile.asr_endpoint
        assert profile.asr_resource_id == "volc.bigasr.auc_turbo"

    def test_default_profile_refuses_to_send_anything(self):
        """内置档案必须是"关着的"：默认零外发。"""
        profile = DEFAULT_PROFILES[0]
        assert not profile.enabled
        assert not profile.allow_upload
        assert not profile.egress_allowed(CAP_ASR)
        assert not profile.egress_allowed(CAP_JUDGE)

    def test_capabilities_depend_on_configured_fields(self):
        profile = DEFAULT_PROFILES[0]
        assert profile.supports(CAP_ASR), "内置档案配了转写接口"
        assert not profile.supports(CAP_JUDGE), "未填 judge_model 就不算支持语义判断"
        with pytest.raises(ValueError):
            profile.supports("video")


class TestProfileSerialization:
    def test_roundtrip(self):
        profile = DEFAULT_PROFILES[0].with_updates(
            judge_model="ep-20260101", api_key="sk-test", enabled=True,
            allow_upload=True, use_system_proxy=True,
        )
        again = type(profile).from_json(profile.to_json())
        assert again == profile

    def test_custom_models_roundtrip_and_tolerate_garbage(self):
        """手填的模型名要落盘；脏数据（空串/重复/非字符串）要清掉。"""
        profile = DEFAULT_PROFILES[0].with_updates(
            custom_models=("ep-20260101", "my-alias")
        )
        again = type(profile).from_json(profile.to_json())
        assert again.custom_models == ("ep-20260101", "my-alias")

        cleaned = type(profile).from_json(
            {"id": "volc", "custom_models": ["  x  ", "", 7, "x", "y"]}
        )
        assert cleaned.custom_models == ("x", "y"), "去空、去重、丢非字符串"

        emptied = type(profile).from_json({"id": "volc", "custom_models": []})
        assert emptied.custom_models == (), "空清单是合法值：删光了不该回落基底"

    def test_model_choices_merge_fetched_and_custom(self):
        """候选清单 = 接口拉回来的（在前）+ 手填的（补后），去重保序。"""
        profile = DEFAULT_PROFILES[0].with_updates(custom_models=("ep-x", "dup"))
        assert profile.model_choices(["dup", "gpt-4o"]) == ("dup", "gpt-4o", "ep-x")
        assert profile.model_choices() == ("ep-x", "dup"), "没有清单时只剩手填的"

    def test_from_json_tolerates_garbage(self):
        """配置坏了必须回落默认值，而不是让启动失败。"""
        profile = type(DEFAULT_PROFILES[0]).from_json(
            {"id": "volc", "enabled": "yes", "api_key": 123, "judge_model": None}
        )
        assert profile.enabled is False, "非布尔的开关按默认（关）处理"
        assert profile.api_key == ""
        assert profile.judge_model == ""
        assert profile.base_url == DEFAULT_PROFILES[0].base_url

    def test_merge_keeps_defaults_and_accepts_new_vendor(self):
        merged = merge_profiles(
            [
                {"id": "volc", "judge_model": "ep-x", "enabled": True},
                {"id": "aliyun", "label": "阿里云", "base_url": "https://example.invalid"},
                {"id": "", "label": "没有 id 的脏数据"},
                "不是 dict",
            ]
        )
        by_id = {profile.id: profile for profile in merged}
        assert by_id["volc"].judge_model == "ep-x", "用户字段应覆盖内置默认"
        assert by_id["volc"].asr_endpoint == DEFAULT_PROFILES[0].asr_endpoint, "未写的字段保留默认"
        assert "aliyun" in by_id, "自定义厂商档案原样保留（加厂商不改这里）"
        assert len(merged) == len(DEFAULT_PROFILES) + 1, f"脏数据应被丢弃：{list(by_id)}"

    def test_registry_reads_from_config(self):
        config = AppConfig()
        config.providers = [{"id": "volc", "judge_model": "ep-y"}]
        registry = config.profiles()
        assert registry.get("volc").judge_model == "ep-y"
        assert registry.get("不存在") is None


class TestApiKey:
    def test_env_overrides_profile(self, monkeypatch):
        profile = DEFAULT_PROFILES[0].with_updates(api_key="from-file")
        monkeypatch.setenv("DRAMA_VOLC_API_KEY", "from-env")
        assert resolve_api_key(profile) == "from-env"

    def test_profile_used_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("DRAMA_VOLC_API_KEY", raising=False)
        profile = DEFAULT_PROFILES[0].with_updates(api_key=" from-file ")
        assert resolve_api_key(profile) == "from-file"

    def test_empty_when_neither(self, monkeypatch):
        monkeypatch.delenv("DRAMA_VOLC_API_KEY", raising=False)
        assert resolve_api_key(DEFAULT_PROFILES[0]) == ""


class TestEgressDisclosure:
    def test_host_extraction(self):
        assert egress_host("https://ark.cn-beijing.volces.com/api/v3") == "ark.cn-beijing.volces.com"
        assert egress_host("not-a-url") == "not-a-url"

    def test_text_states_scope_and_hosts(self):
        """外发告知必须说清"发什么、发给谁"——这是界面/弹窗/日志共用的唯一文案。"""
        profile = DEFAULT_PROFILES[0].with_updates(judge_model="ep-x")
        text = describe_egress(profile)
        assert "上传整条音轨" in text
        assert "openspeech.bytedance.com" in text
        assert "不含画面" in text
        assert "ark.cn-beijing.volces.com" in text

    def test_partial_capabilities(self):
        text = describe_egress(DEFAULT_PROFILES[0], (CAP_JUDGE,))
        assert "语义判断" not in text, "未配置 judge_model 时不应宣称会发文本"
        assert "尚未配置可用的云端能力" in text


class TestAppConfigPersistence:
    def test_defaults_are_local(self):
        config = AppConfig()
        assert config.backend_for(CAP_ASR) == LOCAL_BACKEND
        assert config.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        assert config.fallback_to_local is True
        assert config.active_profile(CAP_JUDGE) is None

    def test_save_and_load_roundtrip(self, tmp_path):
        target = tmp_path / "config.json"
        config = AppConfig()
        config.providers = [
            {"id": "volc", "judge_model": "ep-z", "enabled": True, "allow_upload": True}
        ]
        config.backend_prefs = {CAP_ASR: LOCAL_BACKEND, CAP_JUDGE: "volc"}
        config.fallback_to_local = False
        config.save(target)

        again = AppConfig.load(target)
        assert again.backend_prefs == {CAP_ASR: LOCAL_BACKEND, CAP_JUDGE: "volc"}
        assert again.fallback_to_local is False
        assert again.providers[0]["judge_model"] == "ep-z"

    def test_broken_config_degrades_to_defaults(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text(
            json.dumps(
                {
                    "providers": [{"no_id": 1}, "x", 5],
                    "backend_prefs": {"judge": 42, "asr": "  "},
                    "fallback_to_local": "yes",
                }
            ),
            encoding="utf-8",
        )
        config = AppConfig.load(target)
        assert config.providers == [], "没有 id 的条目一律丢弃"
        assert config.backend_prefs == {CAP_ASR: LOCAL_BACKEND, CAP_JUDGE: LOCAL_BACKEND}
        assert config.fallback_to_local is True

    def test_active_profile_requires_supported_capability(self):
        config = AppConfig()
        config.providers = [
            {"id": "volc", "enabled": True, "allow_upload": True}   # 未填 judge_model
        ]
        config.backend_prefs = {CAP_ASR: "volc", CAP_JUDGE: "volc"}
        assert config.active_profile(CAP_JUDGE) is None, "没配该能力就不该被选中"
        assert config.active_profile(CAP_ASR) is not None, "转写接口是内置配好的"

    def test_active_profile_ignores_switches_on_purpose(self):
        """选没选 与 允不允许外发 是两件事：前者决定日志措辞，后者决定是否真发。"""
        config = AppConfig()
        config.providers = [{"id": "volc", "judge_model": "ep-a"}]
        config.backend_prefs = {CAP_ASR: LOCAL_BACKEND, CAP_JUDGE: "volc"}
        profile = config.active_profile(CAP_JUDGE)
        assert profile is not None
        assert profile.enabled is False and profile.allow_upload is False


# ---------------------------------------------------------------------------
# 协议：厂商是数据、协议是信封
# ---------------------------------------------------------------------------


class TestProtocols:
    def test_volc_uses_openai_for_judge_and_volc_ark_for_asr(self):
        """协议是信封的名字，不是厂商名：方舟的 chat 就是 OpenAI 形态。"""
        volc = DEFAULT_PROFILES[0]
        assert volc.judge_protocol == PROTO_OPENAI
        assert volc.asr_protocol == PROTO_VOLC_ARK
        assert PROTO_VOLC_ARK not in PROTOCOLS_BY_CAP[CAP_JUDGE], (
            "语义判断不该有「火山方舟」这个协议值——同一件事有两种名字必然漂移"
        )

    def test_supports_openai_asr_only_needs_base_url(self):
        profile = ProviderProfile(
            id="custom", label="自定义", base_url="https://api.example.com/v1",
            judge_model="m", judge_protocol=PROTO_OPENAI,
            asr_model_name="whisper-1", asr_protocol=PROTO_OPENAI,
        )
        assert profile.supports(CAP_ASR), "路径由协议约定，只要 base_url 在就算配好"
        assert profile.supports(CAP_JUDGE)

    def test_disabled_protocol_never_supports(self):
        profile = ProviderProfile(
            id="custom", label="自定义", base_url="https://api.example.com/v1",
            judge_model="m", asr_model_name="whisper-1",
        )
        assert not profile.supports(CAP_ASR), "协议停用就不算支持，哪怕字段都填了"
        assert not profile.supports(CAP_JUDGE)

    def test_volc_ark_asr_still_needs_endpoint_and_resource(self):
        base = dict(
            id="custom", label="自定义", asr_protocol=PROTO_VOLC_ARK,
            asr_model_name="bigmodel",
        )
        assert not ProviderProfile(**base).supports(CAP_ASR)
        assert not ProviderProfile(
            **base, asr_endpoint="https://x.example.com/asr"
        ).supports(CAP_ASR), "缺资源 ID 不算支持"
        assert ProviderProfile(
            **base, asr_endpoint="https://x.example.com/asr", asr_resource_id="r"
        ).supports(CAP_ASR)

    def test_old_config_zero_migration(self):
        """改造前的 volc 配置没有协议字段：合并后必须仍能用火山转写。"""
        old = {
            "id": "volc",
            "api_key": "sk-old",
            "enabled": True,
            "allow_upload": True,
        }
        merged = {profile.id: profile for profile in merge_profiles([old])}
        volc = merged["volc"]
        assert volc.judge_protocol == PROTO_OPENAI
        assert volc.asr_protocol == PROTO_VOLC_ARK
        assert volc.asr_endpoint == "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
        assert volc.supports(CAP_ASR), "转写能力与改造前一致"
        assert not volc.supports(CAP_JUDGE), "用户没填 judge_model，仍不算支持"

    def test_unknown_custom_id_does_not_inherit_volc(self):
        """自定义厂商不得继承火山的地址与资源 ID（否则会凭空「支持火山转写」）。"""
        profile = ProviderProfile.from_json({"id": "my-gateway", "label": "自建网关"})
        assert profile.asr_endpoint == ""
        assert profile.asr_resource_id == ""
        assert profile.judge_protocol == PROTO_DISABLED
        assert profile.asr_protocol == PROTO_DISABLED
        assert not profile.supports(CAP_ASR)

    def test_bad_protocol_value_falls_back_to_base_not_disabled(self):
        """协议写错一个字，不能静默把能力关掉——回落基底，让报错在连通性测试里出现。"""
        profile = ProviderProfile.from_json(
            {"id": "volc", "judge_protocol": "openai-compatible"},
            fallback=DEFAULT_PROFILES[0],
        )
        assert profile.judge_protocol == PROTO_OPENAI

    def test_protocol_roundtrip(self):
        profile = DEFAULT_PROFILES[0].with_updates(
            judge_model="ep-x", judge_protocol=PROTO_OPENAI, asr_protocol=PROTO_VOLC_ARK
        )
        again = ProviderProfile.from_json(profile.to_json())
        assert again == profile

    def test_native_text_protocols_roundtrip(self):
        for protocol in (PROTO_ANTHROPIC, PROTO_GEMINI):
            profile = ProviderProfile(
                id="custom",
                label="自定义接入",
                base_url="https://api.example.com/v1",
                judge_model="model-x",
                judge_protocol=protocol,
            )
            assert ProviderProfile.from_json(profile.to_json()) == profile
            assert profile.supports(CAP_JUDGE)


class TestProtocolUrls:
    def test_urls_are_built_from_base_url(self):
        assert chat_completions_url("https://api.example.com/v1/") == (
            "https://api.example.com/v1/chat/completions"
        )
        assert audio_transcriptions_url("https://api.example.com/v1") == (
            "https://api.example.com/v1/audio/transcriptions"
        )
        assert chat_completions_url("") == "", "没地址就没有目标 URL，不编一个出来"

    def test_egress_host_for_openai_asr_comes_from_base_url(self):
        """OpenAI 兼容转写没有 asr_endpoint：外发主机名必须来自 base_url。"""
        profile = ProviderProfile(
            id="custom", label="自定义", base_url="https://api.example.com/v1",
            asr_model_name="whisper-1", asr_protocol=PROTO_OPENAI,
        )
        text = describe_egress(profile)
        assert "api.example.com" in text
        assert "（未配置地址）" not in text

    def test_anthropic_and_gemini_use_their_own_paths(self):
        assert messages_url("https://api.anthropic.com/v1/") == (
            "https://api.anthropic.com/v1/messages"
        )
        assert gemini_generate_url(
            "https://generativelanguage.googleapis.com/v1beta",
            "models/gemini-2.5-flash",
        ) == (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-2.5-flash:generateContent"
        )


class TestCustomId:
    def test_generates_a_stable_unique_id_without_asking_the_user(self):
        assert suggest_custom_id("Example Gateway") == "example-gateway"
        assert suggest_custom_id("自定义接入", ["custom"]) == "custom-2"
        used = ["custom", "custom-2"]
        assert suggest_custom_id("中文名称", used) == "custom-3"


class TestBlockReasons:
    def test_judge_protocol_missing(self):
        profile = ProviderProfile(id="custom", label="自定义", base_url="https://x.example.com")
        reason = missing_config_reason(profile, CAP_JUDGE)
        assert reason and "未选择语义判断协议" in reason

    def test_openai_asr_missing_model_name(self):
        profile = ProviderProfile(
            id="custom", label="自定义", base_url="https://x.example.com",
            asr_protocol=PROTO_OPENAI,
        )
        reason = missing_config_reason(profile, CAP_ASR)
        assert reason and "转写模型名" in reason

    def test_volc_asr_reasons_are_split(self):
        endpoint_only = ProviderProfile(
            id="custom", label="自定义", asr_protocol=PROTO_VOLC_ARK,
            asr_endpoint="https://x.example.com/asr",
        )
        assert "资源 ID" in (missing_config_reason(endpoint_only, CAP_ASR) or "")

    def test_block_reason_prefixes_switches(self):
        profile = ProviderProfile(
            id="custom", label="自定义", base_url="https://x.example.com",
            judge_model="m", judge_protocol=PROTO_OPENAI,
        )
        assert "未启用" in (block_reason(profile, CAP_JUDGE) or "")
        profile = profile.with_updates(enabled=True)
        assert "允许外发" in (block_reason(profile, CAP_JUDGE) or "")
        profile = profile.with_updates(allow_upload=True)
        assert "API Key" in (block_reason(profile, CAP_JUDGE) or "")
        assert block_reason(profile.with_updates(api_key="sk-x"), CAP_JUDGE) is None


class TestValidateProfile:
    def _good(self, **changes) -> ProviderProfile:
        base = ProviderProfile(
            id="my-gateway", label="自建网关", base_url="https://api.example.com/v1",
            judge_model="m", judge_protocol=PROTO_OPENAI,
        )
        return base.with_updates(**changes)

    def test_accepts_valid_profile(self):
        assert validate_profile(self._good()) == []

    def test_rejects_bad_id(self):
        assert validate_profile(self._good(id="Bad-ID")), "大写不合法"
        assert validate_profile(self._good(id="中文")), "非 ASCII 不合法"
        assert validate_profile(self._good(id="-x")), "首字符不能是短横线"
        assert validate_profile(self._good(id="")) , "空 id 不合法"

    def test_rejects_duplicate_id(self):
        problems = validate_profile(self._good(), existing_ids=["my-gateway"])
        assert any("已被占用" in item for item in problems)

    def test_rejects_empty_label_and_bad_url(self):
        assert validate_profile(self._good(label="  ")), "名称不能为空"
        assert validate_profile(self._good(base_url="ftp://example.com")), "只认 http(s)"
        assert validate_profile(self._good(base_url="example.com")), "缺 scheme 不合法"

    def test_requires_model_name_for_chosen_asr_protocol(self):
        problems = validate_profile(
            self._good(asr_protocol=PROTO_OPENAI, asr_model_name="")
        )
        assert any("asr_model_name" in item for item in problems)

    def test_missing_judge_model_is_not_a_structural_error(self):
        """选了语义判断协议但没填模型名：可以保存，由 missing_config_reason 说明原因。

        预置的方舟档案正是这种状态（等用户填推理接入点 ID），保存时硬拦反而挡住它。
        """
        profile = self._good(judge_model="")
        assert validate_profile(profile) == []
        assert not profile.supports(CAP_JUDGE)
        assert "judge_model" in (missing_config_reason(profile, CAP_JUDGE) or "")

    def test_rejects_protocol_not_allowed_for_capability(self):
        problems = validate_profile(self._good(judge_protocol=PROTO_VOLC_ARK))
        assert any("不支持的协议" in item for item in problems)

    def test_volc_ark_requires_endpoint_and_resource(self):
        problems = validate_profile(
            self._good(
                judge_protocol=PROTO_DISABLED,
                asr_protocol=PROTO_VOLC_ARK,
                asr_endpoint="",
                asr_resource_id="",
            )
        )
        joined = "；".join(problems)
        assert "asr_endpoint" in joined
        assert "asr_resource_id" in joined


class TestPresetCatalog:
    """预置目录自身的合法性：加厂商时最容易在这里出纰漏。"""

    def test_ids_are_unique_and_legal(self):
        ids = [profile.id for profile in DEFAULT_PROFILES]
        assert len(ids) == len(set(ids)), f"id 重复：{ids}"
        for profile in DEFAULT_PROFILES:
            assert not validate_profile(
                profile, existing_ids=[item for item in ids if item != profile.id]
            ), f"预置档案自身不合法：{profile.id}"

    def test_volc_stays_first_and_judge_model_is_empty(self):
        assert DEFAULT_PROFILES[0].id == "volc", "下拉默认项与合并排序都依赖它"
        assert DEFAULT_PROFILES[0].judge_model == "", (
            "方舟的推理接入点 ID 必须用户自己填，不能预置一个假值"
        )

    def test_every_preset_declares_known_protocols(self):
        for profile in DEFAULT_PROFILES:
            assert profile.judge_protocol in PROTO_LABELS, profile.id
            assert profile.asr_protocol in PROTO_LABELS, profile.id
            assert profile.judge_protocol in PROTOCOLS_BY_CAP[CAP_JUDGE], profile.id
            assert profile.asr_protocol in PROTOCOLS_BY_CAP[CAP_ASR], profile.id

    def test_every_preset_ships_without_secrets_or_egress(self):
        for profile in DEFAULT_PROFILES:
            assert profile.api_key == "", f"{profile.id} 不该带 Key"
            assert profile.enabled is False and profile.allow_upload is False
            assert profile.supports(CAP_JUDGE) is False or profile.judge_model

    def test_urls_are_http_s(self):
        for profile in DEFAULT_PROFILES:
            if profile.base_url:
                parsed = urlparse(profile.base_url)
                assert parsed.scheme in ("http", "https"), profile.id
                assert parsed.hostname, profile.id
            if profile.asr_protocol == PROTO_VOLC_ARK:
                assert profile.asr_resource_id, f"{profile.id} 缺资源 ID"

    def test_catalog_covers_more_than_one_vendor(self):
        assert len(DEFAULT_PROFILES) >= 10, "用户要的是「大部分厂商都能加进来」"
