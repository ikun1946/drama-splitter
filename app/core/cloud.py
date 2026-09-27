"""档案 → 适配器的映射（**按协议分派，不认厂商**）。

为什么改成按协议
----------------
以前这里是"厂商 id → 适配器"的硬编码表：加一家厂商就要来改代码。现在 dispatch
的键是**协议**（停用 / OpenAI 兼容 / 火山方舟），而协议是封闭集合——于是
**加一家厂商 = 在 `DEFAULT_PROFILES` 里加一份数据**，本文件一行都不用改。

`id` 只剩下三个用途：`backend_prefs` 的引用名、`DRAMA_<ID>_API_KEY` 环境变量名、
日志里的标识。厂商差异只允许留在各自的适配器里
（`cloud_openai.py` / `cloud_volc.py`）；通用 HTTP 在 `cloud_http.py`。

新增一种*协议*才需要写适配器并在这里登记一行；接入前必须先自证**词级时间戳**
（`docs/云端模型接入_设计.md` §9），否则只能走"分句级降级"。
"""

from __future__ import annotations

from .cloud_openai import OpenAiAsrTransport, OpenAiChatTransport, list_models as _list_openai_models
from .cloud_volc import VolcFlashAsrTransport
from .providers import (
    PROTO_LABELS,
    PROTO_OPENAI,
    PROTO_VOLC_ARK,
    AsrTransport,
    JudgeTransport,
    ProviderError,
    ProviderProfile,
)

__all__ = ["build_asr_transport", "build_judge_transport", "list_models"]

#: 语义判断协议 → 适配器工厂。方舟的 chat 也是 OpenAI 形态，所以没有"火山"这一项。
JUDGE_BUILDERS = {
    PROTO_OPENAI: lambda profile: OpenAiChatTransport(
        profile.base_url,
        profile.resolved_api_key(),
        profile.judge_model,
        label=profile.label,
        use_system_proxy=profile.use_system_proxy,
    ),
}

#: 转写协议 → 适配器工厂。
ASR_BUILDERS = {
    PROTO_OPENAI: lambda profile: OpenAiAsrTransport(
        profile.base_url,
        profile.resolved_api_key(),
        profile.asr_model_name,
        label=profile.label,
        use_system_proxy=profile.use_system_proxy,
    ),
    PROTO_VOLC_ARK: lambda profile: VolcFlashAsrTransport(
        profile.asr_endpoint,
        profile.resolved_api_key(),
        profile.asr_resource_id,
        profile.asr_model_name,
        use_system_proxy=profile.use_system_proxy,
    ),
}


def build_judge_transport(profile: ProviderProfile) -> JudgeTransport | None:
    """按协议构造语义判断适配器；该协议没登记适配器时返回 None（调用方回退本地）。"""
    builder = JUDGE_BUILDERS.get(profile.judge_protocol)
    return builder(profile) if builder is not None else None


def build_asr_transport(profile: ProviderProfile) -> AsrTransport | None:
    """按协议构造转写适配器；该协议没登记适配器时返回 None（调用方回退本地）。

    接入新协议前必须先确认它返回**词级时间戳**：没有词级结果的适配器只能走
    "质量降级"路径（设计 §7），不能假装有词边界。
    """
    builder = ASR_BUILDERS.get(profile.asr_protocol)
    return builder(profile) if builder is not None else None


def list_models(profile: ProviderProfile) -> list[str]:
    """按协议拉取该厂商的模型清单（界面里"按厂商刷新模型列表"走这里）。

    目前只有 OpenAI 兼容协议有清单端点；火山方舟的语义判断虽然也是 OpenAI 形态，
    但它认的是**推理接入点 ID**（ep-xxx，不在 /models 里），因此会在那儿报错——
    文案里说清"手动填写"，而不是假装这家没有模型。
    """
    if profile.judge_protocol == PROTO_OPENAI or profile.asr_protocol == PROTO_OPENAI:
        return _list_openai_models(
            profile.base_url,
            profile.resolved_api_key(),
            use_system_proxy=profile.use_system_proxy,
        )
    protocol = PROTO_LABELS.get(profile.asr_protocol, profile.asr_protocol)
    raise ProviderError(
        f"「{protocol}」协议没有模型清单接口，请手动填写模型名"
    )