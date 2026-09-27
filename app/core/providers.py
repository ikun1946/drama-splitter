"""厂商 API 档案（供应商配置）与适配器协议。

为什么需要这一层
----------------
本工具的转写与语义判断原先只允许本地模型（`semantic.py` 头部曾写死"没有任何云端
调用路径"）。放开之后，风险不在"能不能调通"，而在**外发范围失控**与**厂商差异
渗进主链路**。因此把"厂商是什么"全部收在这个模块里，只暴露三样东西：

- :class:`ProviderProfile` 一份档案（地址、Key、模型名、协议、是否允许外发）；
- :class:`ProfileRegistry` 档案读取（用户配置 + 内置默认档案合并）；
- :class:`JudgeTransport` / :class:`AsrTransport` 两个**适配器协议**。

调用方（CloudLlmJudge / CloudAsrEngine）只认协议；鉴权、信封、时间戳单位这些
厂商差异只允许出现在适配器实现里（如 `cloud_volc.py`）。

厂商是数据，协议是信封
----------------------
**加一家厂商 = 加一份预置数据**（`DEFAULT_PROFILES` 里的一条），不需要改任何
代码；`app/core/cloud.py` 按**协议**（不是厂商 id）分派适配器。协议是封闭集合：
停用 / OpenAI 兼容 / 火山方舟——只有新增一种*协议*才需要写适配器。

注意"火山方舟"只是**信封的名字**，不是厂商名：`volc` 这家厂商的语义判断走的
就是 OpenAI 兼容协议，只有转写是方舟自己的极速版协议。

默认不外发
----------
档案内置 `enabled=False` / `allow_upload=False`。`backend_prefs` 指向某档案但开关
没开时，调用方按本地路径执行并在日志说明——**不给任何隐式外发路径**。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

__all__ = [
    "CAP_ASR",
    "CAP_JUDGE",
    "CAP_LABELS",
    "DEFAULT_PROFILES",
    "ENV_KEY_TEMPLATE",
    "PROTOCOLS_BY_CAP",
    "PROTO_DISABLED",
    "PROTO_LABELS",
    "PROTO_OPENAI",
    "PROTO_VOLC_ARK",
    "AsrTransport",
    "JudgeTransport",
    "ProfileRegistry",
    "ProviderError",
    "ProviderProfile",
    "ProviderSegment",
    "ProviderTranscript",
    "ProviderWord",
    "asr_target_host",
    "asr_target_url",
    "audio_transcriptions_url",
    "block_reason",
    "chat_completions_url",
    "describe_egress",
    "egress_host",
    "judge_target_url",
    "merge_profiles",
    "missing_config_reason",
    "resolve_api_key",
    "validate_profile",
]

CAP_ASR = "asr"
CAP_JUDGE = "judge"

#: 能力 → 界面/日志里的中文名（选云端却没生效时，要说清是哪一项能力）
CAP_LABELS = {CAP_ASR: "转写", CAP_JUDGE: "语义判断"}

#: 协议取值。封闭集合：只有新增一种协议才需要写适配器（见 `cloud.py`）。
PROTO_DISABLED = "disabled"
PROTO_OPENAI = "openai"
PROTO_VOLC_ARK = "volc_ark"

PROTO_LABELS = {
    PROTO_DISABLED: "停用",
    PROTO_OPENAI: "OpenAI 兼容",
    PROTO_VOLC_ARK: "火山方舟",
}

#: 能力 → 可用协议（顺序即下拉顺序）。语义判断不需要"火山方舟"这一项：
#: 方舟的 chat 本身就是 OpenAI 形态，再列一项只会让同一种请求有两个名字。
PROTOCOLS_BY_CAP = {
    CAP_JUDGE: (PROTO_DISABLED, PROTO_OPENAI),
    CAP_ASR: (PROTO_DISABLED, PROTO_OPENAI, PROTO_VOLC_ARK),
}

#: Key 的环境变量前缀：`DRAMA_<档案 id 大写>_API_KEY`
ENV_KEY_TEMPLATE = "DRAMA_{id}_API_KEY"

#: 语义判断一次性请求的默认超时（秒）
DEFAULT_TIMEOUT = 120.0

#: 自定义厂商 id 的合法形式：ASCII 小写字母/数字，可含 - 与 _，首字符非连字符
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class ProviderError(RuntimeError):
    """一次厂商调用失败（网络、鉴权、限流、返回体不合法）。

    `status` / `vendor_message` / `logid` 都要带到用户可见的日志里：
    云端失败最需要的是"为什么"，而不是"失败了"。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        vendor_message: str = "",
        logid: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.vendor_message = vendor_message
        self.logid = logid

    def describe(self) -> str:
        parts = [str(self)]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.vendor_message:
            parts.append(f"厂商说明：{self.vendor_message}")
        if self.logid:
            parts.append(f"logid：{self.logid}")
        return "；".join(parts)


@dataclass(frozen=True)
class ProviderWord:
    """厂商返回的**词级**结果（毫秒），交给核心层映射成 `AsrWord`。

    时间戳单位固定毫秒：火山、阿里、腾讯都是毫秒，厂商若给别的单位，
    由适配器换算后填这里——单位差异不允许渗进核心层。
    """

    start_ms: int
    end_ms: int
    text: str
    confidence: float = 0.0


@dataclass(frozen=True)
class ProviderSegment:
    """厂商自己的分句（毫秒）。只用于展示与缓存，候选点依据仍是词级时间戳。"""

    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class ProviderTranscript:
    """一次云端转写的结果。

    ``words`` 为空表示该厂商没有给词级时间戳——这是**质量降级**，不是失败：
    上层会按 §7 记为限制（句子退化为分句级），而不是编造时间戳。
    """

    words: list[ProviderWord] = field(default_factory=list)
    segments: list[ProviderSegment] = field(default_factory=list)
    duration_ms: int = 0


class JudgeTransport(Protocol):
    """语义判断适配器：把提示词变成模型输出文本。"""

    def complete(self, prompt: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:  # pragma: no cover
        """执行一次判定请求，返回模型原始输出（解析交给上层）。"""

    def probe(self, *, timeout: float = 8.0) -> None:  # pragma: no cover
        """连通性自检；不可用或鉴权失败时抛 :class:`ProviderError`。"""

    def describe(self) -> str:  # pragma: no cover
        """一句话描述（落到日志与检查器）。"""


class AsrTransport(Protocol):
    """转写适配器：把**一段完整音频文件**变成词级结果。

    只接受"整段一次请求"的形态。长片由上层按 600 秒分块逐段调用（见设计 §3），
    流式/分帧协议（如讯飞 WebSocket）不符合本协议，若要接入需要另做适配。
    """

    def transcribe_file(
        self,
        audio: Path,
        *,
        language: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ProviderTranscript:  # pragma: no cover
        """转写一段音频；失败抛 :class:`ProviderError`。没有词级结果时 words 为空。"""

    def probe(self, *, timeout: float = 8.0) -> None:  # pragma: no cover
        """连通性自检；失败抛 :class:`ProviderError`。"""

    def describe(self) -> str:  # pragma: no cover
        """一句话描述。"""


@dataclass(frozen=True)
class ProviderProfile:
    """一份厂商档案。

    两个开关语义必须分清，否则会出现"以为没外发、其实发了"：

    - ``enabled``：档案已通过连通性测试，**允许被 backend_prefs 选中**；
    - ``allow_upload``：**外发许可**（转写上传音轨、判断上传台词文本都受它约束）。
    """

    id: str
    label: str
    base_url: str = ""
    asr_endpoint: str = ""
    asr_resource_id: str = ""
    #: 转写模型名。默认空串而不是厂商专有值——否则每份新档案都会"继承"火山的
    #: bigmodel（`VolcFlashAsrTransport` 内部另有 bigmodel 兜底，不依赖这里）。
    asr_model_name: str = ""
    judge_model: str = ""
    #: 用哪种信封（见 PROTO_LABELS）。路径由协议约定，不开放自定义。
    judge_protocol: str = PROTO_DISABLED
    asr_protocol: str = PROTO_DISABLED
    api_key: str = ""
    enabled: bool = False
    allow_upload: bool = False
    use_system_proxy: bool = False
    #: 用户手动添加的模型名（与 `/models` 拉回来的清单合并显示）。
    #: 存在的理由：有些厂商的可用模型**不在清单里**——方舟用推理接入点 ID
    #: （`ep-xxx`）、自建网关常只暴露别名。手填的名字要能记住，否则每次都要重敲。
    #: 用 tuple 而不是 list：档案是 frozen 的，可变默认值会在实例之间共享。
    custom_models: tuple[str, ...] = ()

    # ---- 能力 ----------------------------------------------------------

    def asr_configured(self) -> bool:
        """转写的"地址类"字段是否齐（不含 Key 与两个开关）。"""
        if self.asr_protocol == PROTO_VOLC_ARK:
            return bool(self.asr_endpoint and self.asr_resource_id)
        if self.asr_protocol == PROTO_OPENAI:
            # OpenAI 兼容转写的路径由协议约定（/audio/transcriptions），
            # 只要 base_url 在就算配好地址
            return bool(self.base_url)
        return False

    def supports(self, capability: str) -> bool:
        if capability == CAP_JUDGE:
            return self.judge_protocol != PROTO_DISABLED and bool(self.judge_model)
        if capability == CAP_ASR:
            return self.asr_protocol != PROTO_DISABLED and self.asr_configured()
        raise ValueError(f"未知能力：{capability}")

    def egress_allowed(self, capability: str) -> bool:
        """该能力此刻是否允许外发（两个开关都要开，且该能力已配置）。"""
        return self.enabled and self.allow_upload and self.supports(capability)

    def resolved_api_key(self) -> str:
        return resolve_api_key(self)

    # ---- 序列化 --------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "asr_endpoint": self.asr_endpoint,
            "asr_resource_id": self.asr_resource_id,
            "asr_model_name": self.asr_model_name,
            "judge_model": self.judge_model,
            "judge_protocol": self.judge_protocol,
            "asr_protocol": self.asr_protocol,
            "api_key": self.api_key,
            "enabled": self.enabled,
            "allow_upload": self.allow_upload,
            "use_system_proxy": self.use_system_proxy,
            "custom_models": list(self.custom_models),
        }

    @classmethod
    def from_json(cls, data: dict, *, fallback: "ProviderProfile | None" = None) -> "ProviderProfile":
        """容错解析：类型不对的字段回落默认值，绝不因为配置坏了而让启动失败。

        未知字段丢弃、缺失字段用默认值；``fallback`` 给定时，以它为基底覆盖——
        用户配置只写了两个字段也能合并成一份完整档案。
        """
        base = _base_for(data, fallback)
        payload = data if isinstance(data, dict) else {}

        def text(key: str, default: str) -> str:
            value = payload.get(key, default)
            return value.strip() if isinstance(value, str) else default

        def flag(key: str, default: bool) -> bool:
            value = payload.get(key, default)
            return bool(value) if isinstance(value, (bool, int)) else default

        def protocol(key: str, default: str) -> str:
            """协议枚举容错：非法值回落基底，**不回落 disabled**。

            回落 disabled 会让"手写错一个字母"变成"静默关掉这项能力"——
            宁可保留原协议，让用户在连通性测试里看到真实报错。
            """
            value = payload.get(key, default)
            if isinstance(value, str) and value.strip() in PROTO_LABELS:
                return value.strip()
            return default

        def models(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            """手填模型清单容错：只留非空字符串、去重、保序。

            "字段缺失"与"空清单"要分清：用户把自定义模型全删了就是空清单，
            不该被当成缺失而回落基底里的旧值。
            """
            value = payload.get(key, default)
            if not isinstance(value, list):
                return default
            kept: list[str] = []
            for item in value:
                name = item.strip() if isinstance(item, str) else ""
                if name and name not in kept:
                    kept.append(name)
            return tuple(kept)

        return replace(
            base,
            id=text("id", base.id) or base.id,
            label=text("label", base.label) or base.label,
            base_url=text("base_url", base.base_url),
            asr_endpoint=text("asr_endpoint", base.asr_endpoint),
            asr_resource_id=text("asr_resource_id", base.asr_resource_id),
            asr_model_name=text("asr_model_name", base.asr_model_name) or base.asr_model_name,
            judge_model=text("judge_model", base.judge_model),
            judge_protocol=protocol("judge_protocol", base.judge_protocol),
            asr_protocol=protocol("asr_protocol", base.asr_protocol),
            api_key=text("api_key", base.api_key),
            enabled=flag("enabled", base.enabled),
            allow_upload=flag("allow_upload", base.allow_upload),
            use_system_proxy=flag("use_system_proxy", base.use_system_proxy),
            custom_models=models("custom_models", base.custom_models),
        )

    def with_updates(self, **changes: Any) -> "ProviderProfile":
        return replace(self, **changes)

    def model_choices(self, fetched: Iterable[str] = ()) -> tuple[str, ...]:
        """候选模型清单 = 接口拉回来的 + 手填的（去重、保序，手填的补在后面）。

        合并规则只有 :func:`merge_model_choices` 一份定义；表单里手填的清单可能
        还没落盘，那边直接调函数、不走本方法。
        """
        return merge_model_choices(fetched, self.custom_models)


def merge_model_choices(
    fetched: Iterable[str], custom: Iterable[str]
) -> tuple[str, ...]:
    """把"接口拉回来的"与"手填的"合成一张清单：去空白、去重、保序。

    顺序刻意是"接口在前、手填在后"：接口清单是字母序的已知集合，手填的通常
    是清单里没有的部署名（方舟的 ``ep-xxx``），补在末尾一眼能找到。
    """
    choices: list[str] = []
    for name in list(fetched) + list(custom):
        text = name.strip() if isinstance(name, str) else ""
        if text and text not in choices:
            choices.append(text)
    return tuple(choices)


#: 全空档案：自定义厂商的唯一基底。
#: **刻意不继承任何内置厂商的地址与资源 ID**——否则自定义档案会凭空"支持火山转写"。
_BLANK_PROFILE = ProviderProfile(id="", label="")


def _base_for(data: object, fallback: "ProviderProfile | None") -> "ProviderProfile":
    """选基底档案：显式 fallback > 按 id 找内置 > 全空。

    按 id 找内置这一步不能省：拿内置里第一家（火山）当兜底，会让"id 写错一个
    字母"变成"继承火山的地址与资源 ID"。
    """
    if fallback is not None:
        return fallback
    payload = data if isinstance(data, dict) else {}
    profile_id = str(payload.get("id") or "").strip()
    for profile in DEFAULT_PROFILES:
        if profile.id == profile_id:
            return profile
    return _BLANK_PROFILE


#: 内置档案（预置目录）。
#:
#: **预置只是起点**：模型名会随厂商迭代而过时，用户可在「工具 → 模型管理… →
#: 云端厂商」里改；
#: 这里给出的地址与模型名取自各家官方文档（见每条上方 docs 链接），一律不带 Key，
#: 且 `enabled=False` / `allow_upload=False`——不给任何隐式外发路径。
#:
#: 顺序即界面下拉顺序：国内 → 海外 → 自建/聚合。`volc` 必须留在**第一位**
#: （`ProfileRegistry.default()`、合并排序与既有断言都依赖它）。
#:
#: 转写协议标 `openai` 的家数，是"该厂商提供 OpenAI 兼容的 /audio/transcriptions"；
#: 是否返回**词级时间戳**各家不一，缺词级时走即有的分句级降级（ASR_NO_WORD_TIMESTAMPS）。
DEFAULT_PROFILES: tuple[ProviderProfile, ...] = (
    # docs: https://www.volcengine.com/docs/82379（方舟 chat 是 OpenAI 兼容形态）
    #       https://www.volcengine.com/docs/6561/1631584（录音文件识别极速版）
    # judge_model 保持空串：方舟的语义判断要填**推理接入点 ID**（ep-xxx），
    # 官方默认值不存在，必须由用户自己填。
    ProviderProfile(
        id="volc",
        label="火山方舟 / 豆包",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        asr_endpoint="https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        asr_resource_id="volc.bigasr.auc_turbo",
        asr_model_name="bigmodel",
        judge_protocol=PROTO_OPENAI,
        asr_protocol=PROTO_VOLC_ARK,
    ),
    # ---- 国内 -----------------------------------------------------------
    # docs: https://api-docs.deepseek.com/zh-cn/
    ProviderProfile(
        id="deepseek",
        label="深度求索 DeepSeek",
        base_url="https://api.deepseek.com/v1",
        judge_model="deepseek-chat",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://help.aliyun.com/zh/model-studio/developer-reference/compatibility-of-openai-with-dashscope
    # 转写走百炼自己的 Paraformer 协议（非本期范围），故 asr_protocol 停用。
    ProviderProfile(
        id="dashscope",
        label="阿里云百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        judge_model="qwen-plus",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://platform.moonshot.cn/docs/api/chat
    ProviderProfile(
        id="moonshot",
        label="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        judge_model="moonshot-v1-8k",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://docs.bigmodel.cn/cn/guide/develop/openai/introduction
    # 是否提供 OpenAI 兼容 /audio/transcriptions 与词级时间戳待真机核实，故停用。
    ProviderProfile(
        id="zhipu",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        judge_model="glm-4-flash",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://cloud.tencent.com/document/product/1729/111007
    # 转写是腾讯自己的录音识别协议，故停用。
    ProviderProfile(
        id="hunyuan",
        label="腾讯混元",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        judge_model="hunyuan-turbos-latest",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://docs.siliconflow.cn/cn/api-reference/audio/create-audio-transcriptions
    # SenseVoiceSmall 只给分句时间戳（无词级）→ 走分句级降级，如实标注。
    ProviderProfile(
        id="siliconflow",
        label="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        judge_model="Qwen/Qwen3-8B",
        judge_protocol=PROTO_OPENAI,
        asr_model_name="FunAudioLLM/SenseVoiceSmall",
        asr_protocol=PROTO_OPENAI,
    ),
    # docs: https://www.xfyun.cn/doc/spark/HTTP%E8%B0%83%E7%94%A8%E6%96%87%E6%A1%A3.html
    # 转写是星火自己的 WebSocket 协议，故停用。
    ProviderProfile(
        id="spark",
        label="讯飞星火（兼容端点）",
        base_url="https://spark-api-open.xf-yun.com/v1",
        judge_model="4.0Ultra",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://platform.stepfun.com/docs/overview/concept
    # 是否提供 OpenAI 兼容转写与词级时间戳待真机核实，故停用。
    ProviderProfile(
        id="stepfun",
        label="阶跃星辰 Step",
        base_url="https://api.stepfun.com/v1",
        judge_model="step-2-mini",
        judge_protocol=PROTO_OPENAI,
    ),
    # ---- 海外 -----------------------------------------------------------
    # docs: https://platform.openai.com/docs/api-reference/audio/createTranscription
    # whisper-1 支持 timestamp_granularities[]=word（新版 gpt-4o-transcribe 反而只给
    # 分句），因此预置用 whisper-1，宁可慢一点也要词级时间戳。
    ProviderProfile(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        judge_model="gpt-4o-mini",
        judge_protocol=PROTO_OPENAI,
        asr_model_name="whisper-1",
        asr_protocol=PROTO_OPENAI,
    ),
    # docs: https://console.groq.com/docs/speech-to-text
    ProviderProfile(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        judge_model="llama-3.3-70b-versatile",
        judge_protocol=PROTO_OPENAI,
        asr_model_name="whisper-large-v3-turbo",
        asr_protocol=PROTO_OPENAI,
    ),
    # docs: https://docs.mistral.ai/api/#tag/audio
    ProviderProfile(
        id="mistral",
        label="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        judge_model="mistral-small-latest",
        judge_protocol=PROTO_OPENAI,
        asr_model_name="voxtral-mini-latest",
        asr_protocol=PROTO_OPENAI,
    ),
    # docs: https://deepinfra.com/docs/openai_api
    ProviderProfile(
        id="deepinfra",
        label="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        judge_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        judge_protocol=PROTO_OPENAI,
        asr_model_name="openai/whisper-large-v3",
        asr_protocol=PROTO_OPENAI,
    ),
    # ---- 自建 / 聚合 ----------------------------------------------------
    # docs: https://openrouter.ai/docs/quickstart
    # 聚合站不代理转写，故 asr_protocol 停用。
    ProviderProfile(
        id="openrouter",
        label="OpenRouter（聚合）",
        base_url="https://openrouter.ai/api/v1",
        judge_model="openai/gpt-4o-mini",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://github.com/ollama/ollama/blob/main/docs/openai.md
    # 本地端点：显式绕过系统代理（use_system_proxy 默认 False）是必须的。
    ProviderProfile(
        id="ollama",
        label="Ollama（本地 OpenAI 兼容）",
        base_url="http://127.0.0.1:11434/v1",
        judge_model="qwen3.5:2b",
        judge_protocol=PROTO_OPENAI,
    ),
    # docs: https://lmstudio.ai/docs/app/api/endpoints/openai
    ProviderProfile(
        id="lmstudio",
        label="LM Studio（本地 OpenAI 兼容）",
        base_url="http://127.0.0.1:1234/v1",
        judge_model="local-model",
        judge_protocol=PROTO_OPENAI,
    ),
)


def merge_profiles(raw: Iterable[dict] | None) -> list[ProviderProfile]:
    """用户配置与内置默认档案合并。

    规则：内置档案永远在列表里（用户没写过就是默认值）；用户写过的字段覆盖默认，
    但 ``id`` 不认识的自定义档案原样保留——加第二家厂商时不必改这里。
    """
    defaults = {profile.id: profile for profile in DEFAULT_PROFILES}
    merged: dict[str, ProviderProfile] = dict(defaults)
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("id") or "").strip()
        if not profile_id:
            continue
        merged[profile_id] = ProviderProfile.from_json(
            item, fallback=defaults.get(profile_id)
        )
    # 内置档案排在前面（界面上更稳定），其余按用户给的顺序
    ordered = [merged[pid] for pid in defaults if pid in merged]
    ordered += [p for pid, p in merged.items() if pid not in defaults]
    return ordered


class ProfileRegistry:
    """档案读取入口：`get(id)` / `all()` / `default()`。"""

    def __init__(self, profiles: Iterable[ProviderProfile] | None = None) -> None:
        items = list(profiles) if profiles is not None else list(DEFAULT_PROFILES)
        self._profiles = {profile.id: profile for profile in items}

    @classmethod
    def from_config(cls, config: object) -> "ProfileRegistry":
        """从 AppConfig 构建；配置缺字段时回落内置默认档案。"""
        raw = getattr(config, "providers", None)
        return cls(merge_profiles(raw))

    def get(self, profile_id: str | None) -> ProviderProfile | None:
        if not profile_id:
            return None
        return self._profiles.get(profile_id)

    def all(self) -> list[ProviderProfile]:
        return list(self._profiles.values())

    def default(self) -> ProviderProfile:
        return self.all()[0] if self._profiles else DEFAULT_PROFILES[0]


def resolve_api_key(profile: ProviderProfile) -> str:
    """Key 解析：环境变量优先，其次档案字段。

    环境变量优先是刻意的：部署/打包场景下把凭据放在环境里比写在 JSON 里更可控，
    而档案字段是"桌面用户懒得配环境变量"时的便利路径。
    """
    env_name = ENV_KEY_TEMPLATE.format(id=profile.id.upper())
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        return env_value
    return (profile.api_key or "").strip()


def egress_host(url: str) -> str:
    """从 URL 里取主机名（日志与界面只显示主机，不显示完整路径与查询串）。"""
    try:
        return urlparse(url).hostname or url
    except ValueError:  # pragma: no cover - 极端的畸形 URL
        return url


# ---- 协议约定的 URL（唯一来源）---------------------------------------------
#
# 协议决定路径：OpenAI 兼容 = /chat/completions 与 /audio/transcriptions，
# 火山方舟转写 = 档案里的 asr_endpoint。这里必须只算一次：**"告知用户发往哪台
# 主机"与"适配器真的发往哪台主机"要来自同一份计算**，否则措辞与实际会漂移
# （OpenAI 兼容转写根本没有 asr_endpoint 字段，最容易漂）。


def chat_completions_url(base_url: str) -> str:
    """OpenAI 兼容的对话补全地址；base_url 为空时返回空串。"""
    base = (base_url or "").strip().rstrip("/")
    return f"{base}/chat/completions" if base else ""


def audio_transcriptions_url(base_url: str) -> str:
    """OpenAI 兼容的转写地址；base_url 为空时返回空串。"""
    base = (base_url or "").strip().rstrip("/")
    return f"{base}/audio/transcriptions" if base else ""


def judge_target_url(profile: ProviderProfile) -> str:
    """语义判断实际请求的 URL（按协议）。"""
    if profile.judge_protocol == PROTO_DISABLED:
        return ""
    return chat_completions_url(profile.base_url)


def asr_target_url(profile: ProviderProfile) -> str:
    """转写实际请求的 URL（按协议）。"""
    if profile.asr_protocol == PROTO_VOLC_ARK:
        return profile.asr_endpoint
    if profile.asr_protocol == PROTO_OPENAI:
        return audio_transcriptions_url(profile.base_url)
    return ""


def asr_target_host(profile: ProviderProfile) -> str:
    """转写会发往哪台主机（外发告知用）。"""
    url = asr_target_url(profile)
    return egress_host(url) if url else "（未配置地址）"


def describe_egress(
    profile: ProviderProfile,
    capabilities: Iterable[str] = (CAP_ASR, CAP_JUDGE),
) -> str:
    """给界面与日志用的一句话：这次会把什么、发往哪里。

    这是"外发告知"的唯一文案来源——界面、确认弹窗与日志必须读同一份，
    否则三处措辞会各自漂移。
    """
    parts: list[str] = []
    wanted = [cap for cap in capabilities if profile.supports(cap)]
    if CAP_ASR in wanted:
        parts.append(f"转写：上传整条音轨 → {asr_target_host(profile)}")
    if CAP_JUDGE in wanted:
        host = egress_host(profile.base_url) if profile.base_url else "（未配置地址）"
        parts.append(f"语义判断：上传候选点前后台词文本（不含画面）→ {host}")
    if not parts:
        return f"{profile.label}：尚未配置可用的云端能力。"
    return "；".join(parts)


def missing_config_reason(profile: ProviderProfile, capability: str) -> str | None:
    """该能力的**配置**缺了什么；齐了返回 None。

    刻意不含 `enabled` / `allow_upload` / Key 三项——那两个开关与凭据属于
    "为什么没生效"的另一半。抽出来的理由是它有两个调用方：`block_reason()`
    与模型对话框的连通性测试前置检查；两处各写一份文案必然漂移。
    """
    label = CAP_LABELS.get(capability, capability)
    prefix = f"云端{label}（{profile.label}）"
    if capability == CAP_JUDGE:
        if profile.judge_protocol == PROTO_DISABLED:
            return f"{prefix}未选择语义判断协议（judge_protocol）"
        if not profile.judge_model:
            return f"{prefix}未填写模型名（judge_model）"
        if not profile.base_url:
            return f"{prefix}未填写接口地址（base_url）"
        return None
    if capability == CAP_ASR:
        if profile.asr_protocol == PROTO_DISABLED:
            return f"{prefix}未选择转写协议（asr_protocol）"
        if profile.asr_protocol == PROTO_VOLC_ARK:
            if not profile.asr_endpoint:
                return f"{prefix}未配置火山转写接口（asr_endpoint）"
            if not profile.asr_resource_id:
                return f"{prefix}未配置火山转写资源 ID（asr_resource_id）"
            return None
        if profile.asr_protocol == PROTO_OPENAI:
            if not profile.base_url:
                return f"{prefix}未填写接口地址（base_url）"
            if not profile.asr_model_name:
                return f"{prefix}未填写转写模型名（asr_model_name）"
            return None
        return f"{prefix}不认识的转写协议：{profile.asr_protocol}"
    raise ValueError(f"未知能力：{capability}")


def block_reason(profile: ProviderProfile, capability: str) -> str | None:
    """该能力的云端档案为什么不能生效；可用时返回 None。

    每一种原因都要单独说清（未启用 / 未勾选外发 / 缺协议 / 缺模型名 / 缺地址 /
    缺 Key），否则用户只会看到"没生效"而不知道该改哪一项。
    """
    label = CAP_LABELS.get(capability, capability)
    if not profile.enabled:
        return f"云端{label}（{profile.label}）已选中但档案未启用"
    if not profile.allow_upload:
        return f"云端{label}（{profile.label}）已选中但未勾选「允许外发素材」"
    missing = missing_config_reason(profile, capability)
    if missing is not None:
        return missing
    if not profile.resolved_api_key():
        env_name = ENV_KEY_TEMPLATE.format(id=profile.id.upper())
        return f"云端{label}（{profile.label}）没有 API Key（可写入配置，或设环境变量 {env_name}）"
    return None


def validate_profile(
    profile: ProviderProfile, *, existing_ids: Iterable[str] = ()
) -> list[str]:
    """校验一份（用户正在编辑的）档案；返回问题清单，空清单 = 可以保存。

    纯逻辑放在核心层，"什么算合法"就只有一处定义：界面负责显示，测试跑得快。

    只拦**结构性**问题：id / 名称 / URL / 转写模型名 / 火山专用字段。
    "语义判断选了协议却没填模型名"**不**在这里拦——预置的方舟档案本身就是这种
    状态（等用户填推理接入点 ID），由 `missing_config_reason()` 在界面与日志里
    如实说明"这项能力还缺什么"，比在保存时硬拦更贴近实际。
    """
    problems: list[str] = []
    profile_id = (profile.id or "").strip()
    taken = {str(item).strip() for item in existing_ids if str(item).strip()}
    if not profile_id:
        problems.append("id 不能为空")
    elif not _PROFILE_ID_RE.fullmatch(profile_id):
        problems.append("id 只能用小写字母/数字/短横线/下划线（首字符不能是短横线）")
    elif profile_id in taken:
        problems.append(f"id「{profile_id}」已被占用")
    if not (profile.label or "").strip():
        problems.append("名称（label）不能为空")

    def check_url(value: str, field: str) -> None:
        parsed = urlparse((value or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            problems.append(f"{field}必须是 http(s) 地址")

    if profile.judge_protocol not in PROTOCOLS_BY_CAP[CAP_JUDGE]:
        problems.append(
            "语义判断不支持的协议："
            f"{PROTO_LABELS.get(profile.judge_protocol, profile.judge_protocol)}"
        )
    if profile.asr_protocol not in PROTOCOLS_BY_CAP[CAP_ASR]:
        problems.append(
            f"转写不支持的协议：{PROTO_LABELS.get(profile.asr_protocol, profile.asr_protocol)}"
        )

    needs_base_url = (
        profile.judge_protocol != PROTO_DISABLED or profile.asr_protocol == PROTO_OPENAI
    )
    if needs_base_url and not (profile.base_url or "").strip():
        problems.append("接口地址（base_url）不能为空")
    elif (profile.base_url or "").strip():
        check_url(profile.base_url, "接口地址（base_url）")

    if profile.asr_protocol != PROTO_DISABLED and not (profile.asr_model_name or "").strip():
        problems.append("已选转写协议，必须填模型名（asr_model_name）")
    if profile.asr_protocol == PROTO_VOLC_ARK:
        if not (profile.asr_endpoint or "").strip():
            problems.append("火山方舟转写必须填接口地址（asr_endpoint）")
        else:
            check_url(profile.asr_endpoint, "转写接口地址（asr_endpoint）")
        if not (profile.asr_resource_id or "").strip():
            problems.append("火山方舟转写必须填资源 ID（asr_resource_id）")
    return problems