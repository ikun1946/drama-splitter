"""中栏「分析」pane：阶段清单、每阶段进度、总进度与最近一条输出。

分析日志只有一个落点在右栏检查器的「日志」页签（``log_appended`` 广播出去，
由工作台接进去）。

进度分两层（取代原先"只有一条进度条"的画法）：

- 每个阶段一条自己的胶囊进度条。阶段没有上报细粒度数据时显示**流动高光**
  （见 ``CapsuleProgressBar``），而不是停在 0%——"正在做、剩余时间未知"是
  诚实的表达；
- 下方一条总进度，等于 (已完成阶段数 + 当前阶段比例) / 阶段总数，底栏那条
  小进度条镜像的就是它。

清单里只有分析线程真正会跑的六个阶段：导出不在本页发生（导出对话框里另有
自己的进度）。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
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
from ..theme import current_mode, qcolor
from ..widgets import CapsuleProgressBar, message
from ..workers import AnalysisWorker
from ._common import register_worker

__all__ = ["AnalysisPane"]


class StageState(Enum):
    """阶段状态。颜色、点形、状态文字全由它派生，避免三处各写一套。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 状态 → 填充色调（胶囊条）
_TONE = {
    StageState.PENDING: "muted",
    StageState.RUNNING: "normal",
    StageState.DONE: "done",
    StageState.FAILED: "failed",
    StageState.CANCELLED: "muted",
}

#: 状态 → 状态文字
_STATUS_TEXT = {
    StageState.PENDING: "等待",
    StageState.RUNNING: "进行中",
    StageState.DONE: "已完成",
    StageState.FAILED: "失败",
    StageState.CANCELLED: "已取消",
}

#: 状态 → 前置圆点颜色令牌（沿用全站状态色）
_DOT_KEY = {
    StageState.PENDING: "NEUTRAL",
    StageState.RUNNING: "PRIMARY",
    StageState.DONE: "SUCCESS",
    StageState.FAILED: "DANGER",
    StageState.CANCELLED: "NEUTRAL",
}

_DOT_D = 8


class _StageDot(QWidget):
    """阶段清单最左侧的状态圆点——一眼看出哪几步过了、哪步在跑。

    逐帧画没必要：颜色只随阶段状态变，所以画进一张缓存 pixmap，状态或主题
    变了才重建（判据做成 ``_static_key``，与时间轴同一套做法）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = StageState.PENDING
        self._static: QPixmap | None = None
        self._static_key: tuple | None = None
        self.setFixedSize(_DOT_D + 6, _DOT_D + 6)

    def set_state(self, state: StageState) -> None:
        if state is self._state:
            return
        self._state = state
        self.update()

    def state(self) -> StageState:
        return self._state

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        key = (self._state, current_mode(), self.width(), self.height())
        if self._static is None or key != self._static_key:
            self._static = self._rebuild(key)
            self._static_key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._static)
        painter.end()

    def _rebuild(self, key: tuple) -> QPixmap:
        state, mode, w, h = key
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap(int(w * dpr), int(h * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        tone = QColor(qcolor(mode, _DOT_KEY[state]))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        if state is StageState.DONE:
            # 实心：已完成的事不必再抢注意力，但颜色仍是成功色
            painter.setBrush(tone)
            painter.drawEllipse(_centered_rect(w, h, _DOT_D))
        elif state in (StageState.RUNNING, StageState.FAILED):
            # 当前阶段：实心圆 + 一圈半透明外环，比别的点更"亮"
            halo = QColor(tone)
            halo.setAlpha(70)
            painter.setBrush(halo)
            painter.drawEllipse(_centered_rect(w, h, _DOT_D + 4))
            painter.setBrush(tone)
            painter.drawEllipse(_centered_rect(w, h, _DOT_D))
        else:
            # 等待 / 取消：空心环（弱化）
            ring = QPen(QColor(qcolor(mode, "BORDER_STRONG")))
            ring.setWidthF(1.4)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(_centered_rect(w, h, _DOT_D - 1))
        painter.end()
        return pixmap


def _centered_rect(w: float, h: float, d: float) -> QRectF:
    """以 (w, h) 为中心、边长 d 的方形，供 drawEllipse 使用。"""
    return QRectF((w - d) / 2, (h - d) / 2, d, d)


class _StageRow:
    """阶段清单里的一行：状态点 / 名称 / 本阶段胶囊进度条 / 状态文字。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.state = StageState.PENDING
        self.dot = _StageDot()
        self.label = QLabel(name)
        self.bar = CapsuleProgressBar(thickness=8)
        self.bar.setFixedWidth(200)
        self.status = QLabel("等待")
        self.status.setProperty("role", "muted")
        self.status.setMinimumWidth(52)

    def _apply(self, state: StageState) -> None:
        self.state = state
        self.dot.set_state(state)
        self.bar.set_tone(_TONE[state])
        self.status.setText(_STATUS_TEXT[state])

    def mark_pending(self) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self._apply(StageState.PENDING)

    def mark_running(self) -> None:
        # 流动模式：数值范围 0–0，胶囊条自己扫高光
        self.bar.setRange(0, 0)
        self._apply(StageState.RUNNING)

    def mark_progress(self, ratio: float) -> None:
        if self.bar.maximum() == 0:      # 首次拿到具体比例，从流动切到确定模式
            self.bar.setRange(0, 100)
        value = int(round(max(0.0, min(1.0, ratio)) * 100))
        self.bar.setValue(value)
        self.state = StageState.RUNNING
        self.dot.set_state(StageState.RUNNING)
        self.bar.set_tone(_TONE[StageState.RUNNING])
        self.status.setText(f"{value}%")

    def mark_done(self) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self._apply(StageState.DONE)

    def mark_failed(self) -> None:
        self.bar.setRange(0, 100)        # 停在失败时的数值，不再假装在跑
        self._apply(StageState.FAILED)

    def mark_cancelled(self) -> None:
        self.bar.setRange(0, 100)
        self._apply(StageState.CANCELLED)


class AnalysisPane(QWidget):
    STAGES = [
        "字幕解析",
        "语音转写",
        "镜头与黑场",
        "候选点",
        "整体规划",
        "边界审核",
    ]

    started = Signal()
    stage_changed = Signal(str)
    progress_changed = Signal(str, float)   # (最近输出, 总进度 0-1)
    log_appended = Signal(str, str)         # (文本, 级别)
    finished = Signal()

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: AnalysisWorker | None = None
        # 「已请求开始」标记：start() 里发出 started 时线程还没真正跑起来
        # （worker.start() 在其后一行），只看 isRunning() 会让界面停在上一阶段。
        self._active = False
        # 当前阶段序号与它自己的比例：总进度据此折算
        self._current = -1
        self._stage_ratio = 0.0

        self.stage_label = QLabel("等待开始")
        self.stage_label.setProperty("role", "title")
        self.stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stage_rows = [_StageRow(name) for name in self.STAGES]

        table = QGridLayout()
        table.setHorizontalSpacing(10)
        table.setVerticalSpacing(8)
        table.setColumnStretch(2, 1)     # 胶囊条独占可伸缩的一列
        for row, stage_row in enumerate(self.stage_rows):
            table.addWidget(stage_row.dot, row, 0, Qt.AlignmentFlag.AlignVCenter)
            table.addWidget(stage_row.label, row, 1)
            table.addWidget(stage_row.bar, row, 2, Qt.AlignmentFlag.AlignVCenter)
            table.addWidget(stage_row.status, row, 3)
        table_host = QWidget(self)
        table_host.setLayout(table)
        table_host.setMinimumWidth(360)

        table_line = QHBoxLayout()
        table_line.addStretch(1)
        table_line.addWidget(table_host, 3)
        table_line.addStretch(1)

        self.total_label = QLabel("总进度")
        self.total_label.setProperty("role", "muted")
        self.total = CapsuleProgressBar(thickness=10)
        self.total.setMinimumWidth(240)
        self.total.setMaximumWidth(420)

        total_line = QHBoxLayout()
        total_line.addStretch(1)
        total_line.addWidget(self.total_label)
        total_line.addWidget(self.total)
        total_line.addStretch(1)

        self.recent = QLabel("分析会依次完成字幕解析、语音转写、镜头检测、候选点、"
                             "整体规划与整集复核。")
        self.recent.setProperty("role", "muted")
        self.recent.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)
        outer.addStretch(1)
        outer.addWidget(self.stage_label)
        outer.addLayout(table_line)
        outer.addLayout(total_line)
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

        self._reset_progress()
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

    def _reset_progress(self) -> None:
        self._current = -1
        self._stage_ratio = 0.0
        for row in self.stage_rows:
            row.mark_pending()
        self.total.set_tone("normal")
        self.total.setValue(0)

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
        if 0 <= self._current < len(self.stage_rows):
            self.stage_rows[self._current].mark_cancelled()
        self.total.set_tone("muted")
        self._append("已取消。", "warn")
        self.stage_label.setText("已取消")
        self.finished.emit()

    # ---- 进度回调 ------------------------------------------------------

    def _on_stage(self, name: str) -> None:
        index = self.STAGES.index(name) if name in self.STAGES else -1
        if index >= 0:
            for row in self.stage_rows[:index]:
                row.mark_done()
            for row in self.stage_rows[index + 1:]:
                row.mark_pending()
            self.stage_rows[index].mark_running()
            self._current = index
            self._stage_ratio = 0.0
            self.stage_label.setText(f"阶段 {index + 1}/{len(self.STAGES)} · {name}")
        else:
            self.stage_label.setText(name)
        self._sync_total()
        self._append(f"—— {name} ——")
        self.stage_changed.emit(name)

    def _on_progress(self, text: str, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        if 0 <= self._current < len(self.stage_rows):
            self.stage_rows[self._current].mark_progress(ratio)
            self._stage_ratio = ratio
        self.recent.setText(text)
        self._sync_total()

    def _sync_total(self) -> None:
        if self._current < 0:
            ratio = 0.0
        else:
            ratio = (self._current + self._stage_ratio) / len(self.STAGES)
        ratio = max(0.0, min(1.0, ratio))
        self.total.setValue(int(round(ratio * 100)))
        self.progress_changed.emit(self.recent.text(), ratio)

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

        for row in self.stage_rows:
            row.mark_done()
        self._current = len(self.STAGES) - 1
        self._stage_ratio = 1.0
        self.total.set_tone("done")
        self.total.setValue(100)
        self.stage_label.setText("分析完成")
        self._active = False

        # 模式C：把自动判定结论提到方案摘要**最前面**。用户最想知道的一句是
        # "为什么是这么多集、每集多长"，而在日志里它要滚到一半才看得到。
        decision = getattr(worker, "timing_decision", None) if worker else None
        if decision is not None and decision.chosen is not None:
            summary = (
                f"推荐方案：每集 {float(decision.chosen.target_duration):.1f} 秒 · "
                f"共 {decision.chosen.episode_count} 集 · {decision.chosen.label}"
            )
            self.recent.setText(summary)
            self._append(f"★ {summary}")
            self._append(f"  依据：{decision.beat.describe()}")
        else:
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
        worker = self.sender() or self.worker
        if getattr(worker, "state", "") == "cancelled":
            # 取消不是失败：不弹警告框、不触发「应用建议」，阶段状态由 cancel() 收尾
            return
        self._append(f"失败：{text}", "error")
        if 0 <= self._current < len(self.stage_rows):
            self.stage_rows[self._current].mark_failed()
        self.total.set_tone("failed")
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