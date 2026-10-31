"""模型的参数档案：上下文窗口 / 思考等级 / 模态。

为什么要单独一层
----------------
不少厂商的 ``GET /models`` 只返回模型名；也有一些网关会附上下文窗口、
输入模态或能力字段。厂商真正返回了元数据时优先使用它；没返回时才用内置
知识库 + 名称模式推断，再不行由用户手填。这个模块承载解析之后的统一结构。

**未知就留空**
--------------
这是整个模块最重要的一条规矩：

- ``context_window == 0`` → 不做任何截断，按原来的行为跑；
- ``reasoning == ""`` → 请求里**不带** ``reasoning_effort``；
- ``modalities == ()`` → 不做能力过滤，模型照样能指派给任意能力。

留空 = 不干预，因此自动填充只填有把握的：猜错的代价是"少截断一点"或"少发一个
参数"，而不是"把原本能用的模型弄坏"。把不确定的猜成确定，才是这类功能最容易
出事的地方——`reasoning_effort` 盲传给不支持的模型会直接 400。

数值都是**估计值**
------------------
厂商会改规格，本模块的表也会过时。所以推断出来的值在界面上标着"自动"，
可以随时改；截断时还留了安全余量（见 :data:`RESPONSE_HEADROOM_RATIO`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ModelInfo",
    "from_catalog_item",
    "infer_model_info",
    "merge_catalog_info",
    "format_context",
    "estimate_max_input_chars",
    "REASONING_LEVELS",
    "MODALITIES",
    "RESPONSE_HEADROOM_RATIO",
    "CHARS_PER_TOKEN",
]


#: 请求里 ``reasoning_effort`` 允许的取值（与 OpenAI 的取值一致）
REASONING_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high")

#: 模态取值。转写要求 "audio"，语义判断要求 "text"
MODALITIES: tuple[str, ...] = ("text", "image", "audio")

MODALITY_LABELS: dict[str, str] = {
    "text": "文本",
    "image": "图像",
    "audio": "音频",
}

#: 模型清单返回体里的上下文窗口字段（按可信度从高到低）。
#: 覆盖 OpenAI 兼容网关常见写法、OpenRouter 的顶层字段与 Hugging Face 卡片字段。
_CONTEXT_KEYS: tuple[str, ...] = (
    "context_window",
    "context_length",
    "max_context_length",
    "max_context_tokens",
    "max_input_tokens",
    "input_token_limit",
)

#: 上下文窗口可能藏在嵌套对象里；每个元组是一条由外到内的路径。
_CONTEXT_PATHS: tuple[tuple[str, ...], ...] = (
    ("limits", "context_window"),
    ("limits", "context_length"),
    ("limits", "max_input_tokens"),
    ("top_provider", "context_length"),
    ("architecture", "context_window"),
)

#: 输入模态字段：有的网关给 ``["text","image"]``，有的给 ``{input_modalities: …}``。
_MODALITY_KEYS: tuple[str, ...] = (
    "input_modalities",
    "input_modality",
    "supported_modalities",
    "modalities",
    "modality",
)
_MODALITY_PATHS: tuple[tuple[str, ...], ...] = (
    ("architecture", "input_modalities"),
    ("architecture", "input_modality"),
    ("architecture", "modalities"),
)

#: 网关/模型卡里常见的模态别名 → 本程序的三个内部值。
_MODALITY_ALIASES: dict[str, str] = {
    "text": "text",
    "language": "text",
    "image": "image",
    "vision": "image",
    "audio": "audio",
    "speech": "audio",
}


def _positive_int(value: object) -> int:
    """把厂商返回的窗口字段转成正整数；读不出或非正数返回 0。"""
    if isinstance(value, bool):
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _nested_value(item: dict, path: tuple[str, ...]) -> object:
    value: object = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _catalog_context(item: dict) -> tuple[int, str]:
    """从一条 /models 条目读上下文窗口，返回 ``(值, 来源字段)``。"""
    for key in _CONTEXT_KEYS:
        value = _positive_int(item.get(key))
        if value:
            return value, key
    for path in _CONTEXT_PATHS:
        value = _positive_int(_nested_value(item, path))
        if value:
            return value, ".".join(path)
    return 0, ""


def _catalog_modalities(item: dict) -> tuple[tuple[str, ...], str]:
    """从一条 /models 条目读输入模态，返回 ``(内部模态, 来源字段)``。"""
    raw: object = None
    source = ""
    for key in _MODALITY_KEYS:
        candidate = item.get(key)
        if candidate not in (None, "", [], {}):
            raw, source = candidate, key
            break
    if raw is None:
        for path in _MODALITY_PATHS:
            candidate = _nested_value(item, path)
            if candidate not in (None, "", [], {}):
                raw, source = candidate, ".".join(path)
                break
    if raw is None:
        return (), ""

    values = raw if isinstance(raw, list) else [raw]
    picked: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        alias = _MODALITY_ALIASES.get(text)
        if alias and alias not in picked:
            picked.append(alias)
    return tuple(key for key in MODALITIES if key in picked), source


def from_catalog_item(item: object) -> ModelInfo | None:
    """把厂商 ``/models`` 的一条字典解析成 :class:`ModelInfo`。

    只认能确定的事实：名称、上下文窗口、输入模态。厂商没有给的字段保持
    空值（0 / 空元组）；**不用名称推断补这里的空**，让调用方能区分
    “接口明确返回了”与“本地猜的”。不是字典或缺模型名时返回 None。

    适配的常见形状包括：

    - 顶层 ``context_length`` / ``context_window`` / ``max_input_tokens``；
    - OpenRouter 风格 ``top_provider.context_length``、``architecture.input_modalities``；
    - ``limits.context_window`` 这类嵌套对象；
    - ``input_modalities`` / ``modalities`` 字符串或数组。
    """
    if not isinstance(item, dict):
        return None
    name = str(
        item.get("id") or item.get("name") or item.get("model") or ""
    ).strip()
    if not name:
        return None
    context_window, context_source = _catalog_context(item)
    modalities, modality_source = _catalog_modalities(item)
    if not context_window and not modalities:
        return None
    sources = [source for source in (context_source, modality_source) if source]
    return ModelInfo(
        name=name,
        context_window=context_window,
        modalities=modalities,
        source="catalog",
        note="厂商 /models 返回：" + "、".join(sources),
    )


def merge_catalog_info(current: ModelInfo, incoming: ModelInfo) -> ModelInfo:
    """把厂商返回的规格合并到现有档案；**手动值始终优先**。

    - ``manual``：一个字段都不覆盖，避免刷新模型列表后冲掉用户手填；
    - ``catalog`` / ``auto``：用接口给出的非空上下文和模态补齐空位；
      接口没给某一项时保留原值（可能是名称推断值）。
    """
    if current.source == "manual":
        return current
    return current.with_updates(
        context_window=current.context_window or incoming.context_window,
        modalities=current.modalities or incoming.modalities,
        source="catalog" if (incoming.context_window or incoming.modalities) else current.source,
        note=incoming.note or current.note,
    )

#: 中英混排的经验值：中文约 1 字 1 token，英文约 4 字符 1 token，混排取中间值。
#: 偏保守（宁可少截断），因为截多了会静默丢掉台词证据。
CHARS_PER_TOKEN = 2

#: 给模型输出预留的上下文比例。截断的是**输入**，总窗口还要留地方放回复。
RESPONSE_HEADROOM_RATIO = 0.25


def format_context(window: int) -> str:
    """128000 → "128k"，1048576 → "1M"。"""
    if window <= 0:
        return "—"
    if window >= 1_000_000:
        return f"{window / 1_000_000:g}M"
    if window >= 1000:
        return f"{round(window / 1000):g}k"
    return str(window)


def estimate_max_input_chars(window: int) -> int:
    """上下文窗口 → 输入最多多少字符；未知（<=0）返回 0（表示不限制）。"""
    if window <= 0:
        return 0
    usable = int(window * (1 - RESPONSE_HEADROOM_RATIO))
    return max(0, usable * CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ModelInfo:
    """一个模型的参数档案。**空值 = 未知 = 不干预**，见模块 docstring。"""

    name: str
    #: 上下文窗口（token）。0 = 未知。
    context_window: int = 0
    #: 要发给接口的思考等级；空串 = 不发这个参数。
    reasoning: str = ""
    #: 支持的模态；空元组 = 未知，不做能力过滤。
    modalities: tuple[str, ...] = ()
    #: 这些值是怎么来的："catalog" 厂商 /models / "auto" 名称推断 / "manual" 手填
    source: str = "auto"
    #: 自动推断的依据，展示在 tooltip 上，方便用户判断该不该信
    note: str = ""

    # ---- 能力判定 ------------------------------------------------------

    def is_empty(self) -> bool:
        """三个字段都没填 = 用户什么都没配，落盘时直接不记这一条。"""
        return not self.context_window and not self.reasoning and not self.modalities

    def supports_modality(self, modality: str) -> bool:
        """该模型能不能处理这个模态。未知（没填模态）一律放行。"""
        if not self.modalities:
            return True
        return modality in self.modalities

    def can_judge(self) -> bool:
        return self.supports_modality("text")

    def can_transcribe(self) -> bool:
        return self.supports_modality("audio")

    # ---- 序列化 --------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "context_window": self.context_window,
            "reasoning": self.reasoning,
            "modalities": list(self.modalities),
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: object) -> "ModelInfo | None":
        """容错解析：类型不对的字段回落默认值，绝不因为配置坏了而让启动失败。"""
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None

        def positive_int(key: str) -> int:
            value = data.get(key)
            try:
                number = int(value)
            except (TypeError, ValueError):
                return 0
            return number if number > 0 else 0

        reasoning = str(data.get("reasoning") or "").strip()
        if reasoning not in REASONING_LEVELS:
            reasoning = ""
        raw = data.get("modalities")
        modalities: tuple[str, ...] = ()
        if isinstance(raw, list):
            kept = tuple(
                item.strip()
                for item in raw
                if isinstance(item, str) and item.strip() in MODALITIES
            )
            modalities = tuple(dict.fromkeys(kept))     # 去重且保序
        source = str(data.get("source") or "").strip()
        return cls(
            name=name,
            context_window=positive_int("context_window"),
            reasoning=reasoning,
            modalities=modalities,
            source=source if source in ("catalog", "auto", "manual") else "auto",
            note=str(data.get("note") or "").strip(),
        )

    def with_updates(self, **changes) -> "ModelInfo":
        from dataclasses import replace

        return replace(self, **changes)


@dataclass(frozen=True)
class _Known:
    """知识库条目：上下文窗口（token）+ 模态 + 默认思考等级。"""

    context_window: int = 0
    modalities: tuple[str, ...] = ()
    reasoning: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
#
# 键是**小写**的模型名片段，按"越长越具体"排序匹配（`gpt-4o-mini` 必须先于
# `gpt-4o` 命中，否则小模型会拿到大模型的窗口）。数值取厂商公开文档里的上下文
# 窗口，全部标注为估计值——厂商改规格时这张表会过时，所以界面上可改。
#
# `reasoning` 只在**确定支持 reasoning_effort 参数**的系列上填（OpenAI 的
# o 系列 / gpt-5 系列）。deepseek-reasoner、qwen-thinking 这类"自带思考"的
# 模型走的是各自的开关，不吃这个参数，填了反而可能 400。
# ---------------------------------------------------------------------------
_KNOWN: tuple[tuple[str, _Known], ...] = (
    # ---- OpenAI ------------------------------------------------------
    ("gpt-3.5-turbo", _Known(16_385, ("text",))),
    ("gpt-4-turbo", _Known(128_000, ("text", "image"))),
    ("gpt-4o-mini", _Known(128_000, ("text", "image", "audio"))),
    ("gpt-4o", _Known(128_000, ("text", "image", "audio"))),
    ("gpt-4.1-mini", _Known(1_000_000, ("text", "image", "audio"))),
    ("gpt-4.1", _Known(1_000_000, ("text", "image", "audio"))),
    ("o4-mini", _Known(200_000, ("text", "image"), "medium")),
    ("o3-mini", _Known(200_000, ("text",), "medium")),
    ("o3", _Known(200_000, ("text", "image"), "medium")),
    ("o1-mini", _Known(128_000, ("text",), "medium")),
    ("o1", _Known(200_000, ("text", "image"), "medium")),
    ("gpt-5-mini", _Known(400_000, ("text", "image"), "medium")),
    ("gpt-5", _Known(400_000, ("text", "image"), "medium")),
    # ---- Anthropic ----------------------------------------------------
    ("claude-3-haiku", _Known(200_000, ("text", "image"))),
    ("claude-3-opus", _Known(200_000, ("text", "image"))),
    ("claude-3-5-sonnet", _Known(200_000, ("text", "image"))),
    ("claude-3-7-sonnet", _Known(200_000, ("text", "image"))),
    # ---- 国内 ---------------------------------------------------------
    ("deepseek-reasoner", _Known(64_000, ("text",))),
    ("deepseek-chat", _Known(64_000, ("text",))),
    ("qwen3", _Known(131_072, ("text",))),
    ("qwen-max", _Known(32_768, ("text",))),
    ("qwen-plus", _Known(131_072, ("text",))),
    ("qwen-turbo", _Known(1_000_000, ("text",))),
    ("qwen-vl", _Known(131_072, ("text", "image"))),
    ("glm-4v", _Known(128_000, ("text", "image"))),
    ("glm-4", _Known(128_000, ("text",))),
    ("moonshot-v1-128k", _Known(131_072, ("text",))),
    ("moonshot-v1-32k", _Known(32_768, ("text",))),
    ("moonshot-v1-8k", _Known(8_192, ("text",))),
    ("kimi-k2", _Known(128_000, ("text",))),
    ("glm-4.5", _Known(128_000, ("text",))),
    # ---- 语音 ---------------------------------------------------------
    ("whisper-1", _Known(0, ("text", "audio"), note="转写用，窗口不适用")),
    ("sensevoice", _Known(0, ("text", "audio"), note="转写用，窗口不适用")),
    ("paraformer", _Known(0, ("text", "audio"), note="转写用，窗口不适用")),
    ("fun-asr", _Known(0, ("text", "audio"), note="转写用，窗口不适用")),
    ("bigmodel", _Known(0, ("text", "audio"), note="火山大模型录音识别")),
)


# ---------------------------------------------------------------------------
# 名称模式
# ---------------------------------------------------------------------------

#: 名字里带这些片段 → 音频输入（转写类模型）。**只在没命中知识库时用**。
_AUDIO_HINTS = (
    "whisper", "asr", "speech", "sensevoice", "paraformer", "fun-asr",
    "audio", "voice", "speech-recognition",
)
_IMAGE_HINTS = ("vl", "vision", "omni", "multimodal", "-m3")
#: 名字里带这些片段 → 确实是推理模型。注意：这**不代表**支持 reasoning_effort，
#: 只用于在界面上标一句"支持思考"。
_REASONING_HINTS = ("reasoning", "reasoner", "thinking", "-r1", "o1", "o3", "o4")
#: 不该指派给任何能力的模型（拿它们做对话/转写只会报错）
_EMBEDDING_HINTS = ("embedding", "embed", "bge-", "text-embedding")

#: `-32k` / `-128k` / `-1m` 这类后缀是厂商直接写在名字里的窗口，最可信
_SUFFIX_CONTEXT = re.compile(r"[-_](\d+)(k|m)\b", re.IGNORECASE)


def _match_known(lowered: str) -> _Known | None:
    """按"最长片段优先"匹配知识库。"""
    best: tuple[int, _Known] | None = None
    for fragment, known in _KNOWN:
        if fragment in lowered and (best is None or len(fragment) > best[0]):
            best = (len(fragment), known)
    return best[1] if best else None


def _context_from_suffix(lowered: str) -> int:
    """从 `-128k` / `-1m` 这样的后缀读窗口；读不出返回 0。"""
    match = None
    for match in _SUFFIX_CONTEXT.finditer(lowered):
        pass                      # 取最后一个：`-32k-0613` 的 32k 比 0613 更像窗口
    if not match:
        return 0
    value, unit = int(match.group(1)), match.group(2).lower()
    return value * (1_000_000 if unit == "m" else 1_000)


def infer_model_info(name: str) -> ModelInfo:
    """按模型名推断参数档案。

    顺序：知识库命中 → 名字后缀（``-128k`` 这类，最可信）→ 关键词模式 →
    都不确定就**全部留空**（不干预）。返回值 ``source="auto"``。
    """
    raw = (name or "").strip()
    if not raw:
        return ModelInfo(name="")
    lowered = raw.lower()

    # 嵌入类先单独判掉：它在知识库里的条目只覆盖了部分命名
    if any(hint in lowered for hint in _EMBEDDING_HINTS):
        return ModelInfo(
            name=raw,
            context_window=8_192,
            modalities=("text",),
            note="嵌入模型，不能用来对话或转写",
        )

    known = _match_known(lowered)
    if known is not None:
        window = known.context_window
        source_note = known.note
        if window == 0:
            window = _context_from_suffix(lowered)
        return ModelInfo(
            name=raw,
            context_window=window,
            reasoning=known.reasoning,
            modalities=known.modalities,
            note=source_note or "按模型名匹配到的已知规格（估计值，可改）",
        )

    modalities: list[str] = ["text"]
    if any(hint in lowered for hint in _IMAGE_HINTS):
        modalities.append("image")
    if any(hint in lowered for hint in _AUDIO_HINTS):
        modalities.append("audio")

    # 关键词能判出模态，但**判不出上下文窗口**，也不填 reasoning——
    # 猜一个窗口去截断台词的代价，比不截断大得多。
    return ModelInfo(
        name=raw,
        context_window=_context_from_suffix(lowered),
        modalities=tuple(modalities),
        note="按名称推断的模态；上下文窗口与思考等级需手填",
    )


def supports_reasoning(name: str) -> bool:
    """这个名字看起来是不是推理模型（只用于界面提示，不决定发不发参数）。"""
    lowered = (name or "").lower()
    if _match_known(lowered) is not None:
        return False          # 知识库命中的，reasoning 字段已经说清楚了
    return any(hint in lowered for hint in _REASONING_HINTS)


#: 供测试与工具导入的字段顺序（序列化时保持稳定）
FIELD_ORDER: tuple[str, ...] = (
    "name",
    "context_window",
    "reasoning",
    "modalities",
    "source",
    "note",
)
