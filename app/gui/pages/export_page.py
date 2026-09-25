"""页 5 导出：编码参数、预计空间、事务式批量导出与失败重试。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.export import ExportPreset
from ...core.plan import BoundaryPlan
from ...core.settings import SplitSettings
from ...core.timebase import format_seconds_brief
from ..state import ProjectState
from ..widgets import (
    Card,
    LabeledField,
)
from ..workers import (
    ExportWorker,
)
from ._common import (
    register_worker,
)


__all__ = ["ExportPage", "export_precheck"]


def export_precheck(
    plan: BoundaryPlan,
    settings: SplitSettings | None = None,
) -> tuple[list, list, int]:
    """§37 导出前置检查：返回 (硬约束问题, 警告, 未审核切点数)。

    硬约束必须拦住；警告与未审核只提示、不禁止——用户可能有意忽略。
    与审核页「方案层校验」用同一套 ``plan.validate(settings)`` 入参，
    保证「返回审核」后用户看到的问题清单与导出前检查一致。
    """
    problems = plan.validate(settings, None)
    blocking = [p for p in problems if p.is_blocking]
    warnings = [p for p in problems if not p.is_blocking]
    unreviewed = (
        0
        if plan.semantic_review_status == "confirmed"
        else max(0, plan.episode_count - 1)   # 首尾是片头片尾，不算切点
    )
    return blocking, warnings, unreviewed


class ExportPage(QWidget):
    # §37：用户在导出前置检查中选择「返回审核」时发出，由主窗口负责切页
    review_requested = Signal()

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._worker: ExportWorker | None = None

        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(18)
        self.preset_combo = QComboBox()
        for name in ("ultrafast", "veryfast", "faster", "medium", "slow"):
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentText("medium")
        self.audio_bitrate_combo = QComboBox()
        for value in ("128k", "192k", "256k"):
            self.audio_bitrate_combo.addItem(value)
        self.audio_bitrate_combo.setCurrentText("192k")

        preset_card = Card("编码预设（精确裁切，重新编码）", parent=self)
        preset_form = QFormLayout()
        preset_form.addRow(QLabel("容器 / 视频编码："), QLabel("MP4 / H.264（libx264）"))
        crf_field = LabeledField("CRF")
        crf_field.attach(self.crf_spin)
        preset_form.addRow(crf_field)
        preset_field = LabeledField("编码速度")
        preset_field.attach(self.preset_combo)
        preset_form.addRow(preset_field)
        audio_field = LabeledField("音频")
        audio_field.attach(self.audio_bitrate_combo)
        preset_form.addRow(audio_field)

        old = preset_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        preset_card.body.setLayout(preset_form)
        preset_card.body_layout = preset_form
        preset_form.setContentsMargins(12, 10, 12, 10)
        preset_form.setSpacing(8)

        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.btn_output = QPushButton("📂  选择输出目录…")
        self.btn_output.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_output)

        self.estimate = QLabel("—")

        output_card = Card("输出目录", parent=self)
        output_card.body_layout.addLayout(output_row)
        output_card.body_layout.addWidget(self.estimate)

        self.queue = QTableWidget(0, 5)
        self.queue.setHorizontalHeaderLabels(["集号", "状态", "帧数校验", "时长", "说明"])
        self.queue.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue.setAlternatingRowColors(True)

        queue_card = Card("导出队列", parent=self)
        queue_card.body_layout.addWidget(self.queue, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        self.btn_start = QPushButton("▶  开始导出")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)
        self.btn_retry = QPushButton("重试失败项")
        self.btn_cancel = QPushButton("取消")
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(lambda: self._start(None))
        self.btn_retry.clicked.connect(self._retry)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_retry)
        btn_row.addWidget(self.btn_start)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(preset_card)
        outer.addWidget(output_card)
        outer.addWidget(queue_card, 1)
        outer.addWidget(self.progress)
        outer.addLayout(btn_row)

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if directory:
            self.state.output_root = Path(directory)
            self.output_edit.setText(directory)
            self._refresh_estimate()

    def refresh(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            self.queue.setRowCount(0)
            return
        episodes = plan.episodes()
        self.queue.setRowCount(len(episodes))
        for row, episode in enumerate(episodes):
            cells = [
                f"第{episode.index:02d}集",
                "待导出",
                "—",
                format_seconds_brief(episode.duration_seconds),
                "",
            ]
            for column, text in enumerate(cells):
                self.queue.setItem(row, column, QTableWidgetItem(text))
        self._refresh_estimate()

    def _refresh_estimate(self) -> None:
        media = self.state.media
        if media is None or not media.bit_rate:
            self.estimate.setText("预计空间：源片未提供码率，无法估算。")
            return
        seconds = float(media.timeline_duration)
        estimated = media.bit_rate * seconds / 8 * 1.1
        self.estimate.setText(
            f"预计空间：约 {estimated / 1024 / 1024:.0f} MB"
            "（按源片码率上浮 10% 粗略估算；CRF 编码实际大小取决于画面复杂度）"
        )

    def _preset(self) -> ExportPreset:
        return ExportPreset(
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
        )

    def _start(self, only: list[int] | None) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None or self.state.binaries is None:
            QMessageBox.warning(self, "无法导出", "请先完成导入、分析与审核。")
            return
        if self.state.output_root is None:
            QMessageBox.warning(self, "缺少输出目录", "请先选择输出目录。")
            return

        blocking, warnings, unreviewed = export_precheck(plan, self.state.settings)
        if blocking:
            QMessageBox.warning(
                self,
                "方案不可导出",
                "存在硬约束问题：\n" + "\n".join(p.describe() for p in blocking),
            )
            return

        # §37 导出前置检查：非阻断问题不禁止导出——用户可能有意忽略。
        # 给「继续导出」与「返回审核」两条路，让用户自己决定。
        if warnings or unreviewed:
            if not self._confirm_precheck(warnings, unreviewed):
                self.review_requested.emit()
                return

        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self._worker = ExportWorker(
            self.state.binaries,
            media,
            plan,
            self.state.output_root,
            self.state.audio_stream_index,
            self._preset(),
            only_episodes=only,
            transcript=self.state.transcript,
            parent=self,
        )
        self._worker.episode_done.connect(self._on_episode_done)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        register_worker(self, "export", self._worker)
        self._worker.start()

    def _confirm_precheck(self, warnings: list, unreviewed: int) -> bool:
        """§37 前置检查对话框。返回 True=继续导出，False=返回审核。

        单独提方法而不是内联弹窗：自动化环境（tools/gui_smoke.py）覆写它
        即可绕过模态——QMessageBox 实例的 exec() 无法通过拦截静态方法替代，
        离屏跑链路时会永久阻塞（实测踩过）。
        """
        parts = []
        if unreviewed:
            parts.append(f"还有 {unreviewed} 个切点未审核")
        if warnings:
            parts.append(f"方案层校验有 {len(warnings)} 条警告")
        box = QMessageBox(self)
        box.setWindowTitle("导出前置检查")
        box.setText("；".join(parts) + "。是否继续？")
        informative = "返回审核处理后再导出更稳妥；也可以选择继续导出（自行承担风险）。"
        if warnings:
            informative += "\n\n警告：\n" + "\n".join(p.describe() for p in warnings)
        box.setInformativeText(informative)
        box.addButton("继续导出", QMessageBox.ButtonRole.AcceptRole)
        btn_back = box.addButton("返回审核", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is not btn_back

    def _retry(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        failed = [
            episode.index
            for episode in plan.episodes()
            if episode.index not in self.state.exported
        ]
        if not failed:
            QMessageBox.information(self, "没有失败项", "所有集都已成功导出。")
            return
        self._start(failed)

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _on_episode_done(self, result) -> None:
        plan = self.state.current_plan
        row = result.episode_index - 1
        if plan is None or row < 0 or row >= self.queue.rowCount():
            return
        self.queue.setItem(row, 1, QTableWidgetItem("成功" if result.success else "失败"))
        checks = (
            f"{result.actual_frame_count}/{result.expected_frame_count}"
            if result.actual_frame_count is not None
            else "—"
        )
        self.queue.setItem(row, 2, QTableWidgetItem(checks))
        if result.actual_duration is not None:
            self.queue.setItem(row, 3, QTableWidgetItem(format_seconds_brief(result.actual_duration)))
        message = result.message
        if result.warnings:
            message = message + "；" + "；".join(result.warnings)
        self.queue.setItem(row, 4, QTableWidgetItem(message))

        if result.success and result.output_path:
            self.state.exported[result.episode_index] = (result.output_path, plan.version)

        total = max(1, self.queue.rowCount())
        done = sum(1 for r in range(self.queue.rowCount())
                   if self.queue.item(r, 1) and self.queue.item(r, 1).text() in {"成功", "失败"})
        self.progress.setValue(int(done * 100 / total))

    def _on_completed(self, batch) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_retry.setEnabled(bool(batch.failed))
        if batch.cancelled:
            QMessageBox.information(self, "已取消", batch.summary())
            return
        text = batch.summary() + f"\n输出目录：{batch.plan_directory}"
        if batch.plan_layer_problems:
            text += "\n计划层问题：\n" + "\n".join(batch.plan_layer_problems)

        if batch.succeeded:
            worker = self.sender()
            if worker is not None and hasattr(worker, "_export_subtitles"):
                worker._export_subtitles()
                results = getattr(worker, "subtitle_results", [])
                written = [r for r in results if r.success]
                if written:
                    text += f"\n字幕：已生成 {len(written)} 集 SRT"
                else:
                    reason = next(
                        (r.notes[0] for r in results if r.notes),
                        "未产出字幕文件",
                    )
                    text += f"\n字幕：未生成（{reason}）"
                for result in results:
                    if not result.success and result.notes:
                        text += f"\n  第{result.episode:02d}集未生成字幕：{result.notes[0]}"
                    if result.clipped:
                        text += f"\n  第{result.episode:02d}集有 {len(result.clipped)} 条字幕被截断"
            else:
                text += "\n字幕：未生成（分析阶段未产出转写）"
        QMessageBox.information(self, "导出结束", text)

    def _on_failed(self, message: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        QMessageBox.critical(self, "导出失败", message)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.output_root is None:
            return False, "请先选择输出目录。"
        return True, ""
