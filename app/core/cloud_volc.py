"""火山方舟 / 豆包：**转写**适配器（录音文件识别极速版）。

**只放厂商差异**：鉴权头、请求信封、响应解析、错误映射。通用 HTTP 工具
（代理策略、超时、错误映射、静音样片）已抽到 ``cloud_http.py``；方舟的对话补全
本身就是 OpenAI 形态，因此**语义判断走 ``cloud_openai.OpenAiChatTransport``**，
这里不再单独实现一个 chat 适配器。

为什么用标准库而不是官方 SDK
----------------------------
与 ``OllamaJudge`` 同一条理由：本项目不引入新的运行时依赖，打包体积与
"装机就能跑"优先。极速版只有一个 JSON POST（音频以 base64 放进请求体），
用标准库足够。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

from .cloud_http import post_json, silent_wav_bytes
from .providers import (
    DEFAULT_TIMEOUT,
    ProviderError,
    ProviderSegment,
    ProviderTranscript,
    ProviderWord,
    egress_host,
)

__all__ = ["ASR_STATUS_OK", "ASR_STATUS_SILENT", "VolcFlashAsrTransport"]

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
            base64.b64encode(silent_wav_bytes()).decode("ascii"),
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
        data, headers = post_json(
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