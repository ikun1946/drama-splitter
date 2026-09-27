"""中栏「分析」pane：当前阶段、进度、最近一条输出。

分析日志只有一个落点在右栏检查器的「日志」页签（``log_appended`` 广播出去，
由工作台接进去）。原先这里还有一份 LogPanel 与七段步骤条，日志两处显示、
进度也重复——那是"复杂"的主要来源之一。

阶段用「阶段 n/7 · 名称」表达即可，不必再画一条 stepper：中栏同一时刻只有
一件事在发生，进度条已经把"还剩多少"说清楚了。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ...core.plan import BoundaryPlan
from ...core.settings import (
    SettingAdvice,
    applicable_setting_advice,
    apply_setting_advice,
)
from ..state import ProjectState
from ..widgets import message
from ..workers import AnalysisWorker
from ._common import register_worker

__all__ = ["AnalysisPane"]


class AnalysisPane(QWidget):
    STAGES = [
        "字幕解析",
        "语音转写",
        "镜头与黑场",
        "候选点",
        "整体规划",
        "边界审核",
        "导出",
    ]

    started = Signal()
    stage_changed = Signal(str)
    progress_changed = Signal(str, float)
    log_appended = Signal(str, str)      # (文本, 级别)
    finished = Signal()

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: AnalysisWorker | None = None
        # 「已请求开始」标记：start() 里发出 started 时线程还没真正跑起来
        # （worker.start() 在其后一行），只看 isRunning() 会让界面停在上一阶段。
        self._active = False

        self.stage_label = QLabel("等待开始")
        self.stage_label.setProperty("role", "title")
        self.stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")
        self.progress.setMinimumWidth(240)

        self.recent = QLabel("分析会依次完成字幕解析、语音转写、镜头检测、候选点、"
                             "整体规划与整集复核。")
        self.recent.setProperty("role", "muted")
        self.recent.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)
        outer.addStretch(1)
        outer.addWidget(self.stage_label)
        outer.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self.recent)
        outer.addStretch(2)

    # ---- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        if self.state.media is None:
            message.warning(self, "尚未导入", "请先选择源视频。")
            return
        settings = self.state.settings
        problems = settings.validate()
        if problems:
            message.warning(self, "参数不完整", problems[0])
            return
        if self.state.binaries is None:
            message.warning(self, "缺少 FFmpeg", "尚未定位到 FFmpeg。")
            return

        self.progress.setValue(0)
        self.stage_label.setText("准备中…")

        self.worker = AnalysisWorker(
            self.state.binaries,
            self.state.media,
            self.state.audio_stream_index,
            settings,
            self,
        )
        self.worker.stage_started.connect(self._on_stage)
        self.worker.stage_progress.connect(self._on_progress)
        self.worker.stage_log.connect(self._append)
        self.worker.completed.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        register_worker(self, "analysis", self.worker)
        self._active = True
        self.started.emit()
        self.worker.start()

    def is_active(self) -> bool:
        """分析是否正在进行（含"已请求开始、线程尚未起来"这一瞬间）。"""
        if not self._active:
            return False
        return self.worker is not None

    def cancel(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        self._active = False
        self._append("已取消。", "warn")
        self.progress.setValue(0)
        self.stage_label.setText("已取消")
        self.finished.emit()

    # ---- 进度回调 ------------------------------------------------------

    def _on_stage(self, name: str) -> None:
        index = self.STAGES.index(name) if name in self.STAGES else 0
        self.stage_label.setText(f"阶段 {index + 1}/{len(self.STAGES)} · {name}")
        self._append(f"—— {name} ——")
        self.stage_changed.emit(name)

    def _on_progress(self, text: str, ratio: float) -> None:
        self.progress.setValue(int(ratio * 100))
        self.recent.setText(text)
        self.progress_changed.emit(text, ratio)

    def _on_done(
        self, plan: BoundaryPlan, report, problems, candidates, reviews=None
    ) -> None:
        self.state.add_plan(plan)
        self.state.candidates = candidates
        self.state.episode_reviews = reviews
        worker = self.sender()
        if worker is not None and getattr(worker, "transcript", None) is not None:
            self.state.transcript = worker.transcript
            self._append("转写已保留，导出时将一并生成单集 SRT 字幕。")

        self.progress.setValue(100)
        self.stage_label.setText("分析完成")
        self._active = False
        self.recent.setText(f"方案 v{plan.version}：{plan.episode_count} 集，见左侧集列表。")

        self._append(f"候选点：{candidates.describe()}")
        for note in candidates.limitations:
            self._append(f"  限流：{note}")
        if candidates.points:
            self._append("评分最高的候选点：")
            for point in candidates.top(3):
                self._append(f"  {point.describe()}")

        durations = [float(d) for d in plan.durations()]
        self._append(
            f"方案 v{plan.version}：{plan.episode_count} 集，"
            f"时长 {min(durations):.2f}s – {max(durations):.2f}s"
        )
        self._append(
            "覆盖校验：" + ("通过（b0=0、bN=T、严格递增）" if plan.coverage_valid() else "未通过")
        )
        snapping = plan.is_frame_aligned(self.state.media)
        self._append(
            "帧对齐：" + ("全部落在合法帧起点" if snapping[0] else f"有 {len(snapping[1])} 处未对齐")
        )
        if reviews:
            self._append("整集复核（阶段4）：")
            grade_labels = {"pass": "通过", "warn": "留意", "block": "阻断"}
            for review in reviews:
                detail = "；".join(review.risks) if review.risks else "无风险"
                self._append(
                    f"  第{review.episode:02d}集[{grade_labels[review.grade]}] {detail}"
                )
        self._append(
            "语义状态：候选已按规则评分"
            + ("并做整集复核" if reviews else "（未接入剧情级语义判断，属阶段4 模型部分）")
            + "。"
        )
        if problems:
            for problem in problems:
                self._append(f"  方案层问题：{problem.describe()}")
        else:
            self._append("方案层校验：无问题。")

        self.finished.emit()

    def _on_failed(self, text: str) -> None:
        self._append(f"失败：{text}", "error")
        self.progress.setValue(0)
        self.stage_label.setText("分析失败")
        self._active = False
        self.recent.setText(text)

        # 无解报错自带「该把哪个参数改成多少」：能一键应用就不让用户再回
        # 分集设置里自己改（§10.4 的成因 + 建议，按钮与文案同源）
        advice = self._failure_advice()
        if advice and self.request_apply_advice(text, advice):
            note = apply_setting_advice(
                self.state.settings, advice, self.state.total_seconds
            )
            if note is not None:
                self._append(note)
                self._append("已按新设置重新分析。")
                self.finished.emit()    # 先回到"未在分析"，start() 再切到"分析中"
                self.start()
                return
        message.warning(self, "无法完成分析", text)
        self.finished.emit()

    def _failure_advice(self) -> list[SettingAdvice]:
        """刚失败的那次求解留下的、可一键应用的建议；没有时返回空表。"""
        worker = self.sender() or self.worker
        diagnostics = getattr(worker, "failure_diagnostics", None)
        if diagnostics is None or self.state.media is None:
            return []
        return applicable_setting_advice(diagnostics.advice)

    def request_apply_advice(self, text: str, advice: list[SettingAdvice]) -> bool:
        """问用户要不要把建议应用到分集设置。

        与导出的 ``request_precheck`` 同一条约定：离屏测试覆写这个方法，
        不走真弹窗（实例级 ``exec()`` 会让自动化永久阻塞）。
        """
        return message.warning_with_action(
            self,
            "无法完成分析",
            f"{text}\n\n把建议应用到分集设置，并立即按新设置重新分析？",
            "应用建议并重新分析",
        )

    def _append(self, text: str, level: str = "info") -> None:
        self.log_appended.emit(text, level)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.current_plan is None:
            return False, "请先开始分析并生成方案。"
        return True, ""