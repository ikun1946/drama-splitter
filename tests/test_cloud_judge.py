"""云端语义判断：假服务器跑通映射、错误映射、降级与回退。

**绝不碰真厂商接口**：本文件里的 "Ark" 是本机一个假 HTTP 服务器，请求与响应
都由测试脚本控制。真机验证是手工关卡（`tools/cloud_smoke.py`，需用户 Key），
不放在 CI 里。

这里断言的是几条"错了会静默出事"的语义：

- 未启用 / 未勾选外发 / 缺 Key → **一个请求都不发**；
- 厂商错误（401 等）→ 变成带厂商说明与 logid 的可读原因，且**不让分析失败**；
- 云端中途失败 → 只切换一次到备选，之后不再撞墙。
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.app_config import LOCAL_BACKEND, AppConfig
from app.core.cloud_openai import OpenAiChatTransport
from app.core.providers import DEFAULT_PROFILES, ProviderProfile
from app.core.semantic import (
    VERDICT_ACCEPTABLE,
    VERDICT_GOOD,
    VERDICT_UNPARSEABLE,
    CloudLlmJudge,
    JudgmentRequest,
    JudgmentVerdict,
    ModelUnavailableError,
    select_judge,
)

API_KEY = "sk-test-key"


class FakeArk:
    """最小的本地假 Ark：记录请求，按脚本回响应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.content = json.dumps(
            {"verdict": "good_cut", "evidence": ["句末收尾"], "risks": []},
            ensure_ascii=False,
        )
        self.error_body: dict = {"error": {"message": "invalid api key"}}
        self.logid = "logid-1234"
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
                        "path": self.path,
                        "payload": payload,
                        "authorization": self.headers.get("Authorization", ""),
                    }
                )
                if outer.status == 200:
                    body = json.dumps(
                        {"choices": [{"message": {"content": outer.content}}]},
                        ensure_ascii=False,
                    ).encode("utf-8")
                else:
                    body = json.dumps(outer.error_body, ensure_ascii=False).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Request-Id", outer.logid)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 静音
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def fake_ark():
    server = FakeArk()
    server.start()
    yield server
    server.stop()


def make_profile(server: FakeArk | None = None, **overrides) -> ProviderProfile:
    base = DEFAULT_PROFILES[0].with_updates(
        base_url=server.base_url if server else "http://127.0.0.1:1",
        judge_model="ep-test",
        api_key=API_KEY,
        enabled=True,
        allow_upload=True,
    )
    return replace(base, **overrides) if overrides else base


def make_config(profile: ProviderProfile, *, fallback: bool = True) -> AppConfig:
    config = AppConfig()
    config.providers = [profile.to_json()]
    config.backend_prefs = {"asr": LOCAL_BACKEND, "judge": profile.id}
    config.fallback_to_local = fallback
    return config


def make_request(**overrides) -> JudgmentRequest:
    defaults = dict(
        candidate_time=Fraction(30),
        candidate_frame=750,
        text_before="他必须先拿到钱。",
        text_after="那就这么定了。",
        gap_after=Fraction(1),
        ends_with_question=False,
        near_shot_cut=False,
        rule_score=0.5,
        strategy="story",
    )
    defaults.update(overrides)
    return JudgmentRequest(**defaults)


def make_judge(server: FakeArk) -> CloudLlmJudge:
    return CloudLlmJudge(
        OpenAiChatTransport(server.base_url, API_KEY, "ep-test", label="火山方舟"),
        profile_id="volc",
        model="ep-test",
    )


class TestTransportAndJudge:
    def test_judge_sends_bearer_and_model_and_parses_verdict(self, fake_ark):
        judge = make_judge(fake_ark)
        verdict = judge.judge(make_request())

        assert verdict.verdict == VERDICT_GOOD
        assert verdict.judge_id == "cloud:volc/ep-test", "结果必须能回答'谁算的'"
        assert len(fake_ark.requests) == 1
        sent = fake_ark.requests[0]
        assert sent["path"] == "/chat/completions"
        assert sent["authorization"] == f"Bearer {API_KEY}"
        assert sent["payload"]["model"] == "ep-test"
        prompt = sent["payload"]["messages"][0]["content"]
        assert "他必须先拿到钱。" in prompt, "发出去的应当只有候选点前后台词"
        assert '"verdict"' in prompt, "提示词里的输出格式示例必须完整送到模型"
        assert "{context}" not in prompt

    def test_unparseable_output_is_marked_not_guessed(self, fake_ark):
        fake_ark.content = "我觉得还行吧，说不好。"
        verdict = make_judge(fake_ark).judge(make_request())
        assert verdict.verdict == VERDICT_UNPARSEABLE
        assert verdict.risks, "解析失败必须给出风险说明，不能猜一个等级"

    def test_invalid_request_never_hits_the_network(self, fake_ark):
        verdict = make_judge(fake_ark).judge(
            make_request(candidate_time=Fraction(-1), candidate_frame=-1)
        )
        assert verdict.verdict == VERDICT_UNPARSEABLE
        assert fake_ark.requests == [], "请求不合法不应外发"

    def test_auth_error_carries_vendor_message_and_logid(self, fake_ark):
        fake_ark.status = 401
        judge = make_judge(fake_ark)
        with pytest.raises(ModelUnavailableError) as excinfo:
            judge.judge(make_request())
        text = str(excinfo.value)
        assert "鉴权失败" in text
        assert "invalid api key" in text, "厂商说明必须带回用户可见的日志"
        assert "logid-1234" in text

    def test_probe_failure_records_reason(self, fake_ark):
        fake_ark.status = 403
        judge = make_judge(fake_ark)
        assert judge.available() is False
        assert "鉴权失败" in judge.probe_error
        assert judge.available() is False, "可重复探测，不应因此抛异常"

    def test_probe_success_clears_error(self, fake_ark):
        judge = make_judge(fake_ark)
        assert judge.available() is True
        assert judge.probe_error == ""
        assert len(fake_ark.requests) == 1, "探测就是一次最小请求"
        assert fake_ark.requests[0]["payload"]["max_tokens"] == 1


class TestSelectionAndZeroEgress:
    def test_disabled_profile_sends_nothing(self, fake_ark, monkeypatch):
        """未启用的档案即使被选中，也必须零请求。"""
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: None)
        config = make_config(make_profile(fake_ark, enabled=False))
        judge, notes = select_judge(config)

        assert judge is None
        assert fake_ark.requests == [], "未启用却发了请求"
        assert any("未启用" in note for note in notes), notes

    def test_upload_switch_off_sends_nothing(self, fake_ark, monkeypatch):
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: None)
        config = make_config(make_profile(fake_ark, allow_upload=False))
        judge, notes = select_judge(config)

        assert judge is None
        assert fake_ark.requests == []
        assert any("允许外发素材" in note for note in notes), notes

    def test_missing_key_points_at_env_var(self, fake_ark, monkeypatch):
        monkeypatch.delenv("DRAMA_VOLC_API_KEY", raising=False)
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: None)
        config = make_config(make_profile(fake_ark, api_key=""))
        judge, notes = select_judge(config)

        assert judge is None
        assert fake_ark.requests == []
        assert any("DRAMA_VOLC_API_KEY" in note for note in notes), notes

    def test_env_var_key_is_used(self, fake_ark, monkeypatch):
        monkeypatch.setenv("DRAMA_VOLC_API_KEY", API_KEY)
        config = make_config(make_profile(fake_ark, api_key=""))
        judge, notes = select_judge(config)
        assert judge is not None
        assert judge.judge(make_request()).verdict == VERDICT_GOOD
        assert fake_ark.requests[0]["authorization"] == f"Bearer {API_KEY}"

    def test_selected_cloud_judge_discloses_egress(self, fake_ark):
        judge, notes = select_judge(make_config(make_profile(fake_ark)))
        assert judge is not None
        joined = "\n".join(notes)
        assert "火山方舟" in joined
        assert "不含画面" in joined, "外发告知必须说清只发文本"
        assert "127.0.0.1" in joined, "要写明发往哪台主机"


class _FixedJudge:
    """备选判定器替身：固定返回 acceptable。"""

    def __init__(self) -> None:
        self.calls = 0
        self.judge_id = "fixed-local"

    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return "固定本地判定"

    def judge(self, request) -> JudgmentVerdict:
        self.calls += 1
        return JudgmentVerdict(verdict=VERDICT_ACCEPTABLE, judge_id=self.judge_id)


class TestFallback:
    def test_auth_failure_falls_back_to_local(self, fake_ark, monkeypatch):
        fallback = _FixedJudge()
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: fallback)
        fake_ark.status = 401

        judge, notes = select_judge(make_config(make_profile(fake_ark)))
        assert judge is not None

        assert judge.available() is True, "探测失败后应换到可用的备选"
        verdict = judge.judge(make_request())
        assert verdict.verdict == VERDICT_ACCEPTABLE
        assert fallback.calls == 1
        joined = "\n".join(notes)
        assert "已切换" in joined and "鉴权失败" in joined, notes

    def test_switch_happens_once_then_stops_calling_cloud(self, fake_ark, monkeypatch):
        fallback = _FixedJudge()
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: fallback)

        judge, notes = select_judge(make_config(make_profile(fake_ark)))
        assert judge.available() is True          # 1 次探测
        fake_ark.status = 429                     # 探测之后云端开始限流
        judge.judge(make_request())               # 第 1 次真请求失败 → 切换
        calls_after_switch = len(fake_ark.requests)
        judge.judge(make_request())
        judge.judge(make_request())

        assert len(fake_ark.requests) == calls_after_switch, "切换后不应再撞云端"
        # 3 次 = 切换那一轮的在途请求也交给备选（不白丢一次判断）+ 之后两次
        assert fallback.calls == 3
        assert any("已切换" in note for note in notes), notes

    def test_cloud_only_mode_degrades_without_crashing(self, fake_ark, monkeypatch):
        """仅云端模式：不回退，但也不能让整轮分析失败。"""
        monkeypatch.setattr(
            "app.core.semantic.local_judge",
            lambda **_: pytest.fail("仅云端模式不应构造本地判定器"),
        )
        fake_ark.status = 500
        judge, notes = select_judge(make_config(make_profile(fake_ark), fallback=False))

        assert judge is not None
        assert judge.available() is False
        verdict = judge.judge(make_request())
        assert verdict.verdict == VERDICT_UNPARSEABLE
        assert verdict.risks, "必须留下原因，而不是静默当作通过"
        assert any("已切换" in note for note in notes), notes

    def test_probe_failure_without_local_available_degrades(self, fake_ark, monkeypatch):
        monkeypatch.setattr("app.core.semantic.local_judge", lambda **_: None)
        fake_ark.status = 401
        judge, notes = select_judge(make_config(make_profile(fake_ark)))

        assert judge is not None
        assert judge.available() is False
        assert "已回退" in judge.describe()
        assert any("已切换" in note and "规则评分" in note for note in notes), notes