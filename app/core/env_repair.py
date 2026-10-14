"""「一键修复环境」：把自检报告里**能自动修**的项排成计划并逐步执行。

与 ``env_check`` 的分工
------------------------
- ``env_check`` 只读：给出报告，并如实标注每一项是「应用内可修」还是
  「只能引导」；
- 本模块负责动手：:func:`repair_steps` 把报告中的可修项排成一个有序计划，
  :func:`execute_repair` 逐步执行——装 / 重装转写引擎、下载缺失的模型。

计划只收两类动作
----------------
``env_check.AUTO_FIX_KINDS``（安装转写引擎、下载模型）。FFmpeg、可选 Python
包、llama-cpp 轮子这些「应用内装不了」的项不出现在计划里——界面继续用单项
按钮给指引。「一键修复」承诺的是「把能自动修的修掉」，不是「什么都能修」。

执行顺序
--------
1. 转写引擎（faster-whisper 依赖）→ 2. 转写模型（whisper-*）→ 3. 语义模型
（llm:*）。报告本身按此顺序产出，这里再显式排一次序，防止调用方乱序注入。

失败与取消
----------
每步的结果（成功 / 失败原因）都收进 :class:`RepairSummary`，由界面如实汇报，
绝不静默吞掉；一步失败不阻塞后续独立步骤；取消则立即停住剩余步骤——底层
下载 / 安装都接受 ``cancel_check``，已下载的部分会保留，重试时跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .env_check import FIX_DOWNLOAD, FIX_INSTALL_ENGINE, EnvReport

__all__ = [
    "RepairStep",
    "RepairStepResult",
    "RepairSummary",
    "repair_steps",
    "execute_repair",
]


@dataclass(frozen=True)
class RepairStep:
    """计划里的一步：做什么 + 为什么（给用户看的文案）。"""

    kind: str        # FIX_INSTALL_ENGINE / FIX_DOWNLOAD
    payload: str     # install_engine：空；download：模型 key（spec_by_key 可解析）
    label: str       # 动作名，如「下载 whisper-small」
    item_key: str    # 来源自检项 key（asr_engine / whisper_model / llm_model）
    requires_engine: bool = False
    #: 本步依赖转写引擎装好。引擎缺失时计划会预排转写模型下载（执行时先装引擎）；
    #: 引擎失败 / 没修好则本步跳过——不白下几个 GB。


@dataclass(frozen=True)
class RepairStepResult:
    """一步的执行结果。"""

    label: str
    ok: bool
    cancelled: bool = False
    skipped: bool = False    # 因前置依赖未就绪而跳过（不算失败）
    message: str = ""    # 可读结果：成功描述或失败原因


@dataclass(frozen=True)
class RepairSummary:
    """一次「一键修复」的汇总。"""

    results: tuple[RepairStepResult, ...] = ()
    cancelled: bool = False
    total_steps: int = 0    # 原计划总步数（含因取消未执行的）

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_count(self) -> int:
        return sum(
            1 for r in self.results if not r.ok and not r.cancelled and not r.skipped
        )

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and not self.cancelled and all(r.ok for r in self.results)

    def describe(self) -> str:
        """一段可直接展示给用户的汇总文案。"""
        if self.cancelled:
            return (
                f"修复已取消：完成 {self.ok_count}/{self.total_steps} 项，"
                "已下载的部分保留，下次点「一键修复」会接着装。"
            )
        if not self.results:
            return "没有需要修复的项。"
        if self.all_ok:
            return f"修复完成：{self.ok_count} 项全部成功。"
        lines = [f"修复结束：{self.ok_count} 项成功、{self.failed_count} 项失败。"]
        if self.skipped_count:
            lines.append(f"（{self.skipped_count} 项因前置未就绪被跳过）")
        lines += [
            f"· {r.label}：{r.message}"
            for r in self.results
            if not r.ok and not r.cancelled
        ]
        return "\n".join(lines)


def _order(step: RepairStep) -> tuple[int, str]:
    """步骤排序键：引擎最前，转写模型其次，其余模型最后。"""
    if step.kind == FIX_INSTALL_ENGINE:
        return (0, step.payload)
    if step.payload.startswith("whisper:"):
        return (1, step.payload)
    return (2, step.payload)


def repair_steps(report: EnvReport) -> tuple[RepairStep, ...]:
    """从自检报告提取「能自动修」的项，按执行顺序排好。

    只收 ``item.auto_fixable`` 的项（装引擎 / 下模型）；打开网页、跳转、
    纯告知这些都不在计划里——它们由界面逐项给指引。
    """
    plan: list[RepairStep] = []
    for item in report.items:
        if not item.auto_fixable or item.fix is None:
            continue
        plan.append(
            RepairStep(
                kind=item.fix.kind,
                payload=item.fix.payload,
                label=item.fix.label,
                item_key=item.key,
            )
        )

    # 引擎缺失时，转写模型那条会被检测规则「压住」（先修引擎，免得白下几个 GB）
    # 而对「一键修复」来说那只是顺序问题：把模型下载预排进计划、标记依赖引擎，
    # 执行时先装引擎，装好才下模型；引擎失败则自动跳过（见 execute_repair）。
    has_engine = any(s.kind == FIX_INSTALL_ENGINE for s in plan)
    has_whisper = any(s.payload.startswith("whisper:") for s in plan)
    if has_engine and not has_whisper and report.by_key("whisper_model") is not None:
        from .asr import locate_model
        from .model_manager import default_whisper_tier

        tier = default_whisper_tier()
        if locate_model(tier) is None:
            plan.append(
                RepairStep(
                    kind=FIX_DOWNLOAD,
                    payload=f"whisper:{tier}",
                    label=f"下载 whisper-{tier}",
                    item_key="whisper_model",
                    requires_engine=True,
                )
            )

    return tuple(sorted(plan, key=_order))


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

OnStep = Callable[[int, int, str], None]
#: (第几步（1 起）, 共几步, 步骤文案)

OnProgress = Callable[[int, int, str, int, int], None]
#: 与 deps_manager / model_manager 的进度回调同构：
#: (已下载字节, 总字节, 当前文件, 第几个, 共几个)

OnLog = Callable[[str], None]


def execute_repair(
    steps: tuple[RepairStep, ...] | list[RepairStep],
    *,
    models_root: str | Path | None = None,
    model_endpoint: str | None = None,
    deps_root: str | Path | None = None,
    on_step: OnStep | None = None,
    on_progress: OnProgress | None = None,
    on_log: OnLog | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> RepairSummary:
    """按计划逐步修复。**永不抛异常**——错误都变成每步的结果。

    同步阻塞调用（大模型会下载很久）——GUI 侧必须放后台线程，
    见 ``app.gui.workers.EnvRepairWorker``。
    """
    from .app_config import default_models_root
    from .deps_manager import install_deps
    from .model_manager import DEFAULT_ENDPOINT, download_model, spec_by_key

    step_list = list(steps)
    total = len(step_list)
    stopping = cancel_check or (lambda: False)
    root = Path(models_root) if models_root is not None else default_models_root()
    endpoint = model_endpoint or DEFAULT_ENDPOINT

    def log(text: str) -> None:
        if on_log is not None:
            on_log(text)

    def relay_progress(done: int, tot: int, name: str, i: int, n: int) -> None:
        if on_progress is not None:
            on_progress(done, tot, name, i, n)

    results: list[RepairStepResult] = []
    cancelled = False
    engine_ok: bool | None = None    # 引擎步骤没跑过 = None
    for index, step in enumerate(step_list, start=1):
        if stopping():
            cancelled = True
            break
        if on_step is not None:
            on_step(index, total, step.label)
        if step.requires_engine and engine_ok is not True:
            message = "已跳过：转写引擎未装好，先修好引擎再下模型。"
            results.append(
                RepairStepResult(
                    label=step.label, ok=False, skipped=True, message=message
                )
            )
            log(f"（{index}/{total}）{step.label}——{message}")
            continue
        log(f"（{index}/{total}）{step.label}…")
        ok = False
        was_cancelled = False
        message = ""
        try:
            result = None
            if step.kind == FIX_INSTALL_ENGINE:
                result = install_deps(
                    deps_root=deps_root,
                    on_progress=relay_progress if on_progress is not None else None,
                    cancel_check=stopping,
                )
            else:
                spec = spec_by_key(step.payload)
                if spec is None:
                    message = f"无法识别的模型：{step.payload}"
                else:
                    result = download_model(
                        spec,
                        root,
                        endpoint=endpoint,
                        on_progress=relay_progress if on_progress is not None else None,
                        cancel_check=stopping,
                    )
            if result is not None:
                ok = bool(result.ok)
                was_cancelled = bool(getattr(result, "cancelled", False))
                message = result.describe()
        except Exception as exc:  # noqa: BLE001 - 单步异常不能让整场修复崩掉
            ok, was_cancelled, message = False, False, f"修复过程异常：{exc}"
        if step.kind == FIX_INSTALL_ENGINE:
            engine_ok = ok
        results.append(
            RepairStepResult(label=step.label, ok=ok, cancelled=was_cancelled, message=message)
        )
        log(message)
        if was_cancelled:
            cancelled = True
            break

    return RepairSummary(results=tuple(results), cancelled=cancelled, total_steps=total)
