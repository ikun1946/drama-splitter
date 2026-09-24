"""ASR 评测：字符错误率与时间戳误差。

为什么需要它
------------
"转出来一段文字"无法评判转写质量。本脚本拿 `tools/make_speech_asset.py`
产出的**标准答案**（已知文本 + 精确句边界）做对照，量化两个指标：

1. **CER（字符错误率）**：编辑距离 / 标准答案字符数。中文按字符算，标点先归一化。
2. **边界误差**：ASR 句段边界与真实句边界的偏差。对分集而言这一项比 CER 更关键——
   候选切点建立在"对白句末"上，边界偏 300ms 就可能把切点放到句子中间。

用途：
- 选定默认模型档位（拿数据选，不靠猜）
- 回归：换模型 / 换参数 / 换提示词后确认没有变差
- 对照基线（§20.3 要求比较"规则基线"与"AI 增强"，这里先建立 ASR 侧的基线）

用法：
    python tools/eval_asr.py small
    python tools/eval_asr.py tiny --no-initial-prompt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 归一化时要剥掉的标点（全角与半角）
_PUNCT = "，。！？、；：（）《》「」『』【】…—～·,.!?;:()<>\"'` \t\n\r　"

SIMPLIFIED_PROMPT = "以下是一段简体中文的短剧对白。"


@dataclass
class EvalResult:
    model: str
    prompt: str | None
    audio_seconds: float
    elapsed: float
    cer: float
    substitutions: int
    deletions: int
    insertions: int
    reference_chars: int
    segment_count: int
    boundary_errors_ms: list[float] = field(default_factory=list)
    hypothesis: str = ""

    @property
    def rtf(self) -> float:
        return self.elapsed / self.audio_seconds if self.audio_seconds else 0.0

    @property
    def max_boundary_error_ms(self) -> float:
        return max(self.boundary_errors_ms) if self.boundary_errors_ms else 0.0

    @property
    def mean_boundary_error_ms(self) -> float:
        if not self.boundary_errors_ms:
            return 0.0
        return sum(self.boundary_errors_ms) / len(self.boundary_errors_ms)


def normalize(text: str) -> str:
    """归一化：剥标点与空白。只比较"说了什么字"，不比较标点。"""
    return "".join(ch for ch in text if ch not in _PUNCT)


def edit_operations(reference: str, hypothesis: str) -> tuple[int, int, int]:
    """Levenshtein 编辑距离的三种操作计数（替换 / 删除 / 插入）。

    需要区分操作类型才能定位失败模式：删除多说明漏字（VAD 把语音切掉了），
    替换多说明识别错误（模型能力问题），插入多说明重复或幻觉。

    全矩阵 + 回溯，因此对超长文本会退化为只算总距离（见 MAX_MATRIX_CELLS）。
    评测对象是几十秒的测试素材，正常都走全矩阵。
    """
    n, m = len(reference), len(hypothesis)
    if n == 0:
        return 0, 0, m
    if m == 0:
        return 0, n, 0

    MAX_MATRIX_CELLS = 4_000_000
    if n * m > MAX_MATRIX_CELLS:
        # 退化路径：滚动数组算总距离，不区分操作类型（记为替换）
        previous = list(range(m + 1))
        for i in range(1, n + 1):
            current = [i] + [0] * m
            for j in range(1, m + 1):
                cost = 0 if reference[i - 1] == hypothesis[j - 1] else 1
                current[j] = min(
                    previous[j] + 1,        # 删除
                    current[j - 1] + 1,     # 插入
                    previous[j - 1] + cost,  # 替换或匹配
                )
            previous = current
        return previous[m], 0, 0

    # dp[i][j] = (总代价, 替换数, 删除数, 插入数)
    dp: list[list[tuple[int, int, int, int]]] = [
        [(0, 0, 0, 0)] * (m + 1) for _ in range(n + 1)
    ]
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
                continue
            replace = dp[i - 1][j - 1]
            delete = dp[i - 1][j]
            insert = dp[i][j - 1]
            best = min(
                (
                    (replace[0] + 1, replace[1] + 1, replace[2], replace[3]),
                    (delete[0] + 1, delete[1], delete[2] + 1, delete[3]),
                    (insert[0] + 1, insert[1], insert[2], insert[3] + 1),
                ),
                key=lambda item: (item[0], item[1] + item[2] + item[3]),
            )
            dp[i][j] = best

    _, sub, dele, ins = dp[n][m]
    return sub, dele, ins


def cer(reference: str, hypothesis: str) -> tuple[float, int, int, int]:
    """字符错误率 = (替换 + 删除 + 插入) / 参考字符数。"""
    ref, hyp = normalize(reference), normalize(hypothesis)
    sub, dele, ins = edit_operations(ref, hyp)
    total = sub + dele + ins
    return (total / len(ref) if ref else 0.0), sub, dele, ins


def evaluate(
    model_path: str | Path,
    audio: str | Path,
    ground_truth: dict,
    *,
    initial_prompt: str | None = SIMPLIFIED_PROMPT,
    compute_type: str = "int8",
) -> EvalResult:
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_path), device="cpu", compute_type=compute_type)
    started = time.time()
    segments, _info = model.transcribe(
        str(audio),
        language="zh",
        beam_size=5,
        initial_prompt=initial_prompt,
        vad_filter=True,
        word_timestamps=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )
    segments = list(segments)
    elapsed = time.time() - started

    hypothesis = "".join(seg.text for seg in segments)
    reference = "".join(line["text"] for line in ground_truth["lines"])
    rate, sub, dele, ins = cer(reference, hypothesis)

    # 边界误差：把 ASR 的句段边界吸附到最近的真实句边界后取偏差。
    # 只评估"每句话的起点"——终点受尾音长度影响，波动本就较大。
    true_starts = [line["start"] for line in ground_truth["lines"]]
    errors: list[float] = []
    for seg in segments:
        nearest = min(true_starts, key=lambda t: abs(t - seg.start))
        error = abs(seg.start - nearest)
        # 超过 1.5 秒的不算"同一句话"，是分段方式差异而非时间戳误差
        if error <= 1.5:
            errors.append(error * 1000)

    return EvalResult(
        model=Path(model_path).name,
        prompt=initial_prompt,
        audio_seconds=float(ground_truth["total_seconds"]),
        elapsed=elapsed,
        cer=rate,
        substitutions=sub,
        deletions=dele,
        insertions=ins,
        reference_chars=len(normalize(reference)),
        segment_count=len(segments),
        boundary_errors_ms=sorted(errors),
        hypothesis=hypothesis,
    )


def report(result: EvalResult, ground_truth: dict) -> None:
    print(f"模型：{result.model}")
    print(f"提示词：{result.prompt or '（无）'}")
    print(f"音频：{result.audio_seconds:.1f}s　耗时 {result.elapsed:.1f}s　RTF {result.rtf:.3f}")
    print()
    print(f"CER：{result.cer * 100:.2f}%　"
          f"（替换 {result.substitutions} / 删除 {result.deletions} / 插入 {result.insertions}，"
          f"参考 {result.reference_chars} 字）")
    print(f"句段数：{result.segment_count}（标准答案 {len(ground_truth['lines'])} 句）")
    print(f"边界误差：均值 {result.mean_boundary_error_ms:.0f}ms　"
          f"最大 {result.max_boundary_error_ms:.0f}ms")
    if result.boundary_errors_ms:
        p90 = result.boundary_errors_ms[int(len(result.boundary_errors_ms) * 0.9)]
        print(f"　　　　　P90 {p90:.0f}ms")
    print()
    print("标准答案：")
    print("  " + "".join(line["text"] for line in ground_truth["lines"]))
    print("转写结果：")
    print("  " + result.hypothesis.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="ASR 评测：CER 与时间戳误差")
    parser.add_argument("model", help="模型目录名（models/ 下）或档位名，如 small")
    parser.add_argument("--audio", default=str(ROOT / "testdata" / "speech_16k.wav"))
    parser.add_argument("--gt", default=str(ROOT / "testdata" / "speech_ground_truth.json"))
    parser.add_argument("--no-initial-prompt", action="store_true")
    parser.add_argument("--json", help="把结果写到该 JSON 文件")
    args = parser.parse_args()

    model_path = ROOT / "models" / args.model
    if not model_path.exists():
        model_path = ROOT / "models" / f"faster-whisper-{args.model}"
    if not model_path.exists():
        print(f"找不到模型：{args.model}")
        print("先下载：python tools/fetch_whisper_model.py small")
        return 2

    audio = Path(args.audio)
    if not audio.exists():
        print(f"找不到音频：{audio}")
        print("先生成：python tools/make_speech_asset.py testdata")
        return 2

    ground_truth = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    prompt = None if args.no_initial_prompt else SIMPLIFIED_PROMPT

    result = evaluate(model_path, audio, ground_truth, initial_prompt=prompt)
    report(result, ground_truth)

    if args.json:
        payload = {
            "model": result.model,
            "prompt": result.prompt,
            "cer": result.cer,
            "substitutions": result.substitutions,
            "deletions": result.deletions,
            "insertions": result.insertions,
            "reference_chars": result.reference_chars,
            "segment_count": result.segment_count,
            "rtf": result.rtf,
            "mean_boundary_error_ms": result.mean_boundary_error_ms,
            "max_boundary_error_ms": result.max_boundary_error_ms,
            "hypothesis": result.hypothesis,
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print()
        print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
