"""档案 → 适配器的映射。

**唯一一处"加一家厂商就改一行"的地方。** 上层（`CloudLlmJudge` / 后续
`CloudAsrEngine`）只认 `providers.py` 里的协议，不认识任何厂商；新厂商的接入 =
实现一个适配器 + 在内置档案里加一份默认值 + 在这里登记一行。

首版只有火山方舟（`cloud_volc.py`）。其余厂商（阿里 Paraformer、腾讯 ASR、
讯飞流式）按设计 §9 留插槽——接入前必须先自证**词级时间戳**，否则只能按
"质量降级"路径标注（设计 §7）。
"""

from __future__ import annotations

from .cloud_volc import VolcArkChatTransport, VolcFlashAsrTransport
from .providers import AsrTransport, JudgeTransport, ProviderProfile

__all__ = ["build_asr_transport", "build_judge_transport"]

#: 档案 id → 适配器工厂。加厂商时在这里登记两行（有哪条能力就登记哪条）。
JUDGE_TRANSPORTS = {
    "volc": lambda profile: VolcArkChatTransport(
        profile.base_url,
        profile.resolved_api_key(),
        profile.judge_model,
        use_system_proxy=profile.use_system_proxy,
    ),
}

ASR_TRANSPORTS = {
    "volc": lambda profile: VolcFlashAsrTransport(
        profile.asr_endpoint,
        profile.resolved_api_key(),
        profile.asr_resource_id,
        profile.asr_model_name,
        use_system_proxy=profile.use_system_proxy,
    ),
}


def build_judge_transport(profile: ProviderProfile) -> JudgeTransport | None:
    """按档案 id 构造语义判断适配器；未登记（或该厂商不提供）时返回 None。"""
    factory = JUDGE_TRANSPORTS.get(profile.id)
    return factory(profile) if factory is not None else None


def build_asr_transport(profile: ProviderProfile) -> AsrTransport | None:
    """按档案 id 构造转写适配器；未登记时返回 None。

    接入新厂商前必须先确认它返回**词级时间戳**：没有词级结果的适配器只能走
    "质量降级"路径（设计 §7），不能假装有词边界。
    """
    factory = ASR_TRANSPORTS.get(profile.id)
    return factory(profile) if factory is not None else None