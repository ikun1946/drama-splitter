"""导出表单：编码参数、预计空间、事务式批量导出与失败重试。

由 ``app/gui/dialogs/export_dialog.py`` 的 ExportDialog 承载；对话框负责
§37 前置检查的决策（可覆写 ``request_precheck``），本表单只负责执行。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.export import (
    CutMode,
    ExportPreset,
    nvenc_available,
    snap_plan_to_keyframes,
)
from ...core.probe import probe_keyframe_frames
from ...core.plan import BoundaryPlan
from ...core.settings import SplitSettings
from ...core.timebase import format_seconds_brief, format_timecode
from ..state import ProjectState
from ..widgets import (
    Card,
    LabeledField,
    NoWheelComboBox,
    NoWheelSpinBox,
    scrollable,
)
from ..widgets import message
from ..workers import (
    ExportWorker,
)
from ._common import (
    register_worker,
)


__all__ = ["ExportForm", "export_precheck", "precheck_prompt", "review_target"]


#: NVENC 可用性探测缓存（键 = ffmpeg 路径）。探测要真跑一次编码，
#: 同进程内不重复付这个成本。
_NVENC_CACHE: dict[str, bool] = {}


def nvenc_cached(binaries) -> bool:
    """带缓存的 NVENC 可用性探测；binaries 为空直接 False。"""
    if binaries is None:
        return False
    key = str(getattr(binaries, "ffmpeg", binaries))
    if key not in _NVENC_CACHE:
        _NVENC_CACHE[key] = nvenc_available(binaries)
    return _NVENC_CACHE[key]


def export_precheck(
    plan: BoundaryPlan,
    settings: SplitSettings | None = None,
) -> tuple[list, list, int]:
    """§37 导出前置检查：返回 (硬约束问题, 警告, 未审核切点数)。

    硬约束必须拦住；警告与未审核只提示、不禁止——用户可能有意忽略。
    与审核态「方案层校验」用同一套 ``plan.validate(settings)`` 入参，
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


def precheck_prompt(warnings: list, unreviewed: int) -> str:
    """§37 前置检查的提示文案（对话框与表单默认实现共用一份）。"""
    parts = []
    if unreviewed:
        parts.append(f"还有 {unreviewed} 个切点未审核")
    if warnings:
        parts.append(f"方案层校验有 {len(warnings)} 条警告")
    text = "；".join(parts) + "。是否继续？"
    text += "\n\n返回审核处理后再导出更稳妥；也可以选择继续导出（自行承担风险）。"
    if warnings:
        text += "\n\n警告：\n" + "\n".join(p.describe() for p in warnings)
    return text


def review_target(plan: BoundaryPlan, problems: list) -> tuple[int, int]:
    """「返回审核」时的定位目标：首条带集号的问题 → (集号, 毫秒)。

    毫秒取该集的片尾切点（问题就出在那条边界上）。没有带集号的问题时返回
    ``(0, 0)``——只回到审核态，不定位到具体位置。
    """
    episodes = plan.episodes()
    for problem in problems:
        if not problem.episode:
            continue
        index = max(1, min(int(problem.episode), len(episodes)))
        return index, int(float(episodes[index - 1].end_seconds) * 1000)
    return 0, 0


class ExportForm(QWidget):
    # 前置检查里选「返回审核」时发出，带 (集号, 毫秒位置)；集号 0 表示不定位
    review_requested = Signal(int, int)

    def __init__(self, state: ProjectState, parent: QWidget | None = None,
                 precheck=None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: ExportWorker | None = None
        # 前置检查的决策函数：(warnings, unreviewed) -> "continue" | "review"。
        # 由对话框注入它的 request_precheck，测试则直接覆写对话框的方法。
        self.precheck = precheck or self._default_precheck

        self.codec_combo = NoWheelComboBox()
        self.codec_combo.addItem("x264（CPU，兼容最好）", "libx264")
        self.codec_combo.addItem("NVENC（GPU 硬件加速）", "h264_nvenc")
        self.codec_combo.addItem("零损失快速复制（不重新编码）", "fast_copy")
        self._nvenc_ok = nvenc_cached(self.state.binaries)
        if not self._nvenc_ok:
            item = self.codec_combo.model().item(1)
            if item is not None:
                item.setEnabled(False)

        self.crf_spin = NoWheelSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(18)
        self.preset_combo = NoWheelComboBox()
        for name in ("ultrafast", "veryfast", "faster", "medium", "slow"):
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentText("medium")
        self.audio_bitrate_combo = NoWheelComboBox()
        for value in ("128k", "192k", "256k"):
            self.audio_bitrate_combo.addItem(value)
        self.audio_bitrate_combo.setCurrentText("192k")

        preset_card = Card("编码设置", parent=self)
        preset_form = QFormLayout()
        preset_form.addRow(QLabel("容器 / 视频编码："), QLabel("MP4 / H.264"))
        codec_field = LabeledField("编码方式")
        codec_field.attach(self.codec_combo)
        preset_form.addRow(codec_field)
        self.codec_hint = QLabel("")
        self.codec_hint.setWordWrap(True)
        self.codec_hint.setProperty("role", "muted")
        preset_form.addRow(self.codec_hint)
        crf_field = LabeledField("CRF（越小越清晰）")
        crf_field.attach(self.crf_spin)
        preset_form.addRow(crf_field)
        preset_field = LabeledField("编码速度")
        preset_field.attach(self.preset_combo)
        preset_form.addRow(preset_field)
        audio_field = LabeledField("音频")
        audio_field.attach(self.audio_bitrate_combo)
        preset_form.addRow(audio_field)
        self._crf_field = crf_field

        old = preset_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        preset_card.body.setLayout(preset_form)
        preset_card.body_layout = preset_form
        preset_form.setContentsMargins(12, 10, 12, 10)
        preset_form.setSpacing(8)
        self.codec_combo.currentIndexChanged.connect(self._sync_codec_fields)
        self._sync_codec_fields()

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
        self.btn_start.clicked.connect(lambda: self.start_export(None))
        self.btn_retry.clicked.connect(self._retry)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_retry)
        btn_row.addWidget(self.btn_start)

        # 正文（预设 / 输出 / 队列）整体可纵向滚动：小屏笔记本上，三张卡片的最小
        # 高度之和会超过屏幕，原先靠布局最小尺寸把窗口顶得比屏幕还高、还拖不小。
        # 装进滚动容器后，窗口能压到屏幕内，内容不够放时出滚动条，按钮行固定在底部。
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.addWidget(preset_card)
        body_layout.addWidget(output_card)
        body_layout.addWidget(queue_card, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(scrollable(body), 1)
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

    def _sync_codec_fields(self) -> None:
        """编码方式联动：GPU / 零损失下哪些参数还有意义。"""
        codec = self.codec_combo.currentData()
        lossless = codec == "fast_copy"
        gpu = codec == "h264_nvenc"
        self.crf_spin.setEnabled(not lossless)
        self.preset_combo.setEnabled(not lossless and not gpu)
        self.audio_bitrate_combo.setEnabled(not lossless)
        self._crf_field.set_label(
            "质量 CQ（越小越清晰）" if gpu else "CRF（越小越清晰）"
        )
        if lossless:
            self.codec_hint.setText(
                "不重新编码，画质与源片逐帧一致；切点会吸附到最近的关键帧，"
                "导出前会显示调整明细。"
            )
        elif gpu:
            self.codec_hint.setText(
                "NVIDIA 硬件编码（最高质量档），速度比 CPU 快数倍；质量由 CQ 控制。"
            )
        else:
            self.codec_hint.setText("")

    def _preset(self) -> ExportPreset:
        if self.codec_combo.currentData() == "h264_nvenc":
            return ExportPreset(
                video_codec="h264_nvenc",
                crf=self.crf_spin.value(),
                audio_bitrate=self.audio_bitrate_combo.currentText(),
            )
        return ExportPreset(
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
        )

    def start_export(self, only: list[int] | None = None) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None or self.state.binaries is None:
            message.warning(self, "无法导出", "请先完成导入、分析与审核。")
            return
        if self.state.output_root is None:
            message.warning(self, "缺少输出目录", "请先选择输出目录。")
            return

        blocking, warnings, unreviewed = export_precheck(plan, self.state.settings)
        if blocking:
            message.warning(
                self,
                "方案不可导出",
                "存在硬约束问题：\n" + "\n".join(p.describe() for p in blocking),
            )
            return

        # §37 导出前置检查：非阻断问题不禁止导出——用户可能有意忽略。
        # 给「继续导出」与「返回审核」两条路，让用户自己决定。
        if warnings or unreviewed:
            if self.precheck(warnings, unreviewed) != "continue":
                self.review_requested.emit(*review_target(plan, warnings))
                return

        cut_mode = CutMode.PRECISE
        export_plan = plan
        if self.codec_combo.currentData() == "fast_copy":
            prepared = self._prepare_fast_copy(plan, media)
            if prepared is None:
                return
            export_plan, cut_mode = prepared

        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self.worker = ExportWorker(
            self.state.binaries,
            media,
            export_plan,
            self.state.output_root,
            self.state.audio_stream_index,
            self._preset(),
            only_episodes=only,
            transcript=self.state.transcript,
            cut_mode=cut_mode,
            parent=self,
        )
        self.worker.episode_done.connect(self._on_episode_done)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        register_worker(self, "export", self.worker)
        self.worker.start()

    def _prepare_fast_copy(self, plan: BoundaryPlan, media) -> tuple[BoundaryPlan, CutMode] | None:
        """零损失快速导出的前置：探关键帧 → 吸附切点 → 用户确认明细。

        确认走 ``_confirm_fast_copy``（可覆写，与 precheck 同一套路数）——
        测试与离屏冒烟不会真的弹模态框。
        """
        binaries = self.state.binaries
        if binaries is None:
            message.warning(self, "无法快速导出", "尚未找到 FFmpeg，无法探测关键帧。")
            return None
        try:
            keyframes = probe_keyframe_frames(binaries, media)
        except Exception as exc:  # noqa: BLE001 - 探测失败要变成可读提示
            message.warning(self, "关键帧探测失败", str(exc))
            return None

        snapped_plan, snaps, problems = snap_plan_to_keyframes(plan, media, keyframes)
        if problems:
            message.warning(
                self, "有切点无法吸附", "\n".join(problems) + "\n\n请改用精确导出。"
            )
            return None

        moved = [s for s in snaps if s.moved]
        if moved:
            lines = [
                f"第 {s.index} 个切点：{format_timecode(s.original_seconds)} → "
                f"{format_timecode(s.snapped_seconds)}"
                f"（{float(s.offset_seconds):+.1f} 秒）"
                for s in moved
            ]
            detail = "以下切点将吸附到最近的关键帧：\n" + "\n".join(lines)
        else:
            detail = "所有切点都已在关键帧上，无需调整。"

        if not self._confirm_fast_copy(detail):
            return None
        return snapped_plan, CutMode.FAST_COPY

    def _confirm_fast_copy(self, detail: str) -> bool:
        """零损失导出的确认框；测试可覆写为直接返回 True。

        切点明细一行一条，几十集的片就是几十行——QMessageBox 会被撑出屏幕
        （按钮够不着、内容被截断）。这里用固定高度的滚动确认框，超出部分
        在内部上下滚，按钮永远可见。
        """
        text = (
            f"{detail}\n\n"
            "零损失快速导出不重新编码，画质与源片完全一致；"
            "各集时长会按上面的调整变化。"
        )
        return message.scrollable_confirm(
            self, "零损失快速导出", text, "开始导出", "取消"
        )

    def _default_precheck(self, warnings: list, unreviewed: int) -> str:
        """§37 前置检查的默认实现：返回 "continue" 或 "review"。

        对话框承载时会把自己的 ``request_precheck`` 注入进来；测试与离屏冒烟
        覆写对话框的那个方法即可绕过模态——QMessageBox 实例的 exec() 无法
        通过拦截静态方法替代，离屏跑链路时会永久阻塞（实测挂过 25 分钟）。
        """
        agreed = message.confirm_continue(
            self, "导出前置检查", precheck_prompt(warnings, unreviewed),
            "继续导出", "返回审核",
        )
        return "continue" if agreed else "review"

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
            message.information(self, "没有失败项", "所有集都已成功导出。")
            return
        self.start_export(failed)

    def _cancel(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
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
        note = result.message
        if result.warnings:
            note = note + "；" + "；".join(result.warnings)
        self.queue.setItem(row, 4, QTableWidgetItem(note))

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
            message.information(self, "已取消", batch.summary())
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
        message.information(self, "导出结束", text)

    def _on_failed(self, text: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        message.critical(self, "导出失败", text)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.output_root is None:
            return False, "请先选择输出目录。"
        return True, ""
