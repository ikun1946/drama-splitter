"""Google Gemini ``generateContent`` 协议适配器（语义判断）。"""

from __future__ import annotations

from .cloud_http import post_json
from .model_info import ModelInfo, estimate_max_input_chars
from .providers import (
    DEFAULT_TIMEOUT,
    ProviderError,
    egress_host,
    gemini_generate_url,
)

__all__ = ["GeminiChatTransport"]


class GeminiChatTransport:
    """Gemini 原生 ``generateContent`` 适配器。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        label: str = "",
        use_system_proxy: bool = False,
        temperature: float = 0.1,
        max_tokens: int = 160,
        info: ModelInfo | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._label = (label or "").strip() or "Google Gemini"
        self._use_system_proxy = use_system_proxy
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._info = info or ModelInfo(name=model)

    def describe(self) -> str:
        return f"{self._label} {self._model} @ {egress_host(self._base_url)}"

    def complete(self, prompt: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        data, _ = self._post(
            {
                "contents": [{"parts": [{"text": self._fit(prompt)}]}],
                "generationConfig": {
                    "temperature": self._temperature,
                    "maxOutputTokens": self._max_tokens,
                },
            },
            timeout=timeout,
        )
        return self._extract_text(data)

    def probe(self, *, timeout: float = 8.0) -> None:
        self._post(
            {
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1},
            },
            timeout=timeout,
        )

    def _fit(self, prompt: str) -> str:
        limit = estimate_max_input_chars(self._info.context_window)
        if limit <= 0 or len(prompt) <= limit:
            return prompt
        dropped = len(prompt) - limit
        return f"（前 {dropped} 字因超出上下文窗口被截断）\n" + prompt[-limit:]

    def _post(self, payload: dict, *, timeout: float) -> tuple[dict, object]:
        if not self._base_url:
            raise ProviderError("未配置接口地址（base_url）")
        if not self._api_key:
            raise ProviderError("缺少 API Key")
        if not self._model:
            raise ProviderError("未配置模型名（judge_model）")
        return post_json(
            gemini_generate_url(self._base_url, self._model),
            payload,
            {"x-goog-api-key": self._api_key},
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0] if isinstance(candidates[0], dict) else {}
            content = first.get("content") if isinstance(first.get("content"), dict) else {}
            parts = content.get("parts")
            if isinstance(parts, list):
                text = "".join(
                    str(part.get("text") or "")
                    for part in parts
                    if isinstance(part, dict)
                )
                if text:
                    return text
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise ProviderError(
                "厂商返回错误",
                vendor_message=str(error.get("message")),
            )
        raise ProviderError("返回体缺少 candidates 文本")
