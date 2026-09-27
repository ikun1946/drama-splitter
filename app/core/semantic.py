"""语义判断层：事件索引、请求校验、预算控制与本地模型适配（阶段4）。

设计依据：
- §11.1 多模态分析的输出必须记录**推荐等级、支持证据、风险、审核状态**；
  说话人身份只能标记"未确认"，除非有强证据。
- §11.3 删除伪精确概率：不得输出"置信度 94%"这类数字。等级 + 证据 + 风险即可。
- §8.4 控制分析规模：先廉价规则筛选，再对优质候选做语义分析；限制每个窗口的
  候选数量及总调用预算；**若为限流缩小了搜索范围，必须记录限制**。
- §10.4 预算耗尽属独立的无解成因（BUDGET_EXHAUSTED），与"无路径"分开报告。
- §6 判断不得阻塞界面线程（由上层 worker 保证，本模块自身是同步纯计算）。

**「素材不出机器」的落点（已改为用户可控开关，默认关闭）**
------------------------------------------------------
本模块的默认后端仍是本地模型（llama.cpp → Ollama），云端判定器
（:class:`CloudLlmJudge`，见设计 §4）只有用户在"模型 → 云端模型"里显式
启用档案**并**勾选「允许外发素材」后才会被选中；两个开关任一为关，走本地路径并
在日志说明。判断只外发**候选点前后的台词文本片段**，不发画面。

**降级是常态而非异常**
--------------------
模型未下载、推理栈缺失、输出无法解析，都会走降级路径：候选保持规则评分、
审核状态保持 pending、limitations 记录原因。**绝不因为模型缺席而让分析失败**，
也**绝不伪造"AI 已审核"的状态**。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence

from .candidates import CandidatePoint
from .asr import Transcript, TranscriptSentence
from .cloud import build_judge_transport
from .probe import LEVEL_INFO, LEVEL_WARN, Issue
from .providers import (
    CAP_JUDGE,
    PROTO_LABELS,
    JudgeTransport,
    ProviderError,
    ProviderProfile,
    block_reason,
    describe_egress,
)
from .shots import SceneBoundary

__all__ = [
    "EventIndex",
    "JudgmentRequest",
    "JudgmentVerdict",
    "Budget",
    "BudgetExhaustedError",
    "ModelUnavailableError",
    "SemanticJudge",
    "NullJudge",
    "LocalLlmJudge",
    "OllamaJudge",
    "CloudLlmJudge",
    "ResilientJudge",
    "JudgeOutcome",
    "judge_candidates",
    "locate_llm_model",
    "create_judge",
    "select_judge",
    "ollama_model_name_for",
    "parse_verdict",
    "DialogueEvent",
    "VERDICT_GOOD",
    "VERDICT_ACCEPTABLE",
    "VERDICT_POOR",
    "VERDICT_UNPARSEABLE",
]

VERDICT_GOOD = "good_cut"          # 适合作为集尾
VERDICT_ACCEPTABLE = "acceptable"  # 可用，需留意
VERDICT_POOR = "poor_cut"          # 会截断对白/动作
VERDICT_UNPARSEABLE = "unparseable"  # 模型输出无法解析——如实记录，不猜测

VALID_VERDICTS = {VERDICT_GOOD, VERDICT_ACCEPTABLE, VERDICT_POOR}

# 语义判定对排序分的调整量。§11.3：这是等级到排序分的**映射**，
# 不是把模型输出包装成概率。
VERDICT_SCORE_ADJUSTMENT = {
    VERDICT_GOOD: 0.20,
    VERDICT_ACCEPTABLE: 0.0,
    VERDICT_POOR: -0.30,
    VERDICT_UNPARSEABLE: 0.0,
}

PROMPT_VERSION = "cut-judge-v1"


# ---------------------------------------------------------------------------
# 事件索引（§11 事件索引）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialogueEvent:
    """一次对白事件。说话人一律未确认（§11.1），除非后续有强证据。"""

    index: int
    start: Fraction
    end: Fraction
    text: str
    speaker: str = "未确认"


@dataclass
class EventIndex:
    """把对白与镜头事件按时间索引，供判断请求组装与整集复核使用。"""

    dialogue: list[DialogueEvent] = field(default_factory=list)
    shots: list[SceneBoundary] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        transcript: Transcript | None,
        shots: Sequence[SceneBoundary] | None = None,
    ) -> "EventIndex":
        dialogue = [
            DialogueEvent(
                index=sentence.index,
                start=sentence.start,
                end=sentence.end,
                text=sentence.text,
            )
            for sentence in (transcript.sentences if transcript else [])
        ]
        return cls(dialogue=dialogue, shots=list(shots or []))

    # ---- 查询 ----------------------------------------------------------

    def sentence_before(self, time: Fraction) -> DialogueEvent | None:
        """结束于该时刻之前（或恰在）的最近一句。"""
        result = None
        for event in self.dialogue:
            if event.end <= time:
                result = event
        return result

    def sentence_after(self, time: Fraction) -> DialogueEvent | None:
        """开始于该时刻之后的最早一句。"""
        for event in self.dialogue:
            if event.start >= time:
                return event
        return None

    def sentence_covering(self, time: Fraction) -> DialogueEvent | None:
        """正被该时刻截断的句子——切在句中是最严重的风险。"""
        for event in self.dialogue:
            if event.start < time < event.end:
                return event
        return None

    def nearest_shot(self, time: Fraction) -> SceneBoundary | None:
        if not self.shots:
            return None
        return min(self.shots, key=lambda s: abs(s.time - time))

    def is_empty(self) -> bool:
        return not self.dialogue and not self.shots

    def describe(self) -> str:
        return (
            f"事件索引：{len(self.dialogue)} 条对白、{len(self.shots)} 次镜头切换"
            + ("（空）" if self.is_empty() else "")
        )


# ---------------------------------------------------------------------------
# 请求与响应（§ 请求校验）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgmentRequest:
    """一次切点判断的完整输入。字段不全会在这里被拦下，而不是污染模型输出。"""

    candidate_time: Fraction
    candidate_frame: int
    text_before: str
    text_after: str
    gap_after: Fraction | None
    ends_with_question: bool
    near_shot_cut: bool
    rule_score: float
    strategy: str

    REQUIRED_TEXT_KEYS = ("candidate_time", "candidate_frame", "strategy")

    def to_prompt_dict(self) -> dict:
        """给模型看的精简上下文。时间取整到毫秒，避免模型读一长串小数。"""
        return {
            "cut_time": f"{float(self.candidate_time):.3f}",
            "text_before": self.text_before or "（无）",
            "text_after": self.text_after or "（无）",
            "gap_after_seconds": (
                f"{float(self.gap_after):.2f}" if self.gap_after is not None else None
            ),
            "ends_with_question": self.ends_with_question,
            "near_shot_cut": self.near_shot_cut,
            "rule_score": round(self.rule_score, 2),
            "strategy": self.strategy,
        }

    def validate(self) -> list[str]:
        """请求校验。返回问题列表；非空即拒绝发送（§ 请求校验）。"""
        problems: list[str] = []
        if self.candidate_time < 0:
            problems.append("candidate_time 为负")
        if self.candidate_frame < 0:
            problems.append("candidate_frame 为负")
        if not 0.0 <= self.rule_score <= 1.0:
            problems.append(f"rule_score 越界：{self.rule_score}")
        if not self.strategy:
            problems.append("缺少 strategy")
        if self.text_before == "" and self.text_after == "":
            problems.append("前后台词均为空，无法做语义判断")
        return problems


@dataclass(frozen=True)
class JudgmentVerdict:
    """一次判断的结果。

    §11.3：没有概率字段。等级 + 证据 + 风险 + 可追溯的判定者标识。
    """

    verdict: str
    evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    judge_id: str = "none"
    prompt_version: str = PROMPT_VERSION
    raw_output: str = ""

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "evidence": list(self.evidence),
            "risks": list(self.risks),
            "judge_id": self.judge_id,
            "prompt_version": self.prompt_version,
        }


# ---------------------------------------------------------------------------
# 预算（§10.4 BUDGET_EXHAUSTED）
# ---------------------------------------------------------------------------


class BudgetExhaustedError(RuntimeError):
    """预算用尽。调用方应停止发起判断并记录限制。"""


@dataclass
class Budget:
    """判断次数预算。

    存在的意义：语义判断是整条链路里**唯一昂贵**的步骤。没有预算，
    候选点多的时候会无节制地推理下去。
    """

    max_requests: int
    spent: int = 0

    def remaining(self) -> int:
        return max(0, self.max_requests - self.spent)

    def try_consume(self) -> None:
        if self.spent >= self.max_requests:
            raise BudgetExhaustedError(
                f"判断预算已用尽（{self.max_requests} 次）"
            )
        self.spent += 1

    def describe(self) -> str:
        return f"预算 {self.spent}/{self.max_requests}"


class ModelUnavailableError(RuntimeError):
    """模型或推理栈不可用。调用方应降级而不是失败。"""


# ---------------------------------------------------------------------------
# 判定器
# ---------------------------------------------------------------------------


class SemanticJudge:
    """判定器协议：输入请求，输出结论。"""

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:  # pragma: no cover
        raise NotImplementedError

    def available(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover
        raise NotImplementedError


class NullJudge(SemanticJudge):
    """降级判定器：明确表示"没有语义判断"。

    注意它**不会**给出结论——降级的表现是"没有判断"，而不是"判断为可用"。
    """

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:
        raise ModelUnavailableError("语义判断未启用（降级模式）")

    def available(self) -> bool:
        return False

    def describe(self) -> str:
        return "未启用（降级：仅使用规则评分）"


_PROMPT_TEMPLATE = """你是一名短剧剪辑助理。下面是一个候选切点的上下文。
请判断"在这个时间点切分两集"是否合适。

判断标准：
- good_cut：前面的台词已经说完（句号/感叹号收尾），后面是新的内容或明确停顿；
- acceptable：可以切，但有需要人工留意的地方；
- poor_cut：会截断台词、或明显处于一个连续动作/对白的中间。

只输出一行 JSON，格式：
{"verdict": "good_cut|acceptable|poor_cut", "evidence": ["..."], "risks": ["..."]}

上下文：
{context}
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _import_llama():
    """导入 llama_cpp.Llama；不可用时返回 None（允许测试注入替身）。"""
    try:
        from llama_cpp import Llama  # type: ignore
    except ImportError:
        return None
    return Llama


class LocalLlmJudge(SemanticJudge):
    """本地 LLM 判定器（GGUF + llama.cpp）。

    模型与本工具的其他模型一样放在 `models/llm/` 下的普通目录，
    由 `tools/fetch_llm.py` 下载（本机必须绕开 huggingface_hub 的符号链接机制）。
    """

    def __init__(self, model_path: str | Path, *, n_ctx: int = 2048,
                 n_threads: int | None = None, max_tokens: int = 160) -> None:
        llama_cls = _import_llama()
        if llama_cls is None:
            raise ModelUnavailableError(
                "未安装 llama-cpp-python，无法使用本地语义判断"
            )
        model_path = Path(model_path)
        if not model_path.exists():
            raise ModelUnavailableError(f"模型文件不存在：{model_path}")

        self.model_path = model_path
        self.judge_id = f"local:{model_path.name}"
        self._max_tokens = max_tokens
        self._llm = llama_cls(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads or 0,
            verbose=False,
        )

    @classmethod
    def from_model_dir(cls, directory: str | Path, **kwargs) -> "LocalLlmJudge":
        directory = Path(directory)
        models = sorted(directory.glob("*.gguf"), key=lambda p: -p.stat().st_size)
        if not models:
            raise ModelUnavailableError(f"{directory} 下没有 .gguf 模型")
        return cls(models[0], **kwargs)

    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return f"本地模型 {self.model_path.name}（提示词 {PROMPT_VERSION}）"

    def probe(self) -> str:
        """加载自检：跑一条最小请求，验证推理栈真的能出结果。"""
        request = JudgmentRequest(
            candidate_time=Fraction(1),
            candidate_frame=25,
            text_before="你好。",
            text_after="再见。",
            gap_after=Fraction(1),
            ends_with_question=False,
            near_shot_cut=False,
            rule_score=0.5,
            strategy="story",
        )
        verdict = self.judge(request)
        return f"{self.describe()} → {verdict.verdict}"

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:
        problems = request.validate()
        if problems:
            # 请求不合法不是模型的问题，不能拿去猜
            return JudgmentVerdict(
                verdict=VERDICT_UNPARSEABLE,
                risks=[f"请求校验未通过：{'；'.join(problems)}"],
                judge_id=self.judge_id,
            )

        context = json.dumps(request.to_prompt_dict(), ensure_ascii=False)
        prompt = _PROMPT_TEMPLATE.format(context=context)
        output = self._llm.create_completion(
            prompt,
            max_tokens=self._max_tokens,
            temperature=0.1,
            stop=["\n\n"],
        )
        text = (output.get("choices") or [{}])[0].get("text", "") if isinstance(output, dict) else str(output)
        return self._parse(text, request)

    def _parse(self, text: str, request: JudgmentRequest) -> JudgmentVerdict:
        """严格解析模型输出（实现见模块级 parse_verdict）。"""
        return parse_verdict(text, self.judge_id)


def ollama_model_name_for(path: str | Path) -> str:
    """按 GGUF 文件路径推导 Ollama 模型名。

    目录约定与 `tools/fetch_llm.py` 一致：
    `models/llm/Qwen3.5-2B-GGUF/Qwen3.5-2B-Q4_K_M.gguf` → `qwen3.5-2b-judge`。
    单独提成公开函数，是为了让界面里"手动指定使用中的档位"复用同一套命名规则。

    注：`.replace("--", "-")` 兜住"文件名里没有 2b"的分支（size 为空串），
    使 `qwen3.5--judge` 收敛为 `qwen3.5-judge`。
    """
    stem = Path(path).stem
    base = stem.split("-")[0].lower()  # "qwen3.5"
    size = "2b" if "2b" in stem.lower() else ""
    return f"{base}-{size}-judge".replace("--", "-")


class OllamaJudge(SemanticJudge):
    """Ollama 本地判定器（http://127.0.0.1:11434）。

    为什么有它：本机 main venv 是 Python 3.13，llama-cpp-python 没有 cp313 轮子
    且无编译器可从源码构建；Ollama 是唯一装得上的本地推理栈。它只监听
    127.0.0.1，不产生任何外发流量——「素材不出机器」仍然成立。

    请求走 **localhost 直连，显式绕过系统代理**：环境里的 http_proxy 会把
    127.0.0.1 的请求也发给代理，代理会拒绝回环地址（实测表现为连接被断开）。
    """

    DEFAULT_HOST = "http://127.0.0.1:11434"

    def __init__(self, model: str, *, host: str = DEFAULT_HOST,
                 timeout_seconds: int = 120) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout_seconds
        self.judge_id = f"ollama:{model}"

    @classmethod
    def auto_model_name(cls) -> str | None:
        """按已下载的 GGUF 推导 Ollama 模型名（与 fetch_llm.py 的目录约定一致）。"""
        path = locate_llm_model()
        return ollama_model_name_for(path) if path is not None else None

    def available(self) -> bool:
        import urllib.request

        try:
            # 绕过代理直连 localhost
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener.open(f"{self.host}/api/tags", timeout=3).read()
            return True
        except Exception:  # noqa: BLE001 - 服务未启动/端口不可达都算不可用
            return False

    def describe(self) -> str:
        return f"Ollama 本地模型 {self.model}（提示词 {PROMPT_VERSION}）"

    def probe(self) -> str:
        request = JudgmentRequest(
            candidate_time=Fraction(1),
            candidate_frame=25,
            text_before="你好。",
            text_after="再见。",
            gap_after=Fraction(1),
            ends_with_question=False,
            near_shot_cut=False,
            rule_score=0.5,
            strategy="story",
        )
        verdict = self.judge(request)
        return f"{self.describe()} → {verdict.verdict}"

    def _generate(self, prompt: str) -> str:
        import urllib.request

        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": self._max_tokens},
            }
        ).encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data.get("response", ""))

    _max_tokens: int = 160

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:
        prompt, invalid = prepare_prompt(request, self.judge_id)
        if invalid is not None:
            return invalid
        try:
            text = self._generate(prompt)
        except Exception as exc:  # noqa: BLE001 - 服务异常按不可用处理
            raise ModelUnavailableError(f"Ollama 调用失败：{exc}") from exc
        return self._parse(text, request)

    def _parse(self, text: str, request: JudgmentRequest) -> JudgmentVerdict:
        return parse_verdict(text, self.judge_id)


def prepare_prompt(
    request: JudgmentRequest, judge_id: str
) -> tuple[str, JudgmentVerdict | None]:
    """请求校验 + 提示词渲染（本地与云端判定器共用同一套，避免两处漂移）。

    返回 ``(prompt, None)`` 或 ``("", 校验失败的结论)``——校验失败不调模型，
    直接给出 unparseable 结论，这与 §11.1"请求字段不全就不该猜"一致。
    """
    problems = request.validate()
    if problems:
        return "", JudgmentVerdict(
            verdict=VERDICT_UNPARSEABLE,
            risks=[f"请求校验未通过：{'；'.join(problems)}"],
            judge_id=judge_id,
        )
    context = json.dumps(request.to_prompt_dict(), ensure_ascii=False)
    # 用 replace 而不是 str.format：模板里那段 JSON 示例的花括号会被 format 当成
    # 替换字段（实测 KeyError: '"verdict"'）。原先本地两条判定路径用的正是
    # format，只要真被调用就会崩——只因为推理栈/模型没装、available() 恒为 False
    # 才一直没暴露。云端接入会让这条路径真正被走到，因此在这里修正。
    return _PROMPT_TEMPLATE.replace("{context}", context), None


class CloudLlmJudge(SemanticJudge):
    """云端 LLM 判定器：厂商适配器 + 共用请求体。

    与 :class:`OllamaJudge` 的差别只在"谁来算"——提示词、严格解析、预算与降级
    语义全部共用（见 :func:`prepare_prompt` / :func:`parse_verdict`）。
    外发范围只有**候选点前后的台词文本片段**，不含画面。

    探测（``available()``）用一次最小请求，并把失败原因留在 ``probe_error`` 里，
    供上层日志展示——"不可用"必须带上为什么。
    """

    def __init__(self, transport: JudgeTransport, *, profile_id: str, model: str) -> None:
        self._transport = transport
        self.judge_id = f"cloud:{profile_id}/{model}"
        self.probe_error = ""

    def available(self) -> bool:
        try:
            self._transport.probe()
        except ProviderError as exc:
            self.probe_error = exc.describe()
            return False
        except Exception as exc:  # noqa: BLE001 - 探测失败一律视为不可用
            self.probe_error = str(exc)
            return False
        self.probe_error = ""
        return True

    def describe(self) -> str:
        return f"云端模型 {self._transport.describe()}（提示词 {PROMPT_VERSION}）"

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:
        prompt, invalid = prepare_prompt(request, self.judge_id)
        if invalid is not None:
            return invalid
        try:
            text = self._transport.complete(prompt)
        except ProviderError as exc:
            raise ModelUnavailableError(f"云端语义判断失败：{exc.describe()}") from exc
        return parse_verdict(text, self.judge_id)


class ResilientJudge(SemanticJudge):
    """云端判定器 + 一次性降级切换。

    为什么需要它：``judge_candidates`` 不捕获异常（它假定判定器是可用的），
    而设计 §7 要求"云端失败**不**把分析判为失败"。云端在半途失败是现实
    （限流、额度耗尽、断网），因此在判定器这一层把切换做掉，并把原因写进
    ``notes``——上层只要把 notes 原样打进日志即可。

    首个失败之后的请求全部交给备选判定器（不做"再次尝试云端"），
    避免每点一次重试都撞一次墙。
    """

    def __init__(
        self,
        primary: SemanticJudge,
        fallback_factory,
        notes: list[str],
        *,
        switched_note: str = "云端语义判断不可用",
    ) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback: SemanticJudge | None = None
        self._notes = notes
        self._switched_note = switched_note
        self._switched = False
        self.judge_id = getattr(primary, "judge_id", "cloud")

    # ---- 内部 ----------------------------------------------------------

    def _fallback_judge(self) -> SemanticJudge | None:
        if self._fallback is None:
            self._fallback = self._fallback_factory()
        return self._fallback

    def _switch(self, reason: str) -> None:
        """切入备选路径并记一条原因（只记一次）。"""
        if self._switched:
            return
        self._switched = True
        fallback = self._fallback_judge()
        target = fallback.describe() if fallback is not None else "降级为规则评分"
        self._notes.append(f"{self._switched_note}：{reason}；已切换 → {target}")

    # ---- 协议实现 ------------------------------------------------------

    def available(self) -> bool:
        if not self._switched:
            if self._primary.available():
                return True
            reason = getattr(self._primary, "probe_error", "") or "探测失败"
            self._switch(reason)
        fallback = self._fallback_judge()
        return bool(fallback is not None and fallback.available())

    def describe(self) -> str:
        if self._switched:
            return f"{self._primary.describe()}（已回退）"
        return self._primary.describe()

    def judge(self, request: JudgmentRequest) -> JudgmentVerdict:
        if not self._switched:
            try:
                return self._primary.judge(request)
            except ModelUnavailableError as exc:
                self._switch(str(exc))
        fallback = self._fallback_judge()
        if fallback is None:
            # 仅云端模式：不回退，本轮如实标记为无法判定（候选保持 pending）
            return JudgmentVerdict(
                verdict=VERDICT_UNPARSEABLE,
                risks=[note for note in self._notes if note.startswith(self._switched_note)],
                judge_id=self.judge_id,
            )
        return fallback.judge(request)


def parse_verdict(text: str, judge_id: str) -> JudgmentVerdict:
    """严格解析模型输出。解析失败如实标记，绝不猜测或兜底成某个等级。"""
    match = _JSON_RE.search(text or "")
    if not match:
        return JudgmentVerdict(
            verdict=VERDICT_UNPARSEABLE,
            risks=["模型输出中没有 JSON"],
            judge_id=judge_id,
            raw_output=(text or "")[:400],
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return JudgmentVerdict(
            verdict=VERDICT_UNPARSEABLE,
            risks=[f"JSON 解析失败：{exc}"],
            judge_id=judge_id,
            raw_output=(text or "")[:400],
        )
    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        return JudgmentVerdict(
            verdict=VERDICT_UNPARSEABLE,
            risks=[f"verdict 非法：{verdict!r}"],
            judge_id=judge_id,
            raw_output=(text or "")[:400],
        )
    evidence = [str(item) for item in data.get("evidence", []) if str(item).strip()]
    risks = [str(item) for item in data.get("risks", []) if str(item).strip()]
    if not evidence:
        evidence = ["（模型未给出证据）"]
        risks.append("模型输出缺少证据，结论可信度低")
    return JudgmentVerdict(
        verdict=verdict,
        evidence=evidence,
        risks=risks,
        judge_id=judge_id,
        raw_output=(text or "")[:400],
    )


# ---------------------------------------------------------------------------
# 组装请求与执行判断
# ---------------------------------------------------------------------------


@dataclass
class JudgeOutcome:
    judged: int = 0
    skipped_by_budget: int = 0
    unparseable: int = 0
    degraded: bool = False
    judge_description: str = ""
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.degraded:
            return "语义判断：未启用（降级，仅规则评分）"
        return (
            f"语义判断：{self.judged} 个候选（{self.judge_description}），"
            f"无法解析 {self.unparseable} 个"
            + (f"，因预算跳过 {self.skipped_by_budget} 个" if self.skipped_by_budget else "")
        )


def local_judge(
    *,
    ollama_model: str | None = None,
    model_dir: str | Path | None = None,
) -> SemanticJudge | None:
    """本地判定器链：进程内 llama.cpp（延迟最低）→ Ollama → None。

    返回 None 不是错误——降级是阶段4 的明确路径（§ 预算与降级）。
    """
    if locate_llm_model(model_dir) is not None:
        try:
            return LocalLlmJudge.from_model_dir(
                model_dir or (Path(__file__).resolve().parent.parent.parent / "models" / "llm")
            )
        except ModelUnavailableError:
            pass

    name = ollama_model or OllamaJudge.auto_model_name()
    if name:
        judge = OllamaJudge(name)
        if judge.available():
            return judge
    return None


def create_judge(
    *,
    ollama_model: str | None = None,
    model_dir: str | Path | None = None,
) -> SemanticJudge | None:
    """按可用性选择**本地**判定器；都不可用时返回 None（调用方降级）。

    保留这个入口是给命令行自检（``run.py --self-check``）等"没有 AppConfig"的
    场景用的；界面链路请用 :func:`select_judge`，它还会读云端档案。
    """
    return local_judge(ollama_model=ollama_model, model_dir=model_dir)


def cloud_block_reason(profile: ProviderProfile) -> str | None:
    """云端**语义判断**为什么不能生效；可用时返回 None。

    判定本身放在 providers.block_reason（转写与语义判断共用一套措辞），
    这里保留一个领域入口，调用方不必关心能力枚举。
    """
    return block_reason(profile, CAP_JUDGE)


def select_judge(
    config: object | None,
    *,
    ollama_model: str | None = None,
    model_dir: str | Path | None = None,
) -> tuple[SemanticJudge | None, list[str]]:
    """按用户配置选判定器，并把"为什么这样选"变成可展示的说明。

    返回 ``(judge, notes)``：judge 为 None 表示降级（候选保持规则评分）；
    notes 必须原样进日志与检查器——"选了云端却没生效"的原因要能被用户看到，
    只写在代码里等于没说。
    """
    notes: list[str] = []
    profile = (
        config.active_profile(CAP_JUDGE) if config is not None else None  # type: ignore[attr-defined]
    )
    if profile is not None:
        reason = cloud_block_reason(profile)
        if reason is not None:
            notes.append(reason + "，本次仍用本地模型。")
        else:
            transport = build_judge_transport(profile)
            if transport is None:
                protocol = PROTO_LABELS.get(profile.judge_protocol, profile.judge_protocol)
                notes.append(
                    f"暂不支持「{protocol}」协议的云端语义判断：{profile.id}"
                    "（可在「厂商管理」里改协议），本次仍用本地模型。"
                )
            else:
                primary = CloudLlmJudge(
                    transport, profile_id=profile.id, model=profile.judge_model
                )
                notes.append(
                    f"语义判断后端：{primary.describe()}（"
                    + describe_egress(profile, (CAP_JUDGE,))
                    + "）"
                )
                fallback_factory = (
                    (lambda: local_judge(ollama_model=ollama_model, model_dir=model_dir))
                    if config.fallback_to_local  # type: ignore[attr-defined]
                    else (lambda: None)
                )
                return ResilientJudge(primary, fallback_factory, notes), notes

    return local_judge(ollama_model=ollama_model, model_dir=model_dir), notes


def build_request(
    point: CandidatePoint,
    index: EventIndex,
    *,
    strategy: str,
) -> JudgmentRequest | None:
    """从候选点与事件索引组装判断请求。

    前后台词都为空时返回 None——没有文本证据的判断是在猜（§11.1）。
    """
    before_event = index.sentence_before(point.time)
    after_event = index.sentence_after(point.time)
    text_before = (before_event.text if before_event else point.speech_before or "").strip()
    text_after = (after_event.text if after_event else point.speech_after or "").strip()
    if not text_before and not text_after:
        return None

    ends_with_question = bool(
        text_before and text_before[-1] in "？！??"
    )
    near_shot = False
    nearest = index.nearest_shot(point.time)
    if nearest is not None and abs(nearest.time - point.time) <= Fraction(1):
        near_shot = True

    gap = point.gap_after
    if gap is None and after_event is not None and before_event is not None:
        gap = max(Fraction(0), after_event.start - before_event.end)

    return JudgmentRequest(
        candidate_time=point.time,
        candidate_frame=point.frame_index,
        text_before=text_before,
        text_after=text_after,
        gap_after=gap,
        ends_with_question=ends_with_question,
        near_shot_cut=near_shot,
        rule_score=point.score,
        strategy=strategy,
    )


def judge_candidates(
    candidates: CandidateSet,
    index: EventIndex,
    judge: SemanticJudge | None,
    *,
    strategy: str,
    budget_requests: int = 24,
    top_k: int | None = None,
) -> JudgeOutcome:
    """对候选点执行语义判断，并**就地**更新候选的评分与审核状态。

    - 只判断规则评分最高的前 K 个（§8.4 先廉价新筛选再昂贵分析）；
    - 预算用尽后停止，并记录跳过了多少（§10.4 BUDGET_EXHAUSTED）；
    - 模型不可用时整体降级，**不给出任何结论**；
    - 判定为 poor_cut 的候选会被扣分，好切点加分——调整量是等级到排序分的
      映射（§11.3），不是概率。
    """
    outcome = JudgeOutcome()

    if judge is None or not judge.available():
        outcome.degraded = True
        outcome.notes.append("语义判断未启用：候选保持规则评分，审核状态保持 pending。")
        return outcome

    outcome.judge_description = judge.describe()
    budget = Budget(max_requests=budget_requests)

    ordered = sorted(candidates.points, key=lambda p: -p.score)
    if top_k is not None:
        ordered = ordered[:top_k]

    judged_scores: dict[int, float] = {}
    for point in ordered:
        if budget.remaining() <= 0:
            outcome.skipped_by_budget += 1
            continue
        request = build_request(point, index, strategy=strategy)
        if request is None:
            outcome.notes.append(
                f"候选 {format_short(point.time)} 无前后台词，跳过语义判断"
            )
            continue
        try:
            budget.try_consume()
        except BudgetExhaustedError:
            outcome.skipped_by_budget += 1
            continue
        verdict = judge.judge(request)
        if verdict.verdict == VERDICT_UNPARSEABLE:
            outcome.unparseable += 1

        point.risks = list(dict.fromkeys([*point.risks, *verdict.risks]))
        evidence = [f"语义判断（{verdict.verdict}）：{item}" for item in verdict.evidence]
        point.evidence = list(dict.fromkeys([*point.evidence, *evidence]))
        point.review_status = "semantic_reviewed"
        judged_scores[id(point)] = VERDICT_SCORE_ADJUSTMENT[verdict.verdict]
        outcome.judged += 1

    if outcome.skipped_by_budget:
        outcome.notes.append(
            f"判断预算 {budget.max_requests} 次已用尽，{outcome.skipped_by_budget} 个候选"
            "未做语义判断（按规则评分参与排序）——结果不声称覆盖全部候选"
        )
    if outcome.unparseable:
        outcome.notes.append(
            f"{outcome.unparseable} 个判断的输出无法解析，已按「未解析」记录而不是猜测等级"
        )

    # 把语义调整叠加到排序分上（限定 0..1），并按新分数重排
    for point in candidates.points:
        adjustment = judged_scores.get(id(point))
        if adjustment is not None:
            point.score = max(0.0, min(1.0, point.score + adjustment))
    candidates.points.sort(key=lambda p: (-p.score, p.time))
    return outcome


def format_short(time: Fraction) -> str:
    return f"{float(time):.3f}s"


def locate_llm_model(root: str | Path | None = None) -> Path | None:
    """定位已下载的 GGUF 模型。

    显式传入 `root` 只查该目录；否则遍历 `model_search_roots()` 的所有候选——
    打包后 `__file__` 指向包内目录，只有候选搜索才能找到用户放在 exe 旁边的模型。
    """
    from .asr import model_search_roots

    directories = [Path(root) / "llm"] if root else [
        candidate / "llm" for candidate in model_search_roots()
    ]
    for directory in directories:
        if not directory.exists():
            continue
        models = sorted(directory.rglob("*.gguf"), key=lambda p: -p.stat().st_size)
        if models:
            return models[0]
    return None
