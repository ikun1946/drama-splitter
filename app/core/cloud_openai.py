"""OpenAI 兼容协议的适配器（对话补全 + 音频转写）。

为什么一家适配器能覆盖"大部分厂商"
----------------------------------
绝大多数厂商都提供 OpenAI 形态的接口：国内的 DeepSeek / Kimi / 智谱 / 百炼兼容模式 /
硅基流动 / 混元 / 星火兼容端点，海外的 OpenAI / Groq / Mistral / DeepInfra /
OpenRouter，以及自建的 Ollama / LM Studio / 各类网关。于是"支持一家厂商"
退化成"填一份档案"，**不需要为每家写适配器**（`cloud.py` 按协议分派，
见 `providers.py` 头部"厂商是数据，协议是信封"）。

两条固定约定（用户不可改，避免配置地狱）
----------------------------------------
- 对话补全：``POST {base_url}/chat/completions``；
- 音频转写：``POST {base_url}/audio/transcriptions``，multipart，固定带
  ``response_format=verbose_json`` 与 ``timestamp_granularities[]=word``。

转写的降级是明说的
------------------
各家对 verbose_json / 词级时间戳的支持差异很大，三种情形分别处理：

- 给了 ``words[]`` → 词级时间戳（候选切点依赖它）；
- 只给 ``segments[]`` → **返回空 words**，交给既有的分句级降级
  （`CloudAsrEngine` 会合成粗粒度词并记 `ASR_NO_WORD_TIMESTAMPS`）；
- 给了文本却一个时间戳都没有 → **报错**。绝不静默当成"这段没有语音"——
  那会让下游把有对白的片段判成静音，是比失败更坏的错。
"""

from __future__ import annotations

from pathlib import Path

from .cloud_http import (
    extract_chat_text,
    get_ok,
    post_json,
    post_multipart,
    send_multipart,
    silent_wav_bytes,
)
from .providers import (
    DEFAULT_TIMEOUT,
    ProviderError,
    ProviderSegment,
    ProviderTranscript,
    ProviderWord,
    audio_transcriptions_url,
    chat_completions_url,
    egress_host,
)

__all__ = ["OpenAiAsrTransport", "OpenAiChatTransport"]

#: 音频后缀 → content-type（只收这些；`CloudAsrEngine` 实际只会给 wav / mp3）
_ASR_FORMATS = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}

#: 内部语言代码 → OpenAI 兼容转写期望的 ISO-639-1（其余原样透传）
_LANGUAGE_MAP = {"yue": "zh"}

#: 静音样片被拒时，厂商说明里出现这些词才认为"是样片的问题，不是配置的问题"
_SAMPLE_COMPLAINTS = ("audio", "short", "duration", "silence", "empty", "format", "length")


def _seconds_to_ms(value) -> int:
    """秒 → 毫秒（OpenAI 兼容接口的时间戳单位是秒，且可能是小数）。"""
    try:
        return max(0, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return 0


class OpenAiChatTransport:
    """OpenAI 兼容 ``/chat/completions`` 适配器（火山方舟的语义判断也走它）。"""

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
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        #: 显示名来自档案 label（如"火山方舟 / 豆包"），日志里才能认出是哪一家
        self._label = (label or "").strip() or "OpenAI 兼容"
        self._use_system_proxy = use_system_proxy
        self._temperature = temperature
        self._max_tokens = max_tokens

    # ---- 协议实现 ------------------------------------------------------

    def describe(self) -> str:
        return f"{self._label} {self._model} @ {egress_host(self._base_url)}"

    def complete(self, prompt: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        data, _ = self._post(
            {"model": self._model, "messages": [{"role": "user", "content": prompt}],
             "temperature": self._temperature, "max_tokens": self._max_tokens},
            timeout=timeout,
        )
        return extract_chat_text(data)

    def probe(self, *, timeout: float = 8.0) -> None:
        """连通性自检：一次最小请求（1 token），只关心"能不能通、鉴权对不对"。"""
        self._post(
            {"model": self._model, "messages": [{"role": "user", "content": "ping"}],
             "temperature": 0.0, "max_tokens": 1},
            timeout=timeout,
        )

    # ---- 内部 ----------------------------------------------------------

    def _post(self, payload: dict, *, timeout: float) -> tuple[dict, object]:
        if not self._base_url:
            raise ProviderError("未配置接口地址（base_url）")
        if not self._api_key:
            raise ProviderError("缺少 API Key")
        if not self._model:
            raise ProviderError("未配置模型名（judge_model）")
        return post_json(
            chat_completions_url(self._base_url),
            payload,
            {"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )


class OpenAiAsrTransport:
    """OpenAI 兼容 ``/audio/transcriptions`` 适配器（Whisper 风格接口）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        label: str = "",
        use_system_proxy: bool = False,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._label = (label or "").strip() or "OpenAI 兼容"
        self._use_system_proxy = use_system_proxy
        #: 探测只验证了鉴权、未验证模型时的说明（界面照原样展示，不假装全测过）
        self.probe_note = ""

    # ---- 协议实现 ------------------------------------------------------

    def describe(self) -> str:
        return f"{self._label} 转写 {self._model} @ {egress_host(self._base_url)}"

    def transcribe_file(
        self,
        audio: Path,
        *,
        language: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ProviderTranscript:
        audio = Path(audio)
        content_type = _ASR_FORMATS.get(audio.suffix.lower())
        if content_type is None:
            raise ProviderError(
                f"不支持的音频格式：{audio.suffix}（支持 "
                + " / ".join(sorted(suffix.lstrip(".") for suffix in _ASR_FORMATS))
                + "）"
            )
        try:
            payload = audio.read_bytes()
        except OSError as exc:
            raise ProviderError(f"读取音频失败：{exc}") from exc
        if not payload:
            raise ProviderError("音频为空（0 字节）")

        data, _ = self._post(
            self._fields(language),
            (audio.name, payload, content_type),
            timeout=timeout,
        )
        return self._parse(data)

    def probe(self, *, timeout: float = 8.0) -> None:
        """连通性自检：上传 1 秒静音。

        只承诺"鉴权与路径可用"：静音样片被拒（部分服务要求非静音或更长）时，
        退化为 ``GET /models`` 验鉴权，并在 :attr:`probe_note` 里如实说明
        "没验证转写模型"。**词级时间戳是否真的可用只能靠真机素材验证**——
        这一点与其他厂商的连通性测试口径一致，不假装探到了。
        """
        try:
            send_multipart(
                audio_transcriptions_url(self._base_url),
                self._fields(""),
                ("file", "probe.wav", silent_wav_bytes(), "audio/wav"),
                {"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout,
                use_system_proxy=self._use_system_proxy,
            )
            return
        except ProviderError as exc:
            if exc.status in (401, 403):
                raise          # Key 不对：唯一可靠的判据，直接失败
            if exc.status in (404, 405):
                raise ProviderError(
                    f"{exc.describe()}；base_url 通常要以 /v1 结尾，"
                    "且不要已经包含 /audio/transcriptions",
                    status=exc.status,
                    vendor_message=exc.vendor_message,
                    logid=exc.logid,
                ) from exc
            if exc.status is None or exc.status >= 500:
                raise          # 网络/服务端问题：原样报出
            if not self._looks_like_sample_complaint(exc):
                raise          # 不是"样片不合规"，那就可能是配置问题，原样报出
        if get_ok(
            f"{self._base_url}/models",
            {"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        ):
            self.probe_note = "只验证了鉴权与路径，未验证转写模型（该服务不接受静音样片）"
            return
        raise ProviderError(
            "连通性测试未通过：静音样片被拒，且该服务没有 /models 端点可供兜底自检"
        )

    # ---- 内部 ----------------------------------------------------------

    def _fields(self, language: str) -> dict[str, str]:
        fields = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        mapped = _LANGUAGE_MAP.get(language, language or "")
        if mapped:
            fields["language"] = mapped
        return fields

    def _post(self, fields: dict, file_field: tuple[str, bytes, str], *, timeout: float):
        if not self._base_url:
            raise ProviderError("未配置接口地址（base_url）")
        if not self._api_key:
            raise ProviderError("缺少 API Key")
        if not self._model:
            raise ProviderError("未配置转写模型名（asr_model_name）")
        return post_multipart(
            audio_transcriptions_url(self._base_url),
            fields,
            ("file", *file_field),
            {"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )

    @staticmethod
    def _looks_like_sample_complaint(exc: ProviderError) -> bool:
        text = f"{exc} {exc.vendor_message}".lower()
        return any(word in text for word in _SAMPLE_COMPLAINTS)

    @staticmethod
    def _parse(data: dict) -> ProviderTranscript:
        duration_ms = _seconds_to_ms(data.get("duration"))
        words: list[ProviderWord] = []
        for item in data.get("words") or []:
            if not isinstance(item, dict):
                continue
            token = str(item.get("word") or item.get("text") or "").strip()
            if not token:
                continue
            words.append(
                ProviderWord(
                    start_ms=_seconds_to_ms(item.get("start")),
                    end_ms=_seconds_to_ms(item.get("end")),
                    text=token,
                    confidence=0.0,   # OpenAI 兼容接口不给词级置信度，不编一个
                )
            )
        segments: list[ProviderSegment] = []
        for item in data.get("segments") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            segments.append(
                ProviderSegment(
                    start_ms=_seconds_to_ms(item.get("start")),
                    end_ms=_seconds_to_ms(item.get("end")),
                    text=text,
                )
            )
        if words or segments:
            return ProviderTranscript(
                words=words, segments=segments, duration_ms=duration_ms
            )
        text = str(data.get("text") or "").strip()
        if not text:
            # 静音：厂商给 2xx + 空文本（与火山 20000003 同义），不是错误
            return ProviderTranscript(duration_ms=duration_ms)
        raise ProviderError(
            "厂商返回了文本但没有时间戳：该服务可能不支持 verbose_json / "
            "timestamp_granularities，请核对其文档",
            vendor_message=text[:120],
        )