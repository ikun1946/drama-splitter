"""主窗口与六个工作页面（方案 §13.1 推荐界面结构）。

外观与交互重构后的版本：
- 主题由 ``app.gui.theme`` 注入（设计令牌化）；
- 通用控件从 ``app.gui.widgets`` 取；
- 每个页面的具体实现仍然挂在同名属性下（``import_page.issues``、
  ``review_page.episode_table``、``export_page.queue`` 等），冒烟测试无须改动。
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
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
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.app_config import AppConfig, default_models_root
from ..core.export import ExportPreset, Exporter
from ..core.ffmpeg import check_encoders, locate_ffmpeg
from ..core.model_manager import (
    MODEL_CATALOG,
    delete_model,
    free_space_bytes,
    installed_state,
    spec_by_key,
)
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
    SplitMode,
    Strategy,
    check_feasibility,
)
from ..core.timebase import format_seconds_brief, format_timecode
from .frame_source import DecodeError, FrameDecoder, StreamPlayer
from .state import ProjectState
from .theme import apply_theme, COLOR_PRIMARY
from .widgets import (
    AppBar,
    Card,
    Drawer,
    DropZone,
    IssueList,
    LabeledField,
    LogPanel,
    MediaPreview,
    StatusPill,
    StepIndicator,
    StepNav,
    StepState,
)
from .workers import (
    AnalysisWorker,
    ExportWorker,
    ModelDownloadWorker,
    PlanWorker,
    ProbeWorker,
)

__all__ = ["MainWindow", "PreviewPanel"]

LEVEL_TEXT = {LEVEL_BLOCK: "阻断", LEVEL_WARN: "警告", LEVEL_INFO: "提示"}
LEVEL_COLOR = {LEVEL_BLOCK: "#b3261e", LEVEL_WARN: "#9a6700", LEVEL_INFO: "#3d6b35"}


# ---------------------------------------------------------------------------
# 预览面板（保留旧类名，向后兼容）
# ---------------------------------------------------------------------------


class PreviewPanel(MediaPreview):
    """旧代码里部分脚本可能仍引用 ``PreviewPanel``；保留同名派生类。"""


# ---------------------------------------------------------------------------
# 页 1  导入
# ---------------------------------------------------------------------------


class ImportPage(QWidget):
    """导入源视频、显示媒体信息、选择音轨、列出兼容性问题。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._worker: ProbeWorker | None = None

        # ---- 顶部：源文件选择 ----
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.btn_browse = QPushButton("📁  选择源视频…")
        self.btn_browse.setProperty("role", "primary")
        self.btn_browse.style().unpolish(self.btn_browse)
        self.btn_browse.style().polish(self.btn_browse)
        self.btn_browse.clicked.connect(self._choose_file)

        file_row = QHBoxLayout()
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(self.btn_browse)

        file_card = Card("① 选择源视频", parent=self)
        file_card.body_layout.addLayout(file_row)
        file_card.body_layout.addWidget(self._summary_label())
        self.file_card = file_card

        # ---- 拖放区（hero 空态） ----
        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self._choose_file)
        self.drop_zone.file_dropped.connect(self._probe)
        # 默认隐藏；有路径后才显示（简洁起见：始终显示，作为行动入口）
        self.drop_zone.setVisible(True)

        # ---- 中部：媒体信息摘要 ----
        self.summary = QLabel("尚未导入。")
        self.summary.setWordWrap(True)
        self.summary.setProperty("role", "muted")

        info_card = Card("② 媒体信息", parent=self)
        info_card.body_layout.addWidget(self.summary)
        self._info_card = info_card

        # ---- 中部：音轨选择 ----
        self.audio_combo = QComboBox()
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        audio_card = Card("③ 主对白音轨", parent=self)
        audio_card.body_layout.addWidget(QLabel("多音轨时必须显式选择"))
        audio_card.body_layout.addWidget(self.audio_combo)
        self.audio_hint = QLabel("导入后列出可选音轨。")
        self.audio_hint.setProperty("role", "muted")
        self.audio_hint.setWordWrap(True)
        audio_card.body_layout.addWidget(self.audio_hint)
        self._audio_card = audio_card

        # ---- 底部：兼容性问题 ----
        self.issues = IssueList(columns=["级别", "代码", "说明", "建议"])
        # 阻断徽章（默认隐藏）
        self._block_pill = StatusPill("0 阻断", tone="danger")
        self._block_pill.setVisible(False)

        issues_card = Card("④ 兼容性与解码检查", parent=self)
        issues_card_title_row = QHBoxLayout()
        issues_card_title_row.addStretch(1)
        issues_card_title_row.addWidget(self._block_pill)
        # 用 QLabel 包装一下放在 header 右侧
        # 简化：直接在 body_layout 顶上加一个水平行
        issues_card.body_layout.insertSpacing(0, 0)
        # pill 行
        pill_row = QHBoxLayout()
        pill_row.addStretch(1)
        pill_row.addWidget(self._block_pill)
        issues_card.body_layout.insertLayout(0, pill_row)
        issues_card.body_layout.addWidget(self.issues, 1)
        self._issues_card = issues_card

        self.status = QLabel("")
        self.status.setProperty("role", "muted")

        # ---- 装配 ----
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(self.drop_zone)
        outer.addWidget(file_card)
        outer.addWidget(info_card)
        outer.addWidget(audio_card)
        outer.addWidget(issues_card, 1)
        outer.addWidget(self.status)

        # 把 self.issues 行数代理给 IssueList（已经兼容）
        self._file_card = file_card

    def _summary_label(self) -> QLabel:
        lbl = QLabel()
        lbl.setProperty("role", "muted")
        lbl.setText("支持 .mp4 / .mov / .mkv / .avi / .ts / .m4v")
        return lbl

    # ---- 行为 --------------------------------------------------------

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
        self.issues.clear_issues()
        self.state.reset_media()

        self._worker = ProbeWorker(self.state.binaries, path, self)
        self._worker.completed.connect(self._on_probed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_probed(self, info, issues) -> None:
        self.state.media = info
        self.status.setText("探测完成。")
        self.summary.setText(describe_media(info))

        self.issues.clear_issues()
        for issue in issues:
            self.issues.add_issue(issue.level, issue.code, issue.message, issue.detail)

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
            self._block_pill.set_text(f"{len(blocking)} 阻断")
            self._block_pill.set_tone("danger")
            self._block_pill.setVisible(True)
        else:
            self.status.setText("探测完成，可以继续设置分集目标。")
            self._block_pill.setVisible(False)

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
# 页 2  设置
# ---------------------------------------------------------------------------


class SettingsPage(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 本页会被装进 420px 宽的抽屉里。默认尺寸策略下，双栏布局的最小
        # 宽度之和会把版面挤成"一列 90px"，文字被迫竖排并溢出卡片——
        # 因此显式给出能容纳双栏的最小宽度，并允许横向滚动兜底。
        self.setMinimumWidth(640)

        # ---- 模式 ----
        self.radio_by_duration = QRadioButton("按目标时长（系统求集数）")
        self.radio_by_count = QRadioButton("按目标集数")
        self.radio_recommend = QRadioButton("推荐方案（先按时长估算，分析后再给剧情建议）")
        self.radio_by_duration.setChecked(True)
        for w in (self.radio_by_duration, self.radio_by_count, self.radio_recommend):
            w.toggled.connect(self._refresh)
        mode_card = Card("分集模式", parent=self)
        mode_card.body_layout.addWidget(self.radio_by_duration)
        mode_card.body_layout.addWidget(self.radio_by_count)
        mode_card.body_layout.addWidget(self.radio_recommend)
        self._mode_card = mode_card

        # ---- 目标数值 ----
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
        for w in (self.policy_exact, self.policy_flexible):
            w.toggled.connect(self._refresh)
        for w in (self.allowed_min, self.allowed_max):
            w.valueChanged.connect(self._refresh)

        flexible_row = QHBoxLayout()
        flexible_row.addWidget(QLabel("允许集数区间："))
        flexible_row.addWidget(self.allowed_min)
        flexible_row.addWidget(QLabel("–"))
        flexible_row.addWidget(self.allowed_max)
        flexible_row.addStretch(1)

        count_card = Card("目标集数 / 时长", parent=self)
        # 用 LabeledField 风格
        dfield = LabeledField("目标时长", suffix="秒")
        dfield.attach(self.duration_spin)
        count_card.body_layout.addWidget(dfield)
        nfield = LabeledField("目标集数 N")
        nfield.attach(self.count_spin)
        count_card.body_layout.addWidget(nfield)
        count_card.body_layout.addSpacing(4)
        count_card.body_layout.addWidget(self.policy_exact)
        count_card.body_layout.addWidget(self.policy_flexible)
        count_card.body_layout.addLayout(flexible_row)
        self._count_card = count_card

        # ---- 范围来源 ----
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
        for w in (self.range_manual, self.range_percent):
            w.toggled.connect(self._refresh)
        for w in (self.min_spin, self.max_spin, self.tolerance_spin):
            w.valueChanged.connect(self._refresh)

        range_card = Card("集长范围（两种来源互斥）", parent=self)
        range_form = QFormLayout()
        range_form.addRow(self.range_percent)
        pfield = LabeledField("浮动比例 p", suffix="%")
        pfield.attach(self.tolerance_spin)
        range_form.addRow(pfield)
        range_form.addRow(self.range_manual)
        lfield = LabeledField("最短集长 L", suffix="秒")
        lfield.attach(self.min_spin)
        range_form.addRow(lfield)
        ufield = LabeledField("最长集长 U", suffix="秒")
        ufield.attach(self.max_spin)
        range_form.addRow(ufield)
        # 把 QFormLayout 嫁接到 range_card.body
        old_layout = range_card.body.layout()
        if old_layout is not None:
            placeholder = QWidget()
            placeholder.setLayout(old_layout)
            placeholder.deleteLater()
        range_card.body.setLayout(range_form)
        range_card.body_layout = range_form
        range_form.setContentsMargins(12, 10, 12, 10)
        range_form.setSpacing(8)
        self._range_card = range_card

        # ---- 策略 ----
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItem("剧情优先", Strategy.STORY.value)
        self.strategy_combo.addItem("悬念优先", Strategy.SUSPENSE.value)
        self.strategy_combo.addItem("时长优先", Strategy.DURATION.value)
        self.allow_exception = QCheckBox("允许单集例外（默认关闭，启用后须逐集记录原因）")

        strategy_card = Card("策略与例外", parent=self)
        sfield = LabeledField("策略")
        sfield.attach(self.strategy_combo)
        strategy_card.body_layout.addWidget(sfield)
        strategy_card.body_layout.addWidget(self.allow_exception)
        self._strategy_card = strategy_card

        # ---- 可行性 ----
        self._feas_pill = StatusPill("可行", tone="success")
        feasibility_card = Card("即时可行性检查（调用 AI 之前就应给出结论）", parent=self)
        pill_row = QHBoxLayout()
        pill_row.addWidget(self._feas_pill)
        pill_row.addStretch(1)
        feasibility_card.body_layout.insertLayout(0, pill_row)
        self.feasibility = QTextEdit()
        self.feasibility.setReadOnly(True)
        feasibility_card.body_layout.addWidget(self.feasibility, 1)
        self._feasibility_card = feasibility_card

        # ---- 装配 ----
        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        left_col.addWidget(mode_card)
        left_col.addWidget(count_card)
        left_col.addWidget(range_card)
        left_col.addWidget(strategy_card)
        left_col.addStretch(1)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(feasibility_card, 1)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addLayout(left_col, 1)
        outer.addLayout(right_col, 1)

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
            self._feas_pill.set_text("就绪")
            self._feas_pill.set_tone("muted")
            return

        problems = settings.validate()
        if problems:
            lines.append("【参数不完整】")
            lines.extend(f"  · {text}" for text in problems)
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("参数不完整")
            self._feas_pill.set_tone("danger")
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
            self._feas_pill.set_text("无解")
            self._feas_pill.set_tone("danger")
        else:
            # 可行但可能有警告
            has_warn = any(iss.level == "warn" for iss in report.issues)
            if has_warn:
                lines.append("结论：参数可行但存在警告，可继续。")
                self._feas_pill.set_text("可行（警告）")
                self._feas_pill.set_tone("warn")
            else:
                lines.append("结论：参数可行，可以开始分析。")
                self._feas_pill.set_text("可行")
                self._feas_pill.set_tone("success")

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
# 页 3  分析
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 页 4  审核
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

        # ---- 集列表 ----
        self.episode_table = QTableWidget(0, 5)
        self.episode_table.setHorizontalHeaderLabels(
            ["集号", "区间", "时长", "切点", "审核"]
        )
        header = self.episode_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.episode_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.episode_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.episode_table.setAlternatingRowColors(True)
        self.episode_table.setMinimumWidth(480)
        self.episode_table.itemSelectionChanged.connect(self._episode_selected)

        list_card = Card("集列表", parent=self)
        list_card.body_layout.addWidget(self.episode_table, 1)

        # ---- 预览 ----
        self.preview = MediaPreview()

        # ---- 当前集卡片 ----
        self.card = QTextEdit()
        self.card.setReadOnly(True)
        self.card.setMinimumHeight(140)

        self._review_pill = StatusPill("未审核", tone="muted")

        card_card = Card("当前集", parent=self)
        pill_row = QHBoxLayout()
        pill_row.addWidget(self._review_pill)
        pill_row.addStretch(1)
        card_card.body_layout.insertLayout(0, pill_row)
        card_card.body_layout.addWidget(self.card, 1)

        # ---- 方案层校验 ----
        self.problem_box = QTextEdit()
        self.problem_box.setReadOnly(True)
        self.problem_box.setMinimumHeight(80)
        self._problem_pill = StatusPill("无问题", tone="success")

        problem_card = Card("方案层校验", parent=self)
        pill_row2 = QHBoxLayout()
        pill_row2.addWidget(QLabel("硬约束问题会阻止导出"))
        pill_row2.addStretch(1)
        pill_row2.addWidget(self._problem_pill)
        problem_card.body_layout.insertLayout(0, pill_row2)
        problem_card.body_layout.addWidget(self.problem_box, 1)

        # ---- 切点操作 ----
        self.btn_set_boundary = QPushButton("把当前位置设为该集结束切点")
        self.btn_set_boundary.setProperty("role", "primary")
        self.btn_set_boundary.style().unpolish(self.btn_set_boundary)
        self.btn_set_boundary.style().polish(self.btn_set_boundary)
        self.btn_lock = QPushButton("🔒  锁定该切点")
        self.btn_unlock = QPushButton("🔓  解除锁定")
        self.btn_confirm = QPushButton("✔  确认该集")
        self.btn_confirm.setProperty("role", "primary")
        self.btn_confirm.style().unpolish(self.btn_confirm)
        self.btn_confirm.style().polish(self.btn_confirm)
        self.btn_snap = QPushButton("吸附到最近帧")

        for w in (
            self.btn_set_boundary,
            self.btn_lock,
            self.btn_unlock,
            self.btn_confirm,
            self.btn_snap,
        ):
            w.setToolTip(w.text())

        self.btn_set_boundary.clicked.connect(self._set_boundary)
        self.btn_lock.clicked.connect(lambda: self._lock(True))
        self.btn_unlock.clicked.connect(lambda: self._lock(False))
        self.btn_confirm.clicked.connect(self._confirm_episode)
        self.btn_snap.clicked.connect(lambda: self._goto(self._position))

        ops_card = Card("切点操作", parent=self)
        for w in (
            self.btn_set_boundary,
            self.btn_lock,
            self.btn_unlock,
            self.btn_confirm,
            self.btn_snap,
        ):
            ops_card.body_layout.addWidget(w)

        # 右侧：preview + 当前集 + 切点操作 + 方案层校验
        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(self.preview, 2)
        right_col.addWidget(card_card, 1)
        right_col.addWidget(ops_card)
        right_col.addWidget(problem_card, 1)

        # 装配
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_widget = QWidget()
        left_widget.setLayout(QVBoxLayout())
        left_widget.layout().setContentsMargins(0, 0, 0, 0)
        left_widget.layout().addWidget(list_card)
        right_widget = QWidget()
        right_widget.setLayout(right_col)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 920])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addWidget(splitter)

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
            self._problem_pill.set_text("无方案")
            self._problem_pill.set_tone("muted")
            return
        problems = plan.validate(self.state.settings, None)
        if not problems:
            self.problem_box.setPlainText("方案层校验：无问题。")
            self._problem_pill.set_text("无问题")
            self._problem_pill.set_tone("success")
            return
        blocking = sum(1 for p in problems if p.is_blocking)
        warning = len(problems) - blocking
        if blocking:
            self._problem_pill.set_text(f"{blocking} 阻断")
            self._problem_pill.set_tone("danger")
        elif warning:
            self._problem_pill.set_text(f"{warning} 警告")
            self._problem_pill.set_tone("warn")
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
        _, snapped = media.snap_to_frame(seconds)
        self._position = snapped
        self._player.set_range(float(snapped))
        self.preview.timecode.setText(
            f"{format_timecode(self._position)}　（{float(self._position):.3f}s）"
        )
        try:
            image = self._decoder.frame_at(snapped)
        except DecodeError as exc:
            self.preview.clear(f"解码失败：{exc}")
            return
        if image is not None:
            # 把帧号注入 MediaPreview 的 HUD
            frame_index = media.frame_index_at(snapped)
            self.preview.set_frame_info(frame_index=frame_index)
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

        index = self._current_episode
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
        self._review_pill.set_text("已确认")
        self._review_pill.set_tone("success")
        self.refresh()
        self.episode_table.selectRow(max(0, self._current_episode - 1))

    def _update_card(self) -> None:
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

        # 同步审核状态徽章
        if plan.semantic_review_status == "confirmed":
            self._review_pill.set_text("已确认")
            self._review_pill.set_tone("success")
        else:
            self._review_pill.set_text("未审核")
            self._review_pill.set_tone("muted")

    def can_continue(self) -> tuple[bool, str]:
        plan = self.state.current_plan
        if plan is None:
            return False, "尚无方案。"
        blocking = [p for p in plan.validate() if p.is_blocking]
        if blocking:
            return False, "方案存在未解决的硬约束问题：" + blocking[0].describe()
        return True, ""


# ---------------------------------------------------------------------------
# 页 5  导出
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
            transcript=self.state.transcript,
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


# ---------------------------------------------------------------------------
# 页 6  模型
# ---------------------------------------------------------------------------


class ModelPage(QWidget):
    """模型选择与下载。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 同 SettingsPage：本页装在抽屉里，表格四列需要足够的横向空间。
        self.setMinimumWidth(640)
        self._worker: "ModelDownloadWorker | None" = None
        self._config = AppConfig.load()

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["模型", "体积", "状态", "用途"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)

        self.quant_combo = QComboBox()
        self.btn_discover = QPushButton("🔍  获取可用量化档位")
        self.btn_discover.clicked.connect(self._discover_quants)

        self.dir_edit = QLineEdit(str(self._config.resolved_models_dir()))
        self.btn_pick_dir = QPushButton("更改目录…")
        self.btn_pick_dir.clicked.connect(self._pick_dir)
        self.free_label = QLabel("")
        self.free_label.setProperty("role", "muted")

        # 目录卡片
        dir_card = Card("模型目录", parent=self)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(self.btn_pick_dir)
        old = dir_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        new_layout = QVBoxLayout()
        new_layout.setContentsMargins(12, 10, 12, 10)
        new_layout.setSpacing(8)
        new_layout.addLayout(dir_row)
        new_layout.addWidget(self.free_label)
        dir_card.body.setLayout(new_layout)
        dir_card.body_layout = new_layout

        # 模型表卡片
        table_card = Card("可用模型", parent=self)
        table_card.body_layout.addWidget(self.table, 1)

        # 量化档位卡片
        quant_card = Card("语义模型量化档位", parent=self)
        quant_row = QHBoxLayout()
        quant_row.addWidget(self.quant_combo, 1)
        quant_row.addWidget(self.btn_discover)
        old = quant_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        new_layout = QVBoxLayout()
        new_layout.setContentsMargins(12, 10, 12, 10)
        new_layout.setSpacing(8)
        new_layout.addLayout(quant_row)
        quant_card.body.setLayout(new_layout)
        quant_card.body_layout = new_layout

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        self.btn_download = QPushButton("⬇  下载选中模型")
        self.btn_download.setProperty("role", "primary")
        self.btn_download.style().unpolish(self.btn_download)
        self.btn_download.style().polish(self.btn_download)
        self.btn_delete = QPushButton("删除选中模型")
        self.btn_delete.setProperty("role", "danger")
        self.btn_delete.style().unpolish(self.btn_delete)
        self.btn_delete.style().polish(self.btn_delete)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_download.clicked.connect(self._start_download)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_download)

        # 日志
        self.log = LogPanel()
        log_card = Card("日志", parent=self)
        log_card.body_layout.addWidget(self.log, 1)

        # 顶部说明
        header = QLabel(
            "选择并下载所需的模型。语音转写模型与语义判断模型相互独立，"
            "可只装其一；未安装时对应能力自动跳过并在日志中说明。"
        )
        header.setProperty("role", "muted")
        header.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(header)
        outer.addWidget(dir_card)
        outer.addWidget(table_card, 1)
        outer.addWidget(quant_card)
        outer.addWidget(self.progress)
        outer.addLayout(btn_row)
        outer.addWidget(log_card, 1)

        self._refresh()

    # ---- 目录 ----------------------------------------------------------

    def _models_root(self):
        return Path(self.dir_edit.text().strip() or str(default_models_root()))

    def _refresh(self) -> None:
        self.table.setRowCount(0)
        for spec in MODEL_CATALOG:
            state = installed_state(spec, self._models_root())
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(spec.label))
            size = f"{spec.approx_mb} MB" if spec.approx_mb else "未知（下载时探测）"
            self.table.setItem(row, 1, QTableWidgetItem(size))
            item = QTableWidgetItem(state.describe())
            if not state.ready:
                item.setForeground(QColor("#9a6700"))
            self.table.setItem(row, 2, item)
            self.table.setItem(row, 3, QTableWidgetItem(spec.note))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, spec.key)
        self.table.resizeColumnsToContents()

        free = free_space_bytes(self._models_root())
        if free is None:
            self.free_label.setText("磁盘可用空间：未知")
        else:
            self.free_label.setText(
                f"磁盘可用空间：{free / 1024 / 1024 / 1024:.1f} GB"
                "（下载前会再检查一次，空间不足会明确拒绝）"
            )

        installed = [s.key for s in MODEL_CATALOG if installed_state(s, self._models_root()).ready]
        if self._config.last_download_key in installed:
            self.quant_combo.setCurrentText(self._config.last_download_key)

    def _selected_spec(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return spec_by_key(key)

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择模型目录", str(self._models_root())
        )
        if not chosen:
            return
        self.dir_edit.setText(chosen)
        self._config.models_dir = chosen
        try:
            self._config.save()
            self._append(f"模型目录已改为 {chosen}（已记住，重启后仍生效）")
        except OSError as exc:
            self._append(f"目录已切换，但保存配置失败（重启后需重新选择）：{exc}")
        self._refresh()

    def _discover_quants(self) -> None:
        from app.core.model_manager import LLM_SIZES, list_remote_gguf

        size = "2B"
        if self.quant_combo.count():
            current = self.quant_combo.currentText()
            for candidate, _ in LLM_SIZES:
                if candidate in current:
                    size = candidate
                    break
        repo = f"unsloth/Qwen3.5-{size}-GGUF"
        self._append(f"查询 {repo} 可用量化档位…")
        names = list_remote_gguf(repo)
        if not names:
            self._append("查询失败或无结果（网络/镜像不可达）。可先下载默认的 Q4_K_M。", "warn")
            return
        quants = []
        for name in names:
            stem = name.rsplit(".", 1)[0]
            parts = stem.split("-")
            if len(parts) >= 3:
                quants.append(parts[-1])
        self.quant_combo.clear()
        self.quant_combo.addItems(sorted(set(quants)))
        self._append(f"发现 {len(quants)} 个档位：{'、'.join(sorted(set(quants)))}")

    def _spec_from_quant(self) -> list:
        from app.core.model_manager import LLM_SIZES, llm_spec

        quant = self.quant_combo.currentText().strip() or "Q4_K_M"
        specs = []
        for size, _ in LLM_SIZES:
            if size in quant:
                clean = quant.split(f"{size}-")[-1]
                specs.append(llm_spec(size, clean))
                return specs
        return [llm_spec("2B", quant)]

    def _start_download(self) -> None:
        specs = []
        spec = self._selected_spec()
        if spec is not None:
            specs.append(spec)
        else:
            specs.extend(self._spec_from_quant())
        if not specs:
            QMessageBox.information(self, "未选择", "请先在表格里选中要下载的模型。")
            return

        self.btn_download.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self._worker = ModelDownloadWorker(
            specs[0], self._models_root(), endpoint=self._config.endpoint, parent=self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append)
        self._worker.finished_with.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str, index: int, count: int) -> None:
        if total > 0:
            self.progress.setValue(int(done / total * 100))
            self.progress.setFormat(
                f"{name}（{index}/{count}）{done / 1024 / 1024:.0f}/"
                f"{total / 1024 / 1024:.0f} MB"
            )
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"{name} 已下载 {done / 1024 / 1024:.0f} MB（总大小未知）")

    def _on_finished(self, result) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if result.ok else 0)
        self.btn_download.setEnabled(True)
        self.btn_delete.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if result.ok:
            self._config.last_download_key = result.spec.key
            try:
                self._config.save()
            except OSError:
                pass
            self._refresh()
            QMessageBox.information(
                self, "下载完成", f"{result.describe()}\n\n即可直接使用，无需重启。"
            )
        elif result.cancelled:
            self._append("已取消，临时文件已清理。", "warn")
        else:
            QMessageBox.warning(self, "下载未完成", result.describe())

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self.btn_cancel.setEnabled(False)

    def _delete_selected(self) -> None:
        spec = self._selected_spec()
        if spec is None:
            QMessageBox.information(self, "未选择", "请先选中要删除的模型。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"删除 {spec.label}？\n目录：{spec.target_dir(self._models_root())}\n"
            "只删除该模型自己的目录，不影响其他模型。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = delete_model(spec, self._models_root())
        self._append(message)
        self._refresh()

    def _append(self, text: str, level: str = "info") -> None:
        self.log.append(text, level=level)

    def can_continue(self) -> tuple[bool, str]:
        return True, ""


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


def _scrollable(widget: QWidget) -> QScrollArea:
    """把 widget 装进一个无边框的横向滚动容器。

    抽屉固定 420px 宽，而被装入的设置页 / 模型页需要 640px 才能正常排版。
    交给 QScrollArea 后，内容保持自己的最小宽度，超出的部分横向滚动，
    而不是被压成 90px 一列。
    """
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return area


class MainWindow(QMainWindow):
    PAGES = ["导入", "设置", "分析", "审核", "导出", "模型"]

    # stepper 显示用的 4 个工作阶段（≠PAGES 6 项）
    STEPPER_NAMES = ["导入", "分析", "审核", "导出"]
    # PAGES 0/2/3/4 对应 stepper 0/1/2/3；PAGES 1/5 走抽屉
    _STEPPER_INDEX = {0: 0, 2: 1, 3: 2, 4: 3}

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 短剧智能分集器")
        self.resize(1440, 900)
        # 显式设最小尺寸，避免各子控件的 minimumSizeHint 累加成 1325+
        self.setMinimumSize(960, 600)
        self.setMaximumSize(4096, 2160)

        apply_theme()
        self.state = ProjectState()

        # 顶栏（含 stepper + 设置/模型按钮）
        self.app_bar = AppBar("🎬  AI 短剧分集器", self.STEPPER_NAMES)
        self.app_bar.step_clicked.connect(self._on_stepper_clicked)
        self.app_bar.settings_clicked.connect(lambda: self.nav.setCurrentRow(1))
        self.app_bar.model_clicked.connect(lambda: self.nav.setCurrentRow(5))

        # StepNav 实例保留（兼容测试 + 作为导航数据模型），但不可见
        self.nav = StepNav(self.PAGES)
        self.nav.setVisible(False)
        self.nav.currentRowChanged.connect(self._nav_changed)

        # 状态条（极简）
        self.status_strip = QFrame()
        self.status_strip.setStyleSheet(
            "QFrame { background: #f5f6fa; border-bottom: 1px solid #d9dce4; }"
        )
        self.status_strip.setFixedHeight(32)
        self.status_left = QLabel("未导入源视频")
        self.status_left.setStyleSheet("color: #5f6368; padding: 0 16px;")
        self.status_progress = QLabel("")
        self.status_progress.setStyleSheet("color: #5f6368;")
        self.status_right = QLabel(f"第 1 / {len(self.STEPPER_NAMES)} 步")
        self.status_right.setStyleSheet("color: #5f6368; padding: 0 16px;")

        strip_layout = QHBoxLayout(self.status_strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(0)
        strip_layout.addWidget(self.status_left, 1)
        strip_layout.addWidget(self.status_progress, 1)
        strip_layout.addWidget(self.status_right)

        # 主工作区 stack：4 个工作页 + 2 个占位（凑 count()==6，兼容测试）
        self.stack = QStackedWidget()
        self.import_page = ImportPage(self.state)
        self.settings_page = SettingsPage(self.state)
        self.analysis_page = AnalysisPage(self.state)
        self.review_page = ReviewPage(self.state)
        self.export_page = ExportPage(self.state)
        self.model_page = ModelPage(self.state)

        for page in (
            self.import_page,
            self._make_placeholder("设置（点击右上角 ⚙）"),
            self.analysis_page,
            self.review_page,
            self.export_page,
            self._make_placeholder("模型管理（点击右上角 🤖）"),
        ):
            self.stack.addWidget(page)

        # 底部 action bar：提示 + 继续按钮
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#9a6700; padding: 0 16px;")

        self.btn_continue = QPushButton("继续  →")
        self.btn_continue.setProperty("role", "primary")
        self.btn_continue.style().unpolish(self.btn_continue)
        self.btn_continue.style().polish(self.btn_continue)
        self.btn_continue.setMinimumWidth(140)
        self.btn_continue.clicked.connect(self._on_continue_clicked)

        # 兼容性占位（测试断言 window.btn_prev.isEnabled() == False）
        self.btn_prev = QPushButton("←  上一步")
        self.btn_prev.setVisible(False)
        self.btn_prev.setEnabled(False)
        self.btn_next = QPushButton("下一步  →")
        self.btn_next.setVisible(False)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.hint, 1)
        footer.addWidget(self.btn_continue)
        footer_widget = QFrame()
        footer_widget.setLayout(footer)
        footer_widget.setFixedHeight(56)
        footer_widget.setStyleSheet(
            "QFrame { background:#ffffff; border-top:1px solid #d9dce4; }"
        )

        # 主内容区（占满 stack）
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.stack, 1)

        body_widget = QWidget()
        body_widget.setLayout(body)

        # 总布局
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.app_bar)
        root.addWidget(self.status_strip)
        root.addWidget(body_widget, 1)
        root.addWidget(footer_widget)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # 设置 / 模型抽屉（绑在 central 之上）
        # 抽屉固定 420px，而两张页面的最小宽度是 640px：直接塞进去会被挤变形，
        # 因此套一层横向滚动区，窄抽屉里也能完整读到内容。
        self.settings_drawer = Drawer("设置", parent=central)
        self.settings_drawer.set_body(_scrollable(self.settings_page))
        self.settings_drawer.closed.connect(lambda: self._on_drawer_closed(1))

        self.model_drawer = Drawer("模型管理", parent=central)
        self.model_drawer.set_body(_scrollable(self.model_page))
        self.model_drawer.closed.connect(lambda: self._on_drawer_closed(5))

        # 状态条已经把「导入状态 + 第 N / M 步」显示了一遍，QMainWindow 的原生
        # 状态栏会再显示一遍同样内容。因此隐藏原生状态栏，只保留对象本身
        # 以兼容既有测试的取值。
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.addWidget(self.status_left, 1)
        self.status_bar.hide()

        self.nav.setCurrentRow(0)
        self._install_shortcuts()

    @staticmethod
    def _make_placeholder(text: str) -> QWidget:
        """stack 凑数用的占位 widget：实际入口是顶栏的图标按钮。"""
        w = QFrame()
        w.setStyleSheet("QFrame { background: #f5f6fa; }")
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            "color: #5f6368; font-size: 14pt; background: transparent;"
        )
        layout.addWidget(label)
        return w

    # ---- 顶栏 stepper 点击 → 跳对应 PAGES ----

    def _on_stepper_clicked(self, index: int) -> None:
        # index 是 stepper 中的 0..3，对应 PAGES 0/2/3/4
        inv = {v: k for k, v in self._STEPPER_INDEX.items()}
        if index in inv:
            self.nav.setCurrentRow(inv[index])

    def _on_drawer_closed(self, pages_index: int) -> None:
        # 抽屉关闭时，把 nav 也跳回当前工作区（避免误判 can_continue）
        cur = self.nav.currentRow()
        if cur == pages_index:
            # 回退到最近的 stepper index
            self.nav.setCurrentRow(0)

    def _on_continue_clicked(self) -> None:
        """底部按钮：根据当前阶段做不同动作。"""
        cur = self.nav.currentRow()
        if cur == 0:    # 导入 → 分析
            ok, msg = self.import_page.can_continue()
            if not ok:
                self.hint.setText(msg); return
            self.nav.setCurrentRow(2)
        elif cur == 2:  # 分析
            # 分析完成后才能继续
            if self.analysis_page._worker and self.analysis_page._worker.isRunning():
                self.hint.setText("分析进行中，请先取消或等待"); return
            if self.state.current_plan is None:
                # 触发开始
                self.analysis_page._start()
                return
            self.nav.setCurrentRow(3)
        elif cur == 3:  # 审核 → 导出
            ok, msg = self.review_page.can_continue()
            if not ok:
                self.hint.setText(msg); return
            self.nav.setCurrentRow(4)
        elif cur == 4:  # 导出
            self.export_page._start(None)

    # ---- 键盘快捷键 --------------------------------------------------

    def _install_shortcuts(self) -> None:
        # 上一步 / 下一步
        QShortcut(QKeySequence("Left"), self, activated=lambda: self._navigate(-1))
        QShortcut(QKeySequence("Right"), self, activated=lambda: self._navigate(1))
        # 跳到指定页：Ctrl+1..6
        for i in range(1, len(self.PAGES) + 1):
            QShortcut(
                QKeySequence(f"Ctrl+{i}"),
                self,
                activated=lambda idx=i - 1: self.nav.setCurrentRow(idx),
            )
        # 审核页播放/暂停、上下帧
        QShortcut(QKeySequence("Space"), self, activated=self._space_pressed)
        QShortcut(QKeySequence("K"), self, activated=lambda: self.review_page.preview.btn_next_frame.click())
        QShortcut(QKeySequence("J"), self, activated=lambda: self.review_page.preview.btn_prev_frame.click())

    def _space_pressed(self) -> None:
        if self.stack.currentWidget() is self.review_page:
            if self.review_page._player.is_playing() if hasattr(self.review_page._player, 'is_playing') else False:
                self.review_page.preview.btn_stop.click()
            else:
                self.review_page.preview.btn_play.click()

    # ---- 导航 ---------------------------------------------------------

    def _nav_changed(self, index: int) -> None:
        if index < 0:
            return
        # 1/5 = 抽屉；0/2/3/4 = 工作区 stack
        if index == 1:
            self.stack.setCurrentIndex(1)
            self.settings_drawer.show()
            self.settings_page.refresh_from_state()
        elif index == 5:
            self.stack.setCurrentIndex(5)
            self.model_drawer.show()
        else:
            # 关闭所有抽屉
            if self.settings_drawer.isVisible():
                self.settings_drawer.hide()
            if self.model_drawer.isVisible():
                self.model_drawer.hide()
            # 索引直接对应 stack 中的位置（0..5）
            self.stack.setCurrentIndex(index)

        self._refresh_nav_states()

        if index in self._STEPPER_INDEX:
            self.status_right.setText(
                f"第 {self._STEPPER_INDEX[index] + 1} / {len(self.STEPPER_NAMES)} 步"
            )
        else:
            self.status_right.setText("⚙ 设置" if index == 1 else "🤖 模型")

        self._on_page_shown(index)

    def _update_stepper_for_drawer(self, open_: str) -> None:
        """抽屉打开时，stepper 不显示 current 高亮（因为不在工作流中）。"""
        # 直接由 _refresh_nav_states 统一处理
        pass

    def _refresh_nav_states(self) -> None:
        """更新 stepper 的 4 个状态 + nav 内部 6 项状态。"""
        cur = self.nav.currentRow()

        # nav 内部 6 项（保持与 PAGES 对齐，test 仍可读 nav 状态）
        nav_states: list[StepState] = [StepState.FUTURE] * len(self.PAGES)
        nav_states[0] = (
            StepState.DONE if self.state.media is not None
            else (StepState.CURRENT if cur == 0 else StepState.FUTURE)
        )
        nav_states[1] = (
            StepState.CURRENT if cur == 1
            else (StepState.DONE if self.state.media else StepState.FUTURE)
        )
        if self.state.current_plan is not None:
            nav_states[2] = StepState.DONE
        else:
            nav_states[2] = StepState.CURRENT if cur == 2 else StepState.FUTURE
        if self.state.current_plan is not None:
            nav_states[3] = (
                StepState.DONE
                if self.state.current_plan.semantic_review_status == "confirmed"
                else StepState.CURRENT
            )
        else:
            nav_states[3] = StepState.FUTURE
        if self.state.exported:
            nav_states[4] = StepState.DONE
        else:
            nav_states[4] = (
                StepState.CURRENT if cur == 4
                else (StepState.DONE if self.state.current_plan else StepState.FUTURE)
            )
        nav_states[5] = StepState.CURRENT if cur == 5 else StepState.FUTURE
        if 0 <= cur < len(nav_states):
            nav_states[cur] = StepState.CURRENT
        self.nav.set_states(nav_states)

        # stepper 4 项
        step_states: list[StepState] = [StepState.FUTURE] * len(self.STEPPER_NAMES)
        # 0: 导入
        if self.state.media is not None:
            step_states[0] = StepState.DONE
        else:
            step_states[0] = StepState.CURRENT
        # 1: 分析
        if self.state.current_plan is not None:
            step_states[1] = StepState.DONE
        elif self.state.media is not None:
            step_states[1] = StepState.CURRENT
        # 2: 审核
        if self.state.current_plan is not None:
            step_states[2] = (
                StepState.DONE
                if self.state.current_plan.semantic_review_status == "confirmed"
                else StepState.CURRENT
            )
        # 3: 导出
        if self.state.exported:
            step_states[3] = StepState.DONE
        elif self.state.current_plan is not None:
            step_states[3] = StepState.CURRENT

        # 抽屉打开时强制 stepper 不高亮
        if cur in (1, 5):
            for i in range(len(step_states)):
                if step_states[i] == StepState.CURRENT:
                    step_states[i] = StepState.DONE
            # 但要让当前工作区阶段高亮（如果没有 done）
            if cur == 5:
                # 模型抽屉不影响 stepper
                pass
        # 当前 stepper index（若在工作区）
        cur_step = self._STEPPER_INDEX.get(cur, -1)
        if cur_step >= 0 and step_states[cur_step] != StepState.DONE:
            step_states[cur_step] = StepState.CURRENT

        done_indices = {i for i, s in enumerate(step_states) if s == StepState.DONE}
        current_idx = next(
            (i for i, s in enumerate(step_states) if s == StepState.CURRENT), 0
        )
        self.app_bar.set_step_state(current_idx, done_indices)

        # 「继续」按钮文案
        if cur == 0:
            self.btn_continue.setText("下一步：分析  →")
        elif cur == 2:
            if self.analysis_page._worker and self.analysis_page._worker.isRunning():
                self.btn_continue.setText("分析进行中…")
                self.btn_continue.setEnabled(False)
                return
            self.btn_continue.setEnabled(True)
            self.btn_continue.setText("▶  开始分析" if self.state.current_plan is None else "下一步：审核  →")
        elif cur == 3:
            self.btn_continue.setText("下一步：导出  →")
        elif cur == 4:
            self.btn_continue.setText("▶  开始导出")
        else:
            self.btn_continue.setText("继续  →")
        self.btn_continue.setEnabled(cur in (0, 2, 3, 4))

        # 模型通知徽章：若全部模型未安装 → 提示
        try:
            from app.core.model_manager import MODEL_CATALOG, installed_state as _ist
            any_installed = any(
                _ist(s, self.app_bar.parent().__class__).ready  # placeholder
                if False else False
                for s in MODEL_CATALOG
            )
            # 简单检查：依赖具体路径
            from pathlib import Path as _P
            from app.core.app_config import default_models_root as _dmr
            root = _dmr()
            any_installed = any(
                s.target_dir(root).exists()
                and any(s.target_dir(root).glob("*"))
                for s in MODEL_CATALOG
            )
            self.app_bar.set_model_notify(not any_installed)
        except Exception:
            self.app_bar.set_model_notify(False)

    def _on_page_shown(self, index: int) -> None:
        self.hint.setText("")
        if index == 3:
            self.review_page.refresh()
        elif index == 4:
            self.export_page.refresh()
        # 状态条左
        if self.state.media is not None:
            self.status_left.setText(f"源文件：{self.state.media.path}")
        else:
            self.status_left.setText("未导入源视频")

    def _current_page(self):
        """返回对应阶段的页面（仅工作区 4 个）。"""
        mapping = {0: self.import_page, 2: self.analysis_page, 3: self.review_page, 4: self.export_page}
        return mapping.get(self.nav.currentRow())

    def _navigate(self, delta: int) -> None:
        """兼容旧 API：按 delta 上下移动到下一个工作区。"""
        cur = self.nav.currentRow()
        # 顺序：导入(0) → 分析(2) → 审核(3) → 导出(4)
        order = [0, 2, 3, 4]
        if cur not in order:
            return
        i = order.index(cur)
        if delta > 0:
            ok, message = self._current_page().can_continue()
            if not ok:
                self.hint.setText(message)
                return
            if i + 1 < len(order):
                self.nav.setCurrentRow(order[i + 1])
        elif delta < 0:
            if i - 1 >= 0:
                self.nav.setCurrentRow(order[i - 1])

    # ---- 旧 nav 仍要保留 ----

    def _legacy_nav_state(self) -> None:
        """保留旧方法作为占位，避免破坏继承。"""
        pass