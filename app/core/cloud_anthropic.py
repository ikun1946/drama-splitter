"""Anthropic Messages 协议适配器（语义判断）。"""

from __future__ import annotations

from .cloud_http import post_json
from .model_info import ModelInfo, estimate_max_input_chars
from .providers import (
    DEFAULT_TIMEOUT,
    ProviderError,
    egress_host,
    messages_url,
)

__all__ = ["AnthropicChatTransport"]

#: Messages API 必须声明版本；固定值由 Anthropic 官方当前协议约定。
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicChatTransport:
    """Anthropic ``/v1/messages`` 适配器。"""

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
        self._label = (label or "").strip() or "Anthropic Messages"
        self._use_system_proxy = use_system_proxy
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._info = info or ModelInfo(name=model)

    def describe(self) -> str:
        return f"{self._label} {self._model} @ {egress_host(self._base_url)}"

    def complete(self, prompt: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        data, _ = self._post(
            {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "temperature": self._temperature,
                "messages": [{"role": "user", "content": self._fit(prompt)}],
            },
            timeout=timeout,
        )
        return self._extract_text(data)

    def probe(self, *, timeout: float = 8.0) -> None:
        self._post(
            {
                "model": self._model,
                "max_tokens": 1,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": "ping"}],
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
            messages_url(self._base_url),
            payload,
            {
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        content = data.get("content")
        if isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type", "text") == "text"
            )
            if text:
                return text
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise ProviderError(
                "厂商返回错误",
                vendor_message=str(error.get("message")),
            )
        raise ProviderError("返回体缺少 content 文本")
