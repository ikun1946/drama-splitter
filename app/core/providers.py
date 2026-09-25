"""厂商 API 档案（供应商配置）与适配器协议。

为什么需要这一层
----------------
本工具的转写与语义判断原先只允许本地模型（`semantic.py` 头部曾写死"没有任何云端
调用路径"）。放开之后，风险不在"能不能调通"，而在**外发范围失控**与**厂商差异
渗进主链路**。因此把"厂商是什么"全部收在这个模块里，只暴露三样东西：

- :class:`ProviderProfile` 一份档案（地址、Key、模型名、是否允许外发）；
- :class:`ProfileRegistry` 档案读取（用户配置 + 内置默认档案合并）；
- :class:`JudgeTransport` / :class:`AsrTransport` 两个**适配器协议**。

调用方（CloudLlmJudge / CloudAsrEngine）只认协议；鉴权、信封、时间戳单位这些
厂商差异只允许出现在适配器实现里（如 `cloud_volc.py`）。

默认不外发
----------
档案内置 `enabled=False` / `allow_upload=False`。`backend_prefs` 指向某档案但开关
没开时，调用方按本地路径执行并在日志说明——**不给任何隐式外发路径**。
"""

from __future__ import annotations

import os
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
    "AsrTransport",
    "JudgeTransport",
    "ProfileRegistry",
    "ProviderError",
    "ProviderProfile",
    "ProviderSegment",
    "ProviderTranscript",
    "ProviderWord",
    "block_reason",
    "describe_egress",
    "egress_host",
    "merge_profiles",
    "resolve_api_key",
]

CAP_ASR = "asr"
CAP_JUDGE = "judge"

#: 能力 → 界面/日志里的中文名（选云端却没生效时，要说清是哪一项能力）
CAP_LABELS = {CAP_ASR: "转写", CAP_JUDGE: "语义判断"}

#: Key 的环境变量前缀：`DRAMA_<档案 id 大写>_API_KEY`
ENV_KEY_TEMPLATE = "DRAMA_{id}_API_KEY"

#: 语义判断一次性请求的默认超时（秒）
DEFAULT_TIMEOUT = 120.0


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
    asr_model_name: str = "bigmodel"
    judge_model: str = ""
    api_key: str = ""
    enabled: bool = False
    allow_upload: bool = False
    use_system_proxy: bool = False

    # ---- 能力 ----------------------------------------------------------

    def supports(self, capability: str) -> bool:
        if capability == CAP_JUDGE:
            return bool(self.judge_model)
        if capability == CAP_ASR:
            return bool(self.asr_endpoint and self.asr_resource_id)
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
            "api_key": self.api_key,
            "enabled": self.enabled,
            "allow_upload": self.allow_upload,
            "use_system_proxy": self.use_system_proxy,
        }

    @classmethod
    def from_json(cls, data: dict, *, fallback: "ProviderProfile | None" = None) -> "ProviderProfile":
        """容错解析：类型不对的字段回落默认值，绝不因为配置坏了而让启动失败。

        未知字段丢弃、缺失字段用默认值；``fallback`` 给定时，以它为基底覆盖——
        用户配置只写了两个字段也能合并成一份完整档案。
        """
        base = fallback or DEFAULT_PROFILES[0]
        payload = data if isinstance(data, dict) else {}

        def text(key: str, default: str) -> str:
            value = payload.get(key, default)
            return value.strip() if isinstance(value, str) else default

        def flag(key: str, default: bool) -> bool:
            value = payload.get(key, default)
            return bool(value) if isinstance(value, (bool, int)) else default

        return replace(
            base,
            id=text("id", base.id) or base.id,
            label=text("label", base.label) or base.label,
            base_url=text("base_url", base.base_url),
            asr_endpoint=text("asr_endpoint", base.asr_endpoint),
            asr_resource_id=text("asr_resource_id", base.asr_resource_id),
            asr_model_name=text("asr_model_name", base.asr_model_name) or base.asr_model_name,
            judge_model=text("judge_model", base.judge_model),
            api_key=text("api_key", base.api_key),
            enabled=flag("enabled", base.enabled),
            allow_upload=flag("allow_upload", base.allow_upload),
            use_system_proxy=flag("use_system_proxy", base.use_system_proxy),
        )

    def with_updates(self, **changes: Any) -> "ProviderProfile":
        return replace(self, **changes)


#: 内置档案。首版只有火山方舟 / 豆包——地址与资源 ID 取自官方文档。
DEFAULT_PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        id="volc",
        label="火山方舟 / 豆包",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        asr_endpoint="https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        asr_resource_id="volc.bigasr.auc_turbo",
        asr_model_name="bigmodel",
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
        parts.append(f"转写：上传整条音轨 → {egress_host(profile.asr_endpoint)}")
    if CAP_JUDGE in wanted:
        parts.append(f"语义判断：上传候选点前后台词文本（不含画面）→ {egress_host(profile.base_url)}")
    if not parts:
        return f"{profile.label}：尚未配置可用的云端能力。"
    return "；".join(parts)


def block_reason(profile: ProviderProfile, capability: str) -> str | None:
    """该能力的云端档案为什么不能生效；可用时返回 None。

    每一种原因都要单独说清（未启用 / 未勾选外发 / 缺模型名 / 缺 Key / 该能力未配置），
    否则用户只会看到"没生效"而不知道该改哪一项。转写与语义判断共用这一份判定。
    """
    label = CAP_LABELS.get(capability, capability)
    if not profile.enabled:
        return f"云端{label}（{profile.label}）已选中但档案未启用"
    if not profile.allow_upload:
        return f"云端{label}（{profile.label}）已选中但未勾选「允许外发素材」"
    if capability == CAP_JUDGE and not profile.judge_model:
        return f"云端{label}（{profile.label}）未填写模型名（judge_model）"
    if capability == CAP_ASR and not profile.supports(CAP_ASR):
        return f"云端{label}（{profile.label}）未配置转写接口（asr_endpoint / asr_resource_id）"
    if not profile.resolved_api_key():
        env_name = ENV_KEY_TEMPLATE.format(id=profile.id.upper())
        return f"云端{label}（{profile.label}）没有 API Key（可写入配置，或设环境变量 {env_name}）"
    return None