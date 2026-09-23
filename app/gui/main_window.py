"""主窗口与五个工作页面（方案 §13.1 推荐界面结构）。

页面职责严格对应 §13.1 的表格：
    导入 → 源文件、时长、画幅、帧率类型、音轨、编码、解码检查、兼容性提示
    设置 → 分集模式、严格/弹性集数、范围来源、策略、实时可行性提示
    分析 → 当前阶段、已完成量、剩余估计、调用量、暂停/取消
    审核 → 集数列表、时长分布、当前集内容、切点前后预览、风险筛选
    导出 → 方案版本、模式、编码预设、输出目录、预计空间、队列和失败重试
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.export import ExportPreset, Exporter
from ..core.ffmpeg import check_encoders, locate_ffmpeg
from ..core.plan import BoundaryPlan
from ..core.probe import (
    LEVEL_BLOCK,
    LEVEL_INFO,
    LEVEL_WARN,
    check_compatibility,
    describe_media,
)
from ..core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    Strategy,
    check_feasibility,
)
from ..core.timebase import format_seconds_brief, format_timecode
from .frame_source import DecodeError, FrameDecoder, StreamPlayer
from .state import ProjectState
from .workers import ExportWorker, PlanWorker, ProbeWorker

__all__ = ["MainWindow"]

LEVEL_TEXT = {LEVEL_BLOCK: "阻断", LEVEL_WARN: "警告", LEVEL_INFO: "提示"}
LEVEL_COLOR = {LEVEL_BLOCK: "#b3261e", LEVEL_WARN: "#9a6700", LEVEL_INFO: "#3d6b35"}


# ---------------------------------------------------------------------------
# 预览面板
# ---------------------------------------------------------------------------


class PreviewPanel(QWidget):
    """帧级预览：画面 + 播放控制。

    画面由 FFmpeg 解出的真实帧驱动（§13.3），不使用播放器进度条近似。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None

        self.view = QLabel("尚未选择源文件")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(480, 270)
        self.view.setStyleSheet("background:#111; color:#bbb; border:1px solid #333;")

        self.timecode = QLabel("--:--:--.---")
        self.timecode.setStyleSheet("font-family:Consolas,monospace; font-size:13px;")

        controls = QHBoxLayout()
        self.btn_play = QPushButton("播放本集")
        self.btn_boundary = QPushButton("播放切点前后")
        self.btn_stitch = QPushButton("成片连看切点")
        self.btn_prev_frame = QPushButton("上一帧")
        self.btn_next_frame = QPushButton("下一帧")
        self.btn_back_1s = QPushButton("−1秒")
        self.btn_fwd_1s = QPushButton("+1秒")
        self.btn_back_5s = QPushButton("−5秒")
        self.btn_fwd_5s = QPushButton("+5秒")
        self.btn_stop = QPushButton("停止")
        for button in (
            self.btn_play,
            self.btn_boundary,
            self.btn_stitch,
            self.btn_prev_frame,
            self.btn_next_frame,
            self.btn_back_1s,
            self.btn_fwd_1s,
            self.btn_back_5s,
            self.btn_fwd_5s,
            self.btn_stop,
        ):
            button.setMinimumWidth(0)
            controls.addWidget(button)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.timecode)
        layout.addLayout(controls)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self._repaint()

    def clear(self, text: str = "尚未选择源文件") -> None:
        self._image = None
        self.view.setPixmap(QPixmap())
        self.view.setText(text)

    def _repaint(self) -> None:
        if self._image is None:
            return
        self.view.setText("")
        pixmap = QPixmap.fromImage(self._image)
        scaled = pixmap.scaled(
            self.view.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.view.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._repaint()


# ---------------------------------------------------------------------------
# 页1 导入
# ---------------------------------------------------------------------------


class ImportPage(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._worker: ProbeWorker | None = None

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.btn_browse = QPushButton("选择源视频…")
        self.btn_browse.clicked.connect(self._choose_file)

        row = QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.btn_browse)

        self.summary = QLabel("尚未导入。")
        self.summary.setWordWrap(True)

        self.issues = QTableWidget(0, 4)
        self.issues.setHorizontalHeaderLabels(["级别", "代码", "说明", "建议"])
        self.issues.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.issues.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.issues.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        self.audio_combo = QComboBox()
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        audio_group = QGroupBox("主对白音轨（多音轨时必须显式选择）")
        audio_layout = QVBoxLayout(audio_group)
        self.audio_hint = QLabel("导入后列出可选音轨。")
        self.audio_hint.setWordWrap(True)
        audio_layout.addWidget(self.audio_combo)
        audio_layout.addWidget(self.audio_hint)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#555;")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("① 选择一条已剪辑完成的短剧长视频"))
        layout.addLayout(row)
        layout.addWidget(self.summary)
        layout.addWidget(self.issues, 1)
        layout.addWidget(audio_group)
        layout.addWidget(self.status)
        layout.addStretch(0)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择源视频",
            "",
            "视频文件 (*.mp4 *.mov *.mkv *.avi *.ts *.m4v);;所有文件 (*)",
        )
        if not path:
            return
        self.path_edit.setText(path)
        self._probe(path)

    def _probe(self, path: str) -> None:
        if self.state.binaries is None:
            try:
                binaries = locate_ffmpeg()
                check_encoders(binaries)
                self.state.binaries = binaries
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "找不到 FFmpeg", str(exc))
                return

        self.status.setText("正在探测媒体信息…")
        self.issues.setRowCount(0)
        self.state.reset_media()

        self._worker = ProbeWorker(self.state.binaries, path, self)
        self._worker.completed.connect(self._on_probed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_probed(self, info, issues) -> None:
        self.state.media = info
        self.status.setText("探测完成。")
        self.summary.setText(describe_media(info))

        self.issues.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            cells = [
                LEVEL_TEXT.get(issue.level, issue.level),
                issue.code,
                issue.message,
                issue.detail,
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(QColor(LEVEL_COLOR.get(issue.level, "#333333")))
                self.issues.setItem(row, column, item)

        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        if info.audio_tracks:
            for track in info.audio_tracks:
                self.audio_combo.addItem(track.describe(), track.index)
            self.audio_combo.setCurrentIndex(0)
            self.state.audio_stream_index = info.audio_tracks[0].index
            if len(info.audio_tracks) > 1:
                self.audio_hint.setText("检测到多条音轨，请确认哪一条是主对白轨。")
            else:
                self.audio_hint.setText("只有一条音轨，已自动选择。")
        else:
            self.audio_combo.addItem("无音轨（跳过对白分析）", None)
            self.state.audio_stream_index = None
            self.audio_hint.setText("源片没有音轨，将只做画面分析与人工审核（§7.2）。")
        self.audio_combo.blockSignals(False)

        blocking = [issue for issue in issues if issue.is_blocking]
        if blocking:
            self.status.setText(f"探测完成，但有 {len(blocking)} 项阻断问题，需先解决。")
        else:
            self.status.setText("探测完成，可以继续设置分集目标。")

    def _on_failed(self, message: str) -> None:
        self.status.setText("探测失败。")
        QMessageBox.critical(self, "探测失败", message)

    def _audio_changed(self, index: int) -> None:
        if index >= 0:
            self.state.audio_stream_index = self.audio_combo.itemData(index)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.media is None:
            return False, "请先导入源视频。"
        if self.state.binaries is None:
            return False, "尚未定位到 FFmpeg。"
        blocking = [
            issue
            for issue in check_compatibility(self.state.media, self.state.binaries)
            if issue.is_blocking
        ]
        if blocking:
            return False, "存在阻断级兼容性问题：" + blocking[0].message
        if self.state.media.video is None:
            return False, "源文件没有可用视频轨。"
        return True, ""


# ---------------------------------------------------------------------------
# 页2 设置
# ---------------------------------------------------------------------------


class SettingsPage(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state

        # 模式
        self.radio_by_duration = QRadioButton("按目标时长（系统求集数）")
        self.radio_by_count = QRadioButton("按目标集数")
        self.radio_recommend = QRadioButton("推荐方案（先按时长估算，分析后再给剧情建议）")
        self.radio_by_duration.setChecked(True)
        mode_group = QGroupBox("分集模式")
        mode_layout = QVBoxLayout(mode_group)
        for widget in (self.radio_by_duration, self.radio_by_count, self.radio_recommend):
            mode_layout.addWidget(widget)
            widget.toggled.connect(self._refresh)

        # 目标数值
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(1.0, 3600.0)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(120.0)
        self.duration_spin.setSuffix(" 秒")
        self.duration_spin.valueChanged.connect(self._refresh)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(60)
        self.count_spin.valueChanged.connect(self._refresh)

        self.policy_exact = QRadioButton("严格集数：必须恰好 N 集，无解就明确报错")
        self.policy_flexible = QRadioButton("弹性集数：在允许区间内比较方案")
        self.policy_exact.setChecked(True)
        self.allowed_min = QSpinBox()
        self.allowed_min.setRange(1, 9999)
        self.allowed_min.setValue(58)
        self.allowed_max = QSpinBox()
        self.allowed_max.setRange(1, 9999)
        self.allowed_max.setValue(62)
        for widget in (self.policy_exact, self.policy_flexible):
            widget.toggled.connect(self._refresh)
        for widget in (self.allowed_min, self.allowed_max):
            widget.valueChanged.connect(self._refresh)

        flexible_row = QHBoxLayout()
        flexible_row.addWidget(QLabel("允许集数区间："))
        flexible_row.addWidget(self.allowed_min)
        flexible_row.addWidget(QLabel("–"))
        flexible_row.addWidget(self.allowed_max)
        flexible_row.addStretch(1)

        count_group = QGroupBox("目标集数")
        count_layout = QVBoxLayout(count_group)
        count_layout.addWidget(QLabel("目标集数 N："))
        count_layout.addWidget(self.count_spin)
        count_layout.addWidget(self.policy_exact)
        count_layout.addWidget(self.policy_flexible)
        count_layout.addLayout(flexible_row)

        # 范围来源
        self.range_manual = QRadioButton("手动上下限")
        self.range_percent = QRadioButton("按目标时长百分比浮动")
        self.range_percent.setChecked(True)
        self.min_spin = QDoubleSpinBox()
        self.min_spin.setRange(0.1, 3600.0)
        self.min_spin.setDecimals(3)
        self.min_spin.setValue(96.0)
        self.max_spin = QDoubleSpinBox()
        self.max_spin.setRange(0.1, 3600.0)
        self.max_spin.setDecimals(3)
        self.max_spin.setValue(144.0)
        self.tolerance_spin = QDoubleSpinBox()
        self.tolerance_spin.setRange(0.0, 99.0)
        self.tolerance_spin.setDecimals(2)
        self.tolerance_spin.setValue(20.0)
        self.tolerance_spin.setSuffix(" %")
        for widget in (self.range_manual, self.range_percent):
            widget.toggled.connect(self._refresh)
        for widget in (self.min_spin, self.max_spin, self.tolerance_spin):
            widget.valueChanged.connect(self._refresh)

        range_group = QGroupBox("集长范围（两种来源互斥）")
        range_layout = QFormLayout(range_group)
        range_layout.addRow(self.range_percent)
        range_layout.addRow("浮动比例 p：", self.tolerance_spin)
        range_layout.addRow(self.range_manual)
        range_layout.addRow("最短集长 L：", self.min_spin)
        range_layout.addRow("最长集长 U：", self.max_spin)

        # 策略
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItem("剧情优先", Strategy.STORY.value)
        self.strategy_combo.addItem("悬念优先", Strategy.SUSPENSE.value)
        self.strategy_combo.addItem("时长优先", Strategy.DURATION.value)
        self.allow_exception = QCheckBox("允许单集例外（默认关闭，启用后须逐集记录原因）")

        strategy_group = QGroupBox("策略与例外")
        strategy_layout = QVBoxLayout(strategy_group)
        strategy_layout.addWidget(self.strategy_combo)
        strategy_layout.addWidget(self.allow_exception)

        # 即时可行性
        self.feasibility = QTextEdit()
        self.feasibility.setReadOnly(True)
        self.feasibility.setMinimumHeight(150)

        left = QVBoxLayout()
        left.addWidget(mode_group)
        left.addWidget(count_group)
        left.addWidget(range_group)
        left.addWidget(strategy_group)
        left.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(QLabel("即时可行性检查（调用 AI 之前就应给出结论）"))
        right.addWidget(self.feasibility, 1)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 1)
        layout.addLayout(right, 1)

    # ---- 读取当前界面配置 ----------------------------------------------

    def collect_settings(self):
        settings = self.state.settings

        if self.radio_by_duration.isChecked():
            settings.split_mode = SplitMode.TARGET_DURATION
        elif self.radio_by_count.isChecked():
            settings.split_mode = SplitMode.TARGET_EPISODE_COUNT
        else:
            settings.split_mode = SplitMode.RECOMMEND

        settings.target_duration_seconds = Fraction(str(self.duration_spin.value()))
        settings.target_episode_count = self.count_spin.value()
        settings.count_policy = (
            CountPolicy.EXACT if self.policy_exact.isChecked() else CountPolicy.FLEXIBLE
        )
        settings.allowed_count_min = self.allowed_min.value()
        settings.allowed_count_max = self.allowed_max.value()

        spec = settings.range
        if self.range_manual.isChecked():
            spec.switch_to(RangeMode.MANUAL)
            spec.manual_min = Fraction(str(self.min_spin.value()))
            spec.manual_max = Fraction(str(self.max_spin.value()))
        else:
            spec.switch_to(RangeMode.PERCENT)
            spec.tolerance = Fraction(str(self.tolerance_spin.value())) / 100

        settings.strategy = Strategy(self.strategy_combo.currentData())
        settings.allow_episode_exceptions = self.allow_exception.isChecked()
        return settings

    def refresh_from_state(self) -> None:
        """进入设置页时重算可行性提示。

        设置项的编辑入口只在本页，因此不需要把状态反向灌回控件；
        这里只负责刷新派生信息与控件的可用状态，避免出现
        "界面上显示的参数"与"实际参与计算的参数"两份副本。
        """
        self._refresh()

    def _refresh(self) -> None:
        settings = self.collect_settings()

        by_count = settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
        self.duration_spin.setEnabled(not by_count)
        self.count_spin.setEnabled(by_count)
        self.policy_exact.setEnabled(by_count)
        self.policy_flexible.setEnabled(by_count)
        self.allowed_min.setEnabled(by_count and settings.count_policy == CountPolicy.FLEXIBLE)
        self.allowed_max.setEnabled(by_count and settings.count_policy == CountPolicy.FLEXIBLE)
        manual = self.range_manual.isChecked()
        self.min_spin.setEnabled(manual)
        self.max_spin.setEnabled(manual)
        self.tolerance_spin.setEnabled(not manual)

        lines: list[str] = []
        total = self.state.total_seconds
        if total is None:
            lines.append("尚未导入源片，无法检查可行性。")
            self.feasibility.setPlainText("\n".join(lines))
            return

        problems = settings.validate()
        if problems:
            lines.append("【参数不完整】")
            lines.extend(f"  · {text}" for text in problems)
            self.feasibility.setPlainText("\n".join(lines))
            return

        report = check_feasibility(settings, total)

        lines.append(f"源片总时长：{format_timecode(total)}")
        if report.derived is not None:
            lines.append(
                f"目标时长 D：{format_timecode(report.derived.target_duration)}"
                "（内部全精度，界面显示才取整）"
            )
            lines.append(
                f"集长范围：{format_seconds_brief(report.derived.min_duration)}"
                f" – {format_seconds_brief(report.derived.max_duration)}"
            )
        # min > max 时不能照原样打印区间，那对用户毫无意义
        if report.min_episodes > report.max_episodes:
            lines.append("可行集数范围：无（不存在任何集数能满足当前集长范围）")
        else:
            lines.append(
                f"可行集数范围：{report.min_episodes} – {report.max_episodes} 集"
                f"（最少 = ⌈T/U⌉，最多 = ⌊T/L⌋）"
            )
        if (
            settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
            and settings.count_policy == CountPolicy.FLEXIBLE
        ):
            if report.allowed_min > report.allowed_max:
                lines.append("与用户允许区间取交集后：无交集")
            else:
                lines.append(
                    f"与用户允许区间取交集后：{report.allowed_min} – {report.allowed_max} 集"
                )

        if report.issues:
            lines.append("")
            for issue in report.issues:
                tag = "阻断" if issue.level == "block" else ("警告" if issue.level == "warn" else "提示")
                lines.append(f"[{tag}] {issue.message}")
                if issue.hint:
                    lines.append(f"        {issue.hint}")

        lines.append("")
        if report.blocking:
            lines.append("结论：当前参数无解，请先调整参数，不要进入分析。")
        else:
            lines.append("结论：参数可行，可以开始分析。")

        self.feasibility.setPlainText("\n".join(lines))

    def can_continue(self) -> tuple[bool, str]:
        settings = self.collect_settings()
        problems = settings.validate()
        if problems:
            return False, problems[0]
        if self.state.total_seconds is None:
            return False, "尚未导入源片。"
        report = check_feasibility(settings, self.state.total_seconds)
        if report.blocking:
            first = report.issues[0]
            return False, f"{first.message} {first.hint}".strip()
        return True, ""


# ---------------------------------------------------------------------------
# 页3 分析
# ---------------------------------------------------------------------------


class AnalysisPage(QWidget):
    STAGES = [
        "媒体探测",
        "音频与字幕",
        "镜头分析",
        "剧情分析",
        "整体规划",
        "边界审核",
        "导出",
    ]

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._worker: PlanWorker | None = None

        self.stage_list = QListWidget()
        self.stage_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        self.btn_start = QPushButton("开始分析")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)

        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_start)
        buttons.addWidget(self.btn_cancel)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("阶段与进度"))
        layout.addWidget(self.stage_list, 0)
        layout.addWidget(self.progress)
        layout.addWidget(self.log, 1)
        layout.addLayout(buttons)

        self._render_stages()

    def _render_stages(self, current: int = -1, done: int = 0) -> None:
        self.stage_list.clear()
        for index, name in enumerate(self.STAGES):
            if index < done:
                mark = "✔"
            elif index == current:
                mark = "▶"
            else:
                mark = "·"
            item = QListWidgetItem(f"{mark}  {index + 1}. {name}")
            self.stage_list.addItem(item)

    def _append(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {text}")

    def _start(self) -> None:
        if self.state.media is None:
            QMessageBox.warning(self, "尚未导入", "请先在导入页选择源视频。")
            return
        settings = self.state.settings
        problems = settings.validate()
        if problems:
            QMessageBox.warning(self, "参数不完整", problems[0])
            return

        self.log.clear()
        self.progress.setValue(0)
        self._render_stages(current=4, done=1)
        self._append("阶段1 媒体探测：已完成（导入时执行）。")
        self._append("阶段2 音频与字幕：当前版本尚未接入 ASR，跳过。")
        self._append("阶段3 镜头分析：当前版本尚未接入镜头检测，跳过。")
        self._append("阶段4 剧情分析：当前版本尚未接入模型接口，跳过。")
        self._append("阶段5 整体规划：使用规则规划器生成草案并校验约束…")

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self._worker = PlanWorker(self.state.media, settings, self)
        self._worker.completed.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._append("已取消。")
        self.progress.setValue(0)

    def _on_done(self, plan: BoundaryPlan, report, problems) -> None:
        self.state.add_plan(plan)
        self.progress.setValue(100)
        self._render_stages(done=5)

        durations = [float(d) for d in plan.durations()]
        self._append(
            f"方案 v{plan.version}：{plan.episode_count} 集，"
            f"时长 {min(durations):.2f}s – {max(durations):.2f}s"
        )
        self._append("覆盖校验：" + ("通过（b0=0、bN=T、严格递增）" if plan.coverage_valid() else "未通过"))
        snapping = plan.is_frame_aligned(self.state.media)
        self._append(
            "帧对齐：" + ("全部落在合法帧起点" if snapping[0] else f"有 {len(snapping[1])} 处未对齐")
        )
        self._append(
            "语义审核状态：规则草案，尚未经过剧情判断（阶段4 未接入）。"
        )
        if problems:
            for problem in problems:
                self._append(f"  方案层问题：{problem.describe()}")
        else:
            self._append("方案层校验：无问题。")

        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _on_failed(self, message: str) -> None:
        self._append(f"失败：{message}")
        self.progress.setValue(0)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        QMessageBox.warning(self, "无法生成方案", message)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.current_plan is None:
            return False, "请先在分析页生成方案。"
        return True, ""


# ---------------------------------------------------------------------------
# 页4 审核
# ---------------------------------------------------------------------------


class ReviewPage(QWidget):
    """审核与手动微调（§13.2、§13.3）。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._decoder = FrameDecoder()
        self._player = StreamPlayer()
        self._position = Fraction(0)
        self._current_episode = 1
        self._stitch_queue: list[tuple[Path, float, float]] = []

        self.episode_table = QTableWidget(0, 5)
        self.episode_table.setHorizontalHeaderLabels(
            ["集号", "区间", "时长", "切点", "审核"]
        )
        header = self.episode_table.horizontalHeader()
        # 全部按内容自适应：区间列被截断会丢掉"结束时间"这个关键信息
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.episode_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.episode_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.episode_table.setMinimumWidth(480)
        self.episode_table.itemSelectionChanged.connect(self._episode_selected)

        self.preview = PreviewPanel()

        self.card = QTextEdit()
        self.card.setReadOnly(True)
        self.card.setMinimumHeight(120)

        self.problem_box = QTextEdit()
        self.problem_box.setReadOnly(True)
        self.problem_box.setMinimumHeight(80)

        self.btn_set_boundary = QPushButton("把当前位置设为该集结束切点")
        self.btn_lock = QPushButton("锁定该切点")
        self.btn_unlock = QPushButton("解除锁定")
        self.btn_confirm = QPushButton("确认该集")
        self.btn_snap = QPushButton("吸附到最近帧")
        self.btn_set_boundary.clicked.connect(self._set_boundary)
        self.btn_lock.clicked.connect(lambda: self._lock(True))
        self.btn_unlock.clicked.connect(lambda: self._lock(False))
        self.btn_confirm.clicked.connect(self._confirm_episode)
        self.btn_snap.clicked.connect(lambda: self._goto(self._position))

        editing = QHBoxLayout()
        for button in (
            self.btn_set_boundary,
            self.btn_lock,
            self.btn_unlock,
            self.btn_confirm,
            self.btn_snap,
        ):
            editing.addWidget(button)
        editing.addStretch(1)

        left = QVBoxLayout()
        left.addWidget(QLabel("集列表"))
        left.addWidget(self.episode_table, 1)

        right = QVBoxLayout()
        right.addWidget(self.preview, 2)
        right.addWidget(QLabel("当前集"))
        right.addWidget(self.card, 1)
        right.addWidget(QLabel("方案层校验（硬约束问题会阻止导出）"))
        right.addWidget(self.problem_box, 1)
        right.addLayout(editing)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_widget = QWidget()
        left_widget.setLayout(left)
        right_widget = QWidget()
        right_widget.setLayout(right)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([620, 820])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        # 播放控制接线
        self.preview.btn_play.clicked.connect(self._play_episode)
        self.preview.btn_boundary.clicked.connect(self._play_boundary)
        self.preview.btn_stitch.clicked.connect(self._play_stitched)
        self.preview.btn_stop.clicked.connect(self._player.stop)
        self.preview.btn_prev_frame.clicked.connect(lambda: self._step_frames(-1))
        self.preview.btn_next_frame.clicked.connect(lambda: self._step_frames(1))
        self.preview.btn_back_1s.clicked.connect(lambda: self._step_seconds(-1.0))
        self.preview.btn_fwd_1s.clicked.connect(lambda: self._step_seconds(1.0))
        self.preview.btn_back_5s.clicked.connect(lambda: self._step_seconds(-5.0))
        self.preview.btn_fwd_5s.clicked.connect(lambda: self._step_seconds(5.0))

        self._player.frame_ready.connect(self.preview.set_image)
        self._player.position_changed.connect(self._on_position)
        self._player.playback_finished.connect(self._on_playback_finished)
        self._player.error.connect(lambda message: QMessageBox.warning(self, "播放失败", message))

    # ---- 刷新 ----------------------------------------------------------

    def refresh(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None:
            self.episode_table.setRowCount(0)
            self.preview.clear()
            return

        self._decoder.set_source(media.path)
        self._player.set_source(media.path)
        self._player.set_fps(media.video.nominal_fps if media.video else Fraction(25))

        episodes = plan.episodes()
        self.episode_table.setRowCount(len(episodes))
        for row, episode in enumerate(episodes):
            status = (
                "例外"
                if episode.index in plan.exceptions
                else ("已锁定" if episode.end_ticks in plan.locked_ticks else "未锁定")
            )
            review_text = {
                "pending": "未审核",
                "confirmed": "已确认",
            }.get(plan.semantic_review_status, "草案未审核")
            cells = [
                f"第{episode.index:02d}集",
                f"{format_timecode(episode.start_seconds)} → {format_timecode(episode.end_seconds)}",
                format_seconds_brief(episode.duration_seconds),
                status,
                review_text,
            ]
            for column, text in enumerate(cells):
                self.episode_table.setItem(row, column, QTableWidgetItem(text))

        if episodes:
            row = min(self._current_episode - 1, len(episodes) - 1)
            self.episode_table.selectRow(row)

        self._refresh_problems()
        self._goto(episodes[0].start_seconds)

    def _refresh_problems(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            self.problem_box.setPlainText("")
            return
        problems = plan.validate(self.state.settings, None)
        if not problems:
            self.problem_box.setPlainText("方案层校验：无问题。")
            return
        lines = []
        for problem in problems:
            tag = "阻断" if problem.is_blocking else "警告"
            lines.append(f"[{tag}] {problem.describe()}")
        self.problem_box.setPlainText("\n".join(lines))

    # ---- 导航与定位 ----------------------------------------------------

    def _episode_selected(self) -> None:
        rows = self.episode_table.selectionModel().selectedRows()
        if not rows:
            return
        self._current_episode = rows[0].row() + 1
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if 1 <= self._current_episode <= len(episodes):
            episode = episodes[self._current_episode - 1]
            self._goto(episode.start_seconds)

    def _goto(self, seconds) -> None:
        media = self.state.media
        if media is None:
            return
        seconds = Fraction(seconds)
        seconds = max(Fraction(0), min(seconds, media.timeline_duration))
        # §13.3 落点必须吸附到合法源帧边界，并显示实际时间
        _, snapped = media.snap_to_frame(seconds)
        self._position = snapped
        self._player.set_range(float(snapped))
        # 静态定位时也要刷新时间码，不能只在播放回调里更新
        self.preview.timecode.setText(
            f"{format_timecode(self._position)}　（{float(self._position):.3f}s）"
        )
        try:
            image = self._decoder.frame_at(snapped)
        except DecodeError as exc:
            self.preview.clear(f"解码失败：{exc}")
            return
        if image is not None:
            self.preview.set_image(image)
        self._update_card()

    def _on_position(self, seconds: float) -> None:
        self._position = Fraction(seconds).limit_denominator(100000)
        self.preview.timecode.setText(
            f"{format_timecode(self._position)}　（{float(self._position):.3f}s）"
        )

    # ---- 播放 ----------------------------------------------------------

    def _play_episode(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if not 1 <= self._current_episode <= len(episodes):
            return
        episode = episodes[self._current_episode - 1]
        self._player.set_range(float(episode.start_seconds), float(episode.end_seconds))
        self._player.play()

    def _play_boundary(self) -> None:
        """连看切点前后各 3 秒（§13.2 必须能连看，不能只看一侧）。"""
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if not 1 <= self._current_episode <= len(episodes):
            return
        boundary = episodes[self._current_episode - 1].end_seconds
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集终点即片尾，没有后续集可连看。")
            return
        start = max(Fraction(0), boundary - 3)
        self._player.set_range(float(start), float(boundary + 3))
        self._player.play()

    def _play_stitched(self) -> None:
        """用**已导出的成片**连看上一集结尾与下一集开头。

        与"连看源片切点前后"不同，这一步能发现编码层面的问题
        （§13.2 检查同一动作、对白和声画衔接）。
        """
        plan = self.state.current_plan
        if plan is None:
            return
        episodes = plan.episodes()
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集没有后续集。")
            return
        current, following = episodes[self._current_episode - 1], episodes[self._current_episode]

        first = self.state.exported.get(current.index)
        second = self.state.exported.get(following.index)
        if not first or not second:
            QMessageBox.information(
                self,
                "尚无成片",
                "成片连看需要相邻两集都已导出。请先完成导出，或使用「播放切点前后」检视规划。",
            )
            return

        self._stitch_queue = [
            (first[0], float(current.end_seconds) - 3.0, float(current.end_seconds)),
            (second[0], 0.0, 3.0),
        ]
        self._play_next_stitch()

    def _play_next_stitch(self) -> None:
        if not self._stitch_queue:
            return
        path, start, end = self._stitch_queue.pop(0)
        self._player.set_source(path)
        self._player.set_range(max(0.0, start), end)
        self._player.play()

    def _on_playback_finished(self) -> None:
        if self._stitch_queue:
            self._play_next_stitch()

    # ---- 微调 ----------------------------------------------------------

    def _step_frames(self, delta: int) -> None:
        media = self.state.media
        if media is None:
            return
        index = media.frame_index_at(self._position) + delta
        index = max(0, index)
        self._goto(media.frame_start_seconds(index))

    def _step_seconds(self, delta: float) -> None:
        self._goto(self._position + Fraction(str(delta)))

    def _set_boundary(self) -> None:
        plan = self.state.current_plan
        media = self.state.media
        if plan is None or media is None:
            return
        episodes = plan.episodes()
        if self._current_episode >= len(episodes):
            QMessageBox.information(self, "末集", "末集终点为片尾，不可移动。")
            return

        index = self._current_episode  # 边界下标与集号一致：第 i 集与第 i+1 集共享
        ticks = plan.time_base.seconds_to_ticks(self._position)
        problems = plan.move_boundary(index, ticks)
        blocking = [p for p in problems if p.is_blocking]

        self._refresh_problems()
        if blocking:
            QMessageBox.warning(
                self,
                "该切点不可提交",
                "；".join(p.describe() for p in blocking) + "\n\n方案未被修改。",
            )
        else:
            self.refresh()
            self.episode_table.selectRow(self._current_episode - 1)

    def _lock(self, locked: bool) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        index = self._current_episode
        plan.lock_boundary(index, locked)
        self.refresh()
        self.episode_table.selectRow(max(0, self._current_episode - 1))

    def _confirm_episode(self) -> None:
        plan = self.state.current_plan
        if plan is None:
            return
        plan.semantic_review_status = "confirmed"
        self.refresh()
        self.episode_table.selectRow(max(0, self._current_episode - 1))

    def _update_card(self) -> None:
        """§13.2 审核卡片：必须写清推荐依据、风险与审核状态。

        当前版本没有模型判断，因此不编造"推荐等级"，只如实报告时长与约束状态。
        """
        plan = self.state.current_plan
        media = self.state.media
        episodes = plan.episodes() if plan else []
        if not episodes or not 1 <= self._current_episode <= len(episodes):
            self.card.setPlainText("")
            return
        episode = episodes[self._current_episode - 1]
        start = float(episode.start_seconds)
        index = media.frame_index_at(episode.start_seconds) if media else 0
        lines = [
            f"第{episode.index:02d}集｜{format_timecode(episode.start_seconds)}—"
            f"{format_timecode(episode.end_seconds)}｜"
            f"{format_seconds_brief(episode.duration_seconds)}",
            f"当前位置：{format_timecode(self._position)}",
            f"首帧号：{index}　帧数：{plan.frame_counts(media)[episode.index - 1] if plan and media else '—'}",
            "",
            "推荐等级：未评估（本版尚未接入剧情判断，不给出无法验证的推荐分）",
            "支持证据：仅有时长与帧边界合规；对白与动作落点未评估。",
            "风险：对白截断、动作未完成、悬念落点均未评估。",
            f"审核状态：{plan.semantic_review_status}",
            f"锁定状态：{'已锁定' if episode.end_ticks in plan.locked_ticks else '未锁定'}",
            "",
            f"起点相对秒：{start:.6f}",
        ]
        self.card.setPlainText("\n".join(lines))

    def can_continue(self) -> tuple[bool, str]:
        plan = self.state.current_plan
        if plan is None:
            return False, "尚无方案。"
        blocking = [p for p in plan.validate() if p.is_blocking]
        if blocking:
            return False, "方案存在未解决的硬约束问题：" + blocking[0].describe()
        return True, ""


# ---------------------------------------------------------------------------
# 页5 导出
# ---------------------------------------------------------------------------


class ExportPage(QWidget):
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

        preset_group = QGroupBox("编码预设（精确裁切，重新编码）")
        preset_form = QFormLayout(preset_group)
        preset_form.addRow("容器 / 视频编码：", QLabel("MP4 / H.264（libx264）"))
        preset_form.addRow("CRF：", self.crf_spin)
        preset_form.addRow("编码速度：", self.preset_combo)
        preset_form.addRow("音频：", self.audio_bitrate_combo)

        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.btn_output = QPushButton("选择输出目录…")
        self.btn_output.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_output)

        self.estimate = QLabel("—")

        self.queue = QTableWidget(0, 5)
        self.queue.setHorizontalHeaderLabels(["集号", "状态", "帧数校验", "时长", "说明"])
        self.queue.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        self.btn_start = QPushButton("开始导出")
        self.btn_retry = QPushButton("重试失败项")
        self.btn_cancel = QPushButton("取消")
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(lambda: self._start(None))
        self.btn_retry.clicked.connect(self._retry)
        self.btn_cancel.clicked.connect(self._cancel)

        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_start)
        buttons.addWidget(self.btn_retry)
        buttons.addWidget(self.btn_cancel)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(preset_group)
        layout.addWidget(QLabel("输出目录"))
        layout.addLayout(output_row)
        layout.addWidget(self.estimate)
        layout.addWidget(QLabel("导出队列"))
        layout.addWidget(self.queue, 1)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)

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
        # 粗略估算：按源片码率上浮 10%，实际取决于画面复杂度（CRF 编码无法精确预估）
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

        blocking = [p for p in plan.validate() if p.is_blocking]
        if blocking:
            QMessageBox.warning(
                self,
                "方案不可导出",
                "存在硬约束问题：\n" + "\n".join(p.describe() for p in blocking),
            )
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
            parent=self,
        )
        self._worker.episode_done.connect(self._on_episode_done)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

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
        QMessageBox.information(self, "导出结束", text)

    def _on_failed(self, message: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        QMessageBox.critical(self, "导出失败", message)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.output_root is None:
            return False, "请先选择输出目录。"
        return True, ""


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    PAGES = ["导入", "设置", "分析", "审核", "导出"]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 短剧智能分集器")
        self.resize(1440, 900)

        self.state = ProjectState()

        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        for index, name in enumerate(self.PAGES):
            self.nav.addItem(f"{index + 1}. {name}")
        self.nav.currentRowChanged.connect(self._nav_changed)

        self.stack = QStackedWidget()
        self.import_page = ImportPage(self.state)
        self.settings_page = SettingsPage(self.state)
        self.analysis_page = AnalysisPage(self.state)
        self.review_page = ReviewPage(self.state)
        self.export_page = ExportPage(self.state)
        for page in (
            self.import_page,
            self.settings_page,
            self.analysis_page,
            self.review_page,
            self.export_page,
        ):
            self.stack.addWidget(page)

        self.btn_prev = QPushButton("← 上一步")
        self.btn_next = QPushButton("下一步 →")
        self.btn_prev.clicked.connect(lambda: self._navigate(-1))
        self.btn_next.clicked.connect(lambda: self._navigate(1))
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#9a6700;")

        footer = QHBoxLayout()
        footer.addWidget(self.hint, 1)
        footer.addWidget(self.btn_prev)
        footer.addWidget(self.btn_next)

        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)

        layout = QVBoxLayout()
        layout.addLayout(body, 1)
        layout.addLayout(footer)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.nav.setCurrentRow(0)

    def _nav_changed(self, index: int) -> None:
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        self.btn_prev.setEnabled(index > 0)
        self.btn_next.setEnabled(index < len(self.PAGES) - 1)
        self._on_page_shown(index)

    def _on_page_shown(self, index: int) -> None:
        self.hint.setText("")
        if index == 1:
            self.settings_page.refresh_from_state()
        elif index == 3:
            self.review_page.refresh()
        elif index == 4:
            self.export_page.refresh()

    def _current_page(self):
        return self.stack.currentWidget()

    def _navigate(self, delta: int) -> None:
        index = self.stack.currentIndex()
        if delta > 0:
            ok, message = self._current_page().can_continue()
            if not ok:
                self.hint.setText(message)
                return
        target = max(0, min(len(self.PAGES) - 1, index + delta))
        self.nav.setCurrentRow(target)
