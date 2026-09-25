"""火山方舟 / 豆包适配器（首版唯一厂商实现）。

**只放厂商差异**：鉴权头、请求信封、响应解析、错误映射。上层
（``CloudLlmJudge`` / 后续的 ``CloudAsrEngine``）只认 ``providers.py`` 里的协议，
不得出现"Ark 特有的字段名"。

为什么走 urllib 而不是官方 SDK
------------------------------
与 ``OllamaJudge`` 同一条理由：本项目不引入新的运行时依赖，打包体积与
"装机就能跑"优先。请求主干只有一个 JSON POST，用标准库足够。

代理行为
--------
默认**显式绕过系统代理**（``ProxyHandler({})``）：本机环境里 ``http_proxy``
会把请求交给代理，代理再拒绝或改写回环/内网地址（Ollama 路径已实测过一次）。
档案里给了 ``use_system_proxy`` 开关，需要时由用户显式打开。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .providers import (
    DEFAULT_TIMEOUT,
    ProviderError,
    ProviderSegment,
    ProviderTranscript,
    ProviderWord,
    egress_host,
)

__all__ = ["VolcArkChatTransport", "VolcFlashAsrTransport"]

#: 尽量把厂商侧的排错线索带回来（不同网关用的头名不一样）
_LOGID_HEADERS = ("X-Request-Id", "X-Tt-Logid", "X-Client-Request-Id")

# ---- 大模型录音文件识别极速版 -------------------------------------------
#
# 官方文档（https://www.volcengine.com/docs/6561/1631584）：
#   - 单文件 ≤2 小时 / ≤100MB；格式 WAV / MP3 / OGG OPUS；一次请求返回，无需轮询
#   - 成功靠**响应头 X-Api-Status-Code** 判定，不是靠 HTTP 状态码
#   - 只有 request.show_utterances=true 才会返回 words[]（词级时间戳）
#   - 错误码：20000000 成功；**20000003 静音音频**（说明鉴权与权限都对，只是没语音）；
#     45000001 参数无效；45000002 空音频；45000151 格式不正确；550XXXX 服务端错误
ASR_STATUS_OK = "20000000"
ASR_STATUS_SILENT = "20000003"

#: 极速版接受的音频格式（WAV / MP3 / OGG OPUS）
_ASR_FORMATS = {".wav": "wav", ".mp3": "mp3", ".ogg": "ogg", ".opus": "ogg"}

#: 内部语言代码 → 厂商要求的 BCP-47 形式（其余原样透传，如 zh-CN / en-US）
_LANGUAGE_MAP = {"zh": "zh-CN", "en": "en-US", "yue": "yue-CN"}


def _silent_wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """生成一小段静音 WAV（16k 单声道 16bit）。

    连通性测试用它当"最小请求"：不依赖 ffmpeg、不读用户素材，也不会产生
    任何真实内容的外发。厂商对它的返回是 20000003（静音音频）——恰好证明
    Key 与资源权限都是通的。
    """
    frames = int(rate * max(0.1, seconds))
    data = b"\x00\x00" * frames
    header = (
        b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")      # PCM
        + (1).to_bytes(2, "little")      # 单声道
        + rate.to_bytes(4, "little")
        + (rate * 2).to_bytes(4, "little")   # 字节率
        + (2).to_bytes(2, "little")      # 块对齐
        + (16).to_bytes(2, "little")     # 位深
        + b"data" + len(data).to_bytes(4, "little")
    )
    return header + data


def _post_json(
    url: str,
    payload: dict,
    headers: dict,
    *,
    timeout: float,
    use_system_proxy: bool,
) -> tuple[dict, object]:
    """POST 一个 JSON，返回 ``(响应体, 响应头)``；失败抛 :class:`ProviderError`。

    两个适配器（chat 与录音文件识别）共用这一段：代理处理、HTTP 错误映射、
    JSON 解析只此一份，避免两边各写一套后行为漂移。
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    opener = (
        urllib.request.build_opener()
        if use_system_proxy
        else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            response_headers = response.headers
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"网络不可达：{exc.reason}") from exc
    except OSError as exc:  # 含超时
        raise ProviderError(f"请求失败：{exc}") from exc

    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError(f"返回体不是合法 JSON：{body[:200]}") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"返回体不是 JSON 对象：{body[:200]}")
    return data, response_headers


def _http_error(exc: urllib.error.HTTPError) -> ProviderError:
    """把 HTTP 错误变成带厂商说明与 logid 的 ProviderError。"""
    detail = ""
    try:
        raw = (exc.read() or b"").decode("utf-8", "replace")
    except OSError:  # pragma: no cover - 读取响应体失败时只保留状态码
        raw = ""
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        if not detail:
            detail = str(payload.get("message") or "")
    if not detail:
        detail = raw.strip()[:200]

    logid = ""
    headers = exc.headers
    for name in _LOGID_HEADERS:
        value = headers.get(name) if headers is not None else None
        if value:
            logid = str(value)
            break

    status = int(exc.code or 0)
    if status in (401, 403):
        headline = "鉴权失败（API Key 或资源权限）"
    elif status == 429:
        headline = "被限流或额度不足"
    elif status >= 500:
        headline = "厂商服务异常"
    else:
        headline = f"请求被拒绝（HTTP {status}）"
    return ProviderError(headline, status=status, vendor_message=detail, logid=logid)


class VolcArkChatTransport:
    """火山方舟 OpenAI 兼容 ``/chat/completions`` 适配器。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        use_system_proxy: bool = False,
        temperature: float = 0.1,
        max_tokens: int = 160,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._use_system_proxy = use_system_proxy
        self._temperature = temperature
        self._max_tokens = max_tokens

    # ---- 协议实现 ------------------------------------------------------

    def describe(self) -> str:
        return f"火山方舟 {self._model} @ {egress_host(self._base_url)}"

    def complete(self, prompt: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        data, _ = self._post(
            {"model": self._model, "messages": [{"role": "user", "content": prompt}],
             "temperature": self._temperature, "max_tokens": self._max_tokens},
            timeout=timeout,
        )
        return self._extract_text(data)

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
        return _post_json(
            f"{self._base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                raise ProviderError(
                    "厂商返回错误",
                    vendor_message=str(error.get("message")),
                    status=None,
                )
            raise ProviderError(f"返回体缺少 choices：{json.dumps(data, ensure_ascii=False)[:200]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            # 部分网关按多段内容返回
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        return str(content or "")


class VolcFlashAsrTransport:
    """豆包「大模型录音文件识别极速版」适配器。

    一次请求返回一段完整音频的识别结果；长片由上层切成多段逐段调用。
    只有 ``show_utterances=true`` 才会返回 ``words[]``——这是词级时间戳，
    候选切点依赖它，因此**固定打开**，不提供关闭开关。
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        resource_id: str,
        model_name: str = "bigmodel",
        *,
        use_system_proxy: bool = False,
    ) -> None:
        self._endpoint = (endpoint or "").strip()
        self._api_key = (api_key or "").strip()
        self._resource_id = (resource_id or "").strip()
        self._model_name = (model_name or "bigmodel").strip()
        self._use_system_proxy = use_system_proxy

    # ---- 协议实现 ------------------------------------------------------

    def describe(self) -> str:
        return f"豆包录音文件识别 {self._model_name} @ {egress_host(self._endpoint)}"

    def transcribe_file(
        self,
        audio: Path,
        *,
        language: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ProviderTranscript:
        audio = Path(audio)
        fmt = _ASR_FORMATS.get(audio.suffix.lower())
        if fmt is None:
            raise ProviderError(f"不支持的音频格式：{audio.suffix}（支持 wav / mp3 / ogg）")
        try:
            payload_bytes = audio.read_bytes()
        except OSError as exc:
            raise ProviderError(f"读取音频失败：{exc}") from exc

        data, headers = self._post(
            base64.b64encode(payload_bytes).decode("ascii"),
            fmt,
            language=language,
            timeout=timeout,
        )
        return self._parse(data, headers)

    def probe(self, *, timeout: float = 8.0) -> None:
        """连通性自检：上传 1 秒静音。

        厂商对静音的返回码是 20000003——它同样证明 Key 与资源权限都是通的，
        因此与 20000000 一起视为"可用"。不读用户素材、不依赖 ffmpeg。
        """
        data, headers = self._post(
            base64.b64encode(_silent_wav_bytes()).decode("ascii"),
            "wav",
            language="zh",
            timeout=timeout,
        )
        code = str(headers.get("X-Api-Status-Code") or "")  # type: ignore[union-attr]
        if code not in (ASR_STATUS_OK, ASR_STATUS_SILENT):
            raise ProviderError(
                f"连通性测试未通过（{code or '无状态码'}）",
                vendor_message=str(headers.get("X-Api-Message") or ""),  # type: ignore[union-attr]
                logid=str(headers.get("X-Tt-Logid") or ""),  # type: ignore[union-attr]
            )

    # ---- 内部 ----------------------------------------------------------

    def _post(self, audio_b64: str, fmt: str, *, language: str, timeout: float):
        if not self._endpoint:
            raise ProviderError("未配置转写接口地址（asr_endpoint）")
        if not self._api_key:
            raise ProviderError("缺少 API Key")
        if not self._resource_id:
            raise ProviderError("未配置资源 ID（asr_resource_id）")

        payload = {
            "user": {"uid": self._api_key[:16] or "drama-splitter"},
            "audio": {
                "data": audio_b64,
                "format": fmt,
                "rate": 16000,
                "channel": 1,
                "language": _LANGUAGE_MAP.get(language, language or "zh-CN"),
            },
            "request": {
                "model_name": self._model_name,
                "show_utterances": True,
                "enable_punc": True,
                "enable_itn": True,
            },
        }
        data, headers = _post_json(
            self._endpoint,
            payload,
            {
                "X-Api-Key": self._api_key,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Sequence": "-1",
            },
            timeout=timeout,
            use_system_proxy=self._use_system_proxy,
        )
        return data, headers

    @staticmethod
    def _parse(data: dict, headers) -> ProviderTranscript:
        """按响应头状态码判定成败，再把 utterances/words 映射成协议对象。"""
        code = str(headers.get("X-Api-Status-Code") or "")
        message = str(headers.get("X-Api-Message") or "")
        logid = str(headers.get("X-Tt-Logid") or "")
        if code not in (ASR_STATUS_OK, ASR_STATUS_SILENT):
            raise ProviderError(
                f"识别失败（{code or '无状态码'}）",
                vendor_message=message,
                logid=logid,
            )
        if code == ASR_STATUS_SILENT:
            # 静音音频：正常结果（上层会把它记成"没有语音内容"），不是错误
            return ProviderTranscript()

        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        words: list[ProviderWord] = []
        segments: list[ProviderSegment] = []
        for utterance in result.get("utterances") or []:
            if not isinstance(utterance, dict):
                continue
            text = str(utterance.get("text") or "").strip()
            start = _as_ms(utterance.get("start_time"))
            end = _as_ms(utterance.get("end_time"))
            if text:
                segments.append(ProviderSegment(start_ms=start, end_ms=end, text=text))
            for word in utterance.get("words") or []:
                if not isinstance(word, dict):
                    continue
                token = str(word.get("text") or "").strip()
                if not token:
                    continue
                words.append(
                    ProviderWord(
                        start_ms=_as_ms(word.get("start_time")),
                        end_ms=_as_ms(word.get("end_time")),
                        text=token,
                        confidence=_as_float(word.get("confidence")),
                    )
                )
        audio_info = data.get("audio_info") if isinstance(data.get("audio_info"), dict) else {}
        return ProviderTranscript(
            words=words,
            segments=segments,
            duration_ms=_as_ms(audio_info.get("duration")),
        )


def _as_ms(value) -> int:
    """厂商的毫秒字段转 int（缺字段/脏值一律 0，不猜）。"""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0