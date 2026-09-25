"""厂商档案与后端偏好的纯逻辑测试（不联网、不发任何请求）。

外发开关是这一层最要紧的语义：`enabled`（允许被选中）与 `allow_upload`（外发许可）
必须同时为真，否则宁可本地慢一点，也不能悄悄把素材发出去。
"""

from __future__ import annotations

import json

import pytest

from app.core.app_config import LOCAL_BACKEND, AppConfig
from app.core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    DEFAULT_PROFILES,
    describe_egress,
    egress_host,
    merge_profiles,
    resolve_api_key,
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
        assert len(merged) == 2, f"脏数据应被丢弃：{list(by_id)}"

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