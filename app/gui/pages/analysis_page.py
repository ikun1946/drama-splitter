"""页 3 分析：跑后台分析流水线，分阶段汇报进度与结论。"""

from __future__ import annotations


from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.plan import BoundaryPlan
from ..state import ProjectState
from ..widgets import (
    Card,
    LogPanel,
    StepIndicator,
)
from ..workers import (
    AnalysisWorker,
)
from ._common import (
    register_worker,
)


__all__ = ["AnalysisPage"]


class AnalysisPage(QWidget):
    STAGES = [
        "字幕解析",
        "语音转写",
        "镜头与黑场",
        "候选点",
        "整体规划",
        "边界审核",
        "导出",
    ]

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._worker: AnalysisWorker | None = None

        # 横向步骤条
        self.stepper = StepIndicator(self.STAGES)

        # 当前阶段名 + 进度条
        self.stage_label = QLabel("等待开始")
        self.stage_label.setProperty("role", "title")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")

        stage_card = Card("阶段与进度", parent=self)
        stage_card.body_layout.addWidget(self.stage_label)
        stage_card.body_layout.addWidget(self.progress)

        # 日志
        self.log = LogPanel()
        log_card = Card("日志", parent=self)
        log_card.body_layout.addWidget(self.log, 1)

        # 操作按钮
        self.btn_start = QPushButton("▶  开始分析")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_start)

        # 装配
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(self.stepper)
        outer.addWidget(stage_card)
        outer.addWidget(log_card, 1)
        outer.addLayout(btn_row)

    def _start(self) -> None:
        if self.state.media is None:
            QMessageBox.warning(self, "尚未导入", "请先在导入页选择源视频。")
            return
        settings = self.state.settings
        problems = settings.validate()
        if problems:
            QMessageBox.warning(self, "参数不完整", problems[0])
            return
        if self.state.binaries is None:
            QMessageBox.warning(self, "缺少 FFmpeg", "尚未定位到 FFmpeg。")
            return

        self.log.clear()
        self.progress.setValue(0)
        self.stepper.set_done()
        self.stepper.set_current(0)

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self._worker = AnalysisWorker(
            self.state.binaries,
            self.state.media,
            self.state.audio_stream_index,
            settings,
            self,
        )
        self._worker.stage_started.connect(self._on_stage)
        self._worker.stage_progress.connect(self._on_progress)
        self._worker.stage_log.connect(self._append)
        self._worker.completed.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        register_worker(self, "analysis", self._worker)
        self._worker.start()

    def _on_stage(self, name: str) -> None:
        index = self.STAGES.index(name) if name in self.STAGES else 0
        self.stage_label.setText(f"当前阶段：{name}")
        self.stepper.set_done(*range(index))
        self.stepper.set_current(index)
        self._append(f"—— {name} ——")

    def _on_progress(self, text: str, ratio: float) -> None:
        self.progress.setValue(int(ratio * 100))
        self.progress.setFormat(f"{text}（{int(ratio * 100)}%）")

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._append("已取消。", "warn")
        self.progress.setValue(0)

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
        self.stepper.set_done(*range(len(self.STAGES)))

        self._append(f"候选点：{candidates.describe()}")
        for note in candidates.limitations:
            self._append(f"  限流：{note}")
        if candidates.points:
            top = candidates.top(3)
            self._append("评分最高的候选点：")
            for point in top:
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

        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _on_failed(self, message: str) -> None:
        self._append(f"失败：{message}", "error")
        self.progress.setValue(0)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        QMessageBox.warning(self, "无法完成分析", message)

    def _append(self, text: str, level: str = "info") -> None:
        self.log.append(text, level=level)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.current_plan is None:
            return False, "请先在分析页生成方案。"
        return True, ""
