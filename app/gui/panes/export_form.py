"""导出表单：导出概览、输出位置、进阶编码、事务式批量导出与失败重试。

由 ``app/gui/dialogs/export_dialog.py`` 的 ExportDialog 承载；对话框负责
§37 前置检查的决策（可覆写 ``request_precheck``），本表单只负责执行。

界面分两层：首屏只回答"导几集、放哪儿、有多大"，编码器参数收进
「调整编码参数」；导出队列占主体高度，空方案时显示空态而不是空白表。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
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
from ..theme import current_mode, qcolor
from ..state import ProjectState
from ..widgets import (
    Card,
    EmptyState,
    LabeledField,
    NoWheelComboBox,
    NoWheelSpinBox,
    StatusPill,
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


class _QueueCard(Card):
    """队列卡：参与滚动区尺寸计算时只报一个紧凑高度。

    ``QScrollArea`` 在 ``widgetResizable`` 下按子控件的 **sizeHint** 决定正文
    高度，而 ``QTableWidget`` 的 sizeHint 会随行数增长。若直接使用普通 Card，
    6 集时正文就被撑到 670px、视口只有 640px，队列表只露出两行。这里把队列卡
    的 sizeHint 固定成紧凑值，实际高度仍由布局 stretch 分配到视口剩余空间。
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        hint = super().sizeHint()
        # 与表格的最小高度保持一致。过小会让 QScrollArea 按此提示把队列卡
        # 压到只露表头，再靠整页滚动补足；用户看到的就是最后一行被底栏盖住。
        return QSize(hint.width(), 180)


def _format_duration(seconds) -> str:
    """导出概览用的短时长：120 秒 → 2 分 00 秒；小时以上带小时。"""
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 时 {minutes:02d} 分"
    if minutes:
        return f"{minutes} 分 {secs:02d} 秒"
    return f"{secs} 秒"


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
    # 空闲时点「关闭」= 关闭对话框；导出中则取消任务
    close_requested = Signal()

    def __init__(self, state: ProjectState, parent: QWidget | None = None,
                 precheck=None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: ExportWorker | None = None
        # 前置检查的决策函数：(warnings, unreviewed) -> "continue" | "review"。
        # 由对话框注入它的 request_precheck，测试则直接覆写对话框的方法。
        self.precheck = precheck or self._default_precheck

        self.codec_combo = NoWheelComboBox()
        self.codec_combo.addItem(
            "原生编码（不重新编码，画质 / 音质 / 码率不变）", "fast_copy"
        )
        self.codec_combo.addItem("x264（CPU，重新编码）", "libx264")
        self.codec_combo.addItem("NVENC（GPU，重新编码）", "h264_nvenc")
        self._nvenc_ok = nvenc_cached(self.state.binaries)
        if not self._nvenc_ok:
            item = self.codec_combo.model().item(2)
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

        # ---- 首屏：导出概览 --------------------------------------------
        #
        # 旧版第一张卡直接是 CRF / preset / 音频码率这些编码器词汇，用户打开
        # 导出页最想知道的是"会输出几集、放哪儿、能不能开始"。参数收进
        # 「进阶编码设置」，首屏只保留结论与三个关键动作。
        self.summary_title = QLabel("准备导出", self)
        self.summary_title.setProperty("role", "export-heading")
        self.summary_detail = QLabel("", self)
        self.summary_detail.setProperty("role", "muted")
        self.summary_detail.setWordWrap(True)
        self.summary_state = StatusPill("待开始", "muted", self)

        summary_head = QHBoxLayout()
        summary_head.setContentsMargins(0, 0, 0, 0)
        summary_head.setSpacing(10)
        summary_text = QVBoxLayout()
        summary_text.setContentsMargins(0, 0, 0, 0)
        summary_text.setSpacing(3)
        summary_text.addWidget(self.summary_title)
        summary_text.addWidget(self.summary_detail)
        summary_head.addLayout(summary_text, 1)
        summary_head.addWidget(self.summary_state, 0, Qt.AlignmentFlag.AlignTop)

        self.metric_episodes = QLabel("—", self)
        self.metric_episodes.setProperty("role", "export-metric-value")
        self.metric_duration = QLabel("—", self)
        self.metric_duration.setProperty("role", "export-metric-value")
        self.metric_size = QLabel("—", self)
        self.metric_size.setProperty("role", "export-metric-value")
        metrics = QHBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(8)
        for caption, value in (
            ("集数", self.metric_episodes),
            ("总时长", self.metric_duration),
            ("预计占用（按码率）", self.metric_size),
        ):
            metrics.addWidget(self._metric(caption, value), 1)

        # ---- 输出位置 ---------------------------------------------------
        self.output_edit = QLineEdit(self)
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("还没有选择输出目录")
        self.btn_output = QPushButton("选择目录…", self)
        self.btn_output.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(8)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.btn_output)

        # ---- 前情回顾：默认关闭 ------------------------------------------
        self.recap_toggle = QCheckBox("第 2 集起，片头带上一集片尾", self)
        self.recap_toggle.setToolTip(
            "只影响导出文件：在每一集开头重复上一集最后几秒，方便观众接上剧情。"
            "不会改动方案切点与审核结果。"
        )
        self.recap_seconds = NoWheelSpinBox(self)
        self.recap_seconds.setRange(1, 30)
        self.recap_seconds.setValue(5)
        self.recap_seconds.setSuffix(" 秒")
        self.recap_seconds.setToolTip(
            "前情回顾至少取这么久；原生编码会向前吸附到关键帧，实际可能更长"
        )
        recap_row = QHBoxLayout()
        recap_row.setContentsMargins(0, 0, 0, 0)
        recap_row.setSpacing(8)
        recap_row.addWidget(self.recap_toggle)
        recap_row.addWidget(self.recap_seconds)
        recap_row.addStretch(1)
        self.recap_toggle.setToolTip(
            "只影响导出文件：第 2 集起在正片前重复上一集片尾。"
            "不改方案切点与审核结果；字幕会同步包含回顾段。"
        )
        self.recap_hint = QLabel("", self)
        self.recap_hint.setProperty("role", "muted")
        self.recap_toggle.toggled.connect(self._refresh_recap)
        self.recap_seconds.valueChanged.connect(self._refresh_recap)
        # ---- 进阶编码：默认收起 ----------------------------------------
        codec_field = LabeledField("编码方式")
        codec_field.attach(self.codec_combo)
        self.codec_hint = QLabel("")
        self.codec_hint.setWordWrap(True)
        self.codec_hint.setProperty("role", "muted")
        crf_field = LabeledField("CRF（越小越清晰）")
        crf_field.attach(self.crf_spin)
        preset_field = LabeledField("编码速度")
        preset_field.attach(self.preset_combo)
        audio_field = LabeledField("音频")
        audio_field.attach(self.audio_bitrate_combo)
        self._crf_field = crf_field
        self._audio_field = audio_field

        advanced_grid = QGridLayout()
        advanced_grid.setContentsMargins(0, 0, 0, 0)
        advanced_grid.setHorizontalSpacing(12)
        advanced_grid.setVerticalSpacing(8)
        advanced_grid.addWidget(codec_field, 0, 0, 1, 2)
        advanced_grid.addWidget(crf_field, 1, 0)
        advanced_grid.addWidget(preset_field, 1, 1)
        advanced_grid.addWidget(audio_field, 2, 0)

        self.advanced_toggle = QCheckBox("调整编码参数", self)
        self.advanced_toggle.setToolTip(
            "编码方式 / 质量 / 速度 / 音频码率；默认参数适合大多数短剧"
        )
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.advanced_host = QWidget(self)
        advanced_layout = QVBoxLayout(self.advanced_host)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(8)
        advanced_layout.addLayout(advanced_grid)
        advanced_layout.addWidget(self.codec_hint)
        self.advanced_host.setVisible(False)
        # 展开编码参数时设置卡需要长高；收起时限制在自然高度，把空间让给队列。
        self.setup_card = Card("导出设置", parent=self, accent="primary")

        # 概览 / 输出 / 编码合成一张控制卡：三段都是“开始导出前要确认的事”，
        # 拆成多张卡会把队列推到折叠线以下。
        setup_card = self.setup_card
        setup_card.body_layout.setSpacing(8)
        setup_card.body_layout.addLayout(summary_head)
        setup_card.body_layout.addLayout(metrics)
        section = QLabel("输出位置", setup_card)
        section.setProperty("role", "provider-step")
        setup_card.body_layout.addWidget(section)
        setup_card.body_layout.addLayout(output_row)
        setup_card.body_layout.addLayout(recap_row)
        setup_card.body_layout.addWidget(self.recap_hint)
        setup_card.body_layout.addWidget(self.advanced_toggle)
        setup_card.body_layout.addWidget(self.advanced_host)
        # ScrollArea 会按内容的最小高度撑开，直接限制设置卡会把展开后的字段裁掉。
        setup_card.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        self.codec_combo.currentIndexChanged.connect(self._sync_codec_fields)
        self._sync_codec_fields()

        self.queue = QTableWidget(0, 5)
        self.queue.setHorizontalHeaderLabels(
            ["集号", "状态", "帧数 / 校验", "时长", "说明（含前情回顾）"]
        )
        header = self.queue.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue.setAlternatingRowColors(True)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue.verticalHeader().setVisible(False)
        self.queue.setMinimumHeight(0)

        queue_card = _QueueCard("导出队列", parent=self)
        # 卡片按自然高度参与布局时，会只给队列表 192px；队列卡占用剩余空间，
        # 表格才能把 6 集都显示出来。
        queue_card.setMinimumHeight(0)
        # QScrollArea 会按内容最小高度撑开：队列卡必须允许被压缩，否则
        # 正文高度永远等于“设置卡 + 队列表自然高度”，不是可视高度。
        queue_card.setMinimumHeight(0)
        self.queue.setMinimumHeight(0)
        queue_card.body.setMinimumHeight(0)
        queue_card.body_layout.setStretch(0, 1)
        # 表格只保留表头高度；正文会占用剩余空间，队列卡按视口长高。
        # 若给表格固定 4 行下限，QScrollArea 会保留正文自然高度，最后一行
        # 正好落到折叠线以下，看起来像被底栏盖住。
        self._base_queue_min_height = 100
        self.queue.setMinimumHeight(self._base_queue_min_height)
        self.queue_count = StatusPill("0 集", "muted", self)
        queue_card.add_header_widget(self.queue_count)
        queue_card.body_layout.addWidget(self.queue, 1)
        # 队列必须能被压缩（内部滚动），否则小屏上它的最小高度会把底部
        # 进度和按钮顶出视口——截图里表现为“队列只剩表头”。
        self.queue.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        # 没有方案时给出明确空态，而不是一张空白表格。
        self.queue_empty = EmptyState(
            "还没有可导出的方案",
            title="等待分集方案",
            detail="完成导入、分析与审核后，每一集会显示在这里。",
        )
        # 空态只服务没方案的情况；有方案时必须藏起来，否则滚动区会把它的
        # 自然高度也算进队列卡，表格被顶到折叠线以下、最后一行被底栏盖住。
        self.queue_empty.setVisible(False)
        queue_card.body_layout.addWidget(self.queue_empty, 1)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)

        self.btn_start = QPushButton("开始导出")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)
        self.btn_retry = QPushButton("重试失败项")
        self.btn_cancel = QPushButton("关闭")
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_start.clicked.connect(lambda: self.start_export(None))
        self.btn_retry.clicked.connect(self._retry)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_retry)
        btn_row.addWidget(self.btn_start)

        # 进度与操作同一行：底部再有对话框关闭栏，三行各占 4 行文字高度时
        # 720 高的窗口会把队列表头以上全部吃掉。

        # 正文（预设 / 输出 / 队列）整体可纵向滚动：小屏笔记本上，三张卡片的最小
        # 高度之和会超过屏幕，原先靠布局最小尺寸把窗口顶得比屏幕还高、还拖不小。
        # 装进滚动容器后，窗口能压到屏幕内，内容不够放时出滚动条，按钮行固定在底部。
        body = QWidget(self)
        body.setObjectName("exportBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.addWidget(setup_card)
        body_layout.addWidget(queue_card, 1)
        queue_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        # 队列卡吃掉剩余高度；设置卡按内容自然高度，不参与争抢。
        body_layout.setStretch(0, 0)
        body_layout.setStretch(1, 1)
        # QWidget 的 minimumSizeHint 会汇总子控件的最小高度，先清成 0；
        # 不足时由外层滚动区兜底，而不是把队列表压成两行。
        self.setMinimumHeight(0)
        body.setMinimumHeight(0)
        # 编码参数展开后设置卡自然高度约 600px，单列会把队列挤出首屏。
        # 展开时改为左右两栏：左 46% 放设置，右 54% 始终放队列。
        self.body_layout = body_layout
        self.setup_card = setup_card
        self.queue_card = queue_card
        # 队列卡必须被**约束在滚动视口内**。若让 QScrollArea 按 sizeHint
        # 自行决定正文高度，表格拿到的空间会大于可视区，截图里表现为
        # 队列表的最后几行被底部进度条盖住。这里把正文 layout 绑定到视口。
        body_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.setup_card_size_hint = setup_card.sizeHint().height()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        body_area = scrollable(body)
        # 队列的高度必须由视口分配，不能被内容的最小高度冻在 192px：
        # QScrollArea 默认按 widget 的 minimumSizeHint 分配，表格的
        # min-height 会变成硬下限，宁可内部滚动。
        body.setMinimumHeight(0)
        body_area.setMinimumHeight(0)
        # 当对话框只有 640px 可视高时，正文会自然高 670px（顶部 354 + 队列 304
        # + 间距），QScrollArea 会保留内容自然高度而不是压缩队列。把顶部设置卡
        # 的最终高度固定，队列拿剩余空间，避免只露出两三行。
        body_area.setWidgetResizable(True)
        body_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body_area.setSizeAdjustPolicy(
            QScrollArea.SizeAdjustPolicy.AdjustIgnored
        )
        outer.addWidget(body_area, 1)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        status_row.addWidget(self.progress, 1)
        status_row.addLayout(btn_row)
        outer.addLayout(status_row)
        self.progress.setMaximumHeight(24)
        self._refresh_recap()

    def _metric(self, caption: str, value: QLabel) -> QWidget:
        """一个概览数字：小标题在上、数值在下。"""
        holder = QWidget(self)
        holder.setProperty("role", "export-metric")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(1)
        label = QLabel(caption, holder)
        label.setProperty("role", "export-metric-caption")
        value.setParent(holder)
        layout.addWidget(label)
        layout.addWidget(value)
        return holder

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_host.setVisible(bool(checked))
        # 展开时两栏排布，收起时恢复单列；两种状态都保持设置卡完整高度。
        # 展开时用两栏排布：把设置卡与队列卡从纵向布局移到横向布局；
        # 收起时移回纵向，首屏仍保持“概览 + 队列”的单列结构。
        self._rebuild_body_layout(side_by_side=checked)

    def _recap_value(self) -> int:
        """当前要带上的前情时长（秒）；未开启返回 0。"""
        return self.recap_seconds.value() if self.recap_toggle.isChecked() else 0

    def _refresh_recap(self) -> None:
        """前情开关联动：时长框只在开启时可用，并说明会发生什么。"""
        enabled = self.recap_toggle.isChecked()
        self.recap_seconds.setEnabled(enabled)
        plan = self.state.current_plan
        if not enabled:
            self.recap_hint.setText("")
            self.recap_hint.setVisible(False)
            seconds = 0
        else:
            seconds = self.recap_seconds.value()
        if not enabled:
            pass
        elif plan is not None and plan.episode_count <= 1:
            self.recap_hint.setText("当前方案只有 1 集，不会生成前情回顾。")
        elif self.codec_combo.currentData() == "fast_copy":
            self.recap_hint.setText(
                f"至少 {seconds} 秒；原生编码可能更长。"
            )
        else:
            self.recap_hint.setText(
                f"第 2 集起重复上一集最后 {seconds} 秒，正片切点不变。"
            )
        if enabled:
            self.recap_hint.setVisible(True)
        self._refresh_queue_recap(seconds)
        if plan is not None:
            episodes = plan.episodes()
            total = sum(
                (ep.duration_seconds for ep in episodes),
                episodes[0].duration_seconds * 0,
            ) + seconds * max(0, len(episodes) - 1)
            self.metric_duration.setText(_format_duration(total))
            self._refresh_estimate()
        self._refresh_summary()

    def _refresh_queue_recap(self, seconds: int) -> None:
        """队列仍处于「待导出」时，同步显示含前情回顾的预计时长。"""
        if not hasattr(self, "queue"):
            return
        plan = self.state.current_plan
        if plan is None:
            return
        for row, episode in enumerate(plan.episodes()):
            if row >= self.queue.rowCount():
                continue
            status = self.queue.item(row, 1)
            if status is not None and status.text() not in ("待导出", "成功", "失败"):
                continue
            recap = seconds if episode.index > 1 else 0
            duration = episode.duration_seconds + recap
            if status is None or status.text() == "待导出":
                self.queue.setItem(
                    row, 3, QTableWidgetItem(format_seconds_brief(duration))
                )
                note = f"片头含前情回顾 {recap} 秒" if recap else ""
                self.queue.setItem(row, 4, QTableWidgetItem(note))

    def _rebuild_body_layout(self, *, side_by_side: bool) -> None:
        layout = self.body_layout
        if side_by_side == getattr(self, "_side_by_side", False):
            return
        self._side_by_side = side_by_side
        for widget in (self.setup_card, self.queue_card):
            layout.removeWidget(widget)
        if side_by_side:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            row.addWidget(self.setup_card, 46)
            row.addWidget(self.queue_card, 54)
            layout.addLayout(row)
        else:
            layout.addWidget(self.setup_card)
            layout.addWidget(self.queue_card, 1)
            layout.setStretch(0, 0)
            layout.setStretch(1, 1)
        # 收起：表格跟随视口长高，正文超高时整页滚动；展开：两栏并排，
        # 左栏参数完整可见，右栏队列仍占满可用高度。
        self.queue.setMinimumHeight(0 if side_by_side else self._base_queue_min_height)

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if directory:
            self.state.output_root = Path(directory)
            self.output_edit.setText(directory)
            self._refresh_estimate()

    def refresh(self) -> None:
        if self.state.output_root is not None:
            self.output_edit.setText(str(self.state.output_root))
        plan = self.state.current_plan
        if plan is None:
            self.queue.setRowCount(0)
            self.queue.setVisible(False)
            self.queue_empty.setVisible(True)
            self.queue_count.set_text("0 集")
            self.summary_title.setText("准备导出")
            self.summary_detail.setText("完成导入、分析与审核后，即可导出分集成片。")
            self.summary_state.set_text("待开始")
            self.summary_state.set_tone("muted")
            self.metric_episodes.setText("—")
            self.metric_duration.setText("—")
            self.metric_size.setText("—")
            self.btn_start.setEnabled(False)
            self._refresh_recap()
            return
        episodes = plan.episodes()
        self.queue.setVisible(True)
        self.queue_empty.setVisible(False)
        self.queue.setRowCount(len(episodes))
        self.queue_count.set_text(f"{len(episodes)} 集")
        for row, episode in enumerate(episodes):
            recap = self._recap_value() if episode.index > 1 else 0
            cells = [
                f"第{episode.index:02d}集",
                "待导出",
                "—",
                format_seconds_brief(episode.duration_seconds + recap),
                f"片头含前情回顾 {recap} 秒" if recap else "",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (0, 1, 2, 3):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                self.queue.setItem(row, column, item)
            status = QTableWidgetItem(cells[1])
            status.setForeground(QColor(qcolor(current_mode(), "TEXT_MUTED")))
            self.queue.setItem(row, 1, status)
        self.btn_start.setEnabled(True)
        self._refresh_summary(plan)
        self.summary_state.set_text("待开始")
        self.summary_state.set_tone("muted")
        self.metric_episodes.setText(f"{len(episodes)} 集")
        recap_total = self._recap_value() * max(0, len(episodes) - 1)
        total = sum((ep.duration_seconds for ep in episodes), episodes[0].duration_seconds * 0)
        total += recap_total
        self.metric_duration.setText(_format_duration(total))
        self._refresh_estimate()
        self._refresh_recap()

    def _refresh_estimate(self) -> None:
        media = self.state.media
        if media is None:
            self.metric_size.setText("未知")
            return
        plan = self.state.current_plan
        recap_total = self._recap_value() * max(0, (plan.episode_count - 1) if plan else 0)
        if not media.bit_rate:
            self.metric_size.setText("未知")
            return
        seconds = float(media.timeline_duration) + recap_total
        estimated = media.bit_rate * seconds / 8 * 1.1
        self.metric_size.setText(f"约 {estimated / 1024 / 1024:.0f} MB")

    def _sync_codec_fields(self) -> None:
        """编码方式联动：原生复制 / GPU / CPU 下哪些参数还有意义。"""
        codec = self.codec_combo.currentData()
        lossless = codec == "fast_copy"
        gpu = codec == "h264_nvenc"
        self.crf_spin.setEnabled(not lossless)
        self.preset_combo.setEnabled(not lossless and not gpu)
        self.audio_bitrate_combo.setEnabled(not lossless)
        self._audio_field.setEnabled(not lossless)
        self._crf_field.set_label(
            "质量 CQ（越小越清晰）" if gpu else "CRF（越小越清晰）"
        )
        if lossless:
            self.codec_hint.setText(
                "视频与音频原样复制，画质、音质和码率保持源片不变。"
                "为保证各集不重叠，切点会吸附到最近的关键帧，导出前会显示调整明细。"
            )
        elif gpu:
            self.codec_hint.setText(
                "会重新编码视频，可能改变画质、码率和文件大小；"
                "NVIDIA 硬件编码速度更快，质量由 CQ 控制。"
            )
        else:
            self.codec_hint.setText(
                "会重新编码视频与音频，可能改变画质、音质和文件大小。"
            )
        self._refresh_recap()
        self._refresh_summary()

    def _refresh_summary(self, plan=None) -> None:
        """概览文案跟着编码方式走；默认必须是“不改变画质 / 音质 / 码率”。"""
        plan = plan or self.state.current_plan
        if plan is None:
            return
        self.summary_title.setText(f"准备导出 {plan.episode_count} 集成片")
        self.summary_detail.setText(
            f"方案 v{plan.version} · {self._codec_summary()}；"
            "导出时逐集校验帧数。"
        )

    def _codec_summary(self) -> str:
        """概览里的一句话编码说明；默认必须是原生、不改变音画。"""
        codec = self.codec_combo.currentData()
        if codec == "fast_copy":
            base = "原生编码，不重新编码（画质 / 音质 / 码率不变）"
            if self.recap_toggle.isChecked() and (self.state.current_plan or None):
                return base + f"；前情至少 {self.recap_seconds.value()} 秒/集"
            return base
        if codec == "h264_nvenc":
            return "NVENC GPU 重新编码"
        return "x264 CPU 重新编码"

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
        self.summary_state.set_text("导出中")
        self.summary_state.set_tone("primary")
        self.summary_title.setText("正在导出分集成片")
        self.summary_detail.setText("正在准备 FFmpeg 任务；逐集完成后会更新下方面板。")
        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("取消导出")

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
            recap_seconds=self._recap_value(),
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

        if self._recap_value():
            detail += (
                f"\n\n第 2 集起会带上上一集片尾至少 {self._recap_value()} 秒作前情回顾；"
                "原生编码的回顾起点也会吸附到关键帧，实际时长可能更长。"
            )

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
        self.btn_cancel.setEnabled(True)

    def _on_cancel_clicked(self) -> None:
        """导出中取消任务；空闲时关闭整页。"""
        if self.worker and self.worker.isRunning():
            self.btn_cancel.setText("取消导出")
            self._cancel()
            return
        self.close_requested.emit()

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
        if result.recap_seconds:
            note += f"；片头含前情回顾 {float(result.recap_seconds):.1f} 秒"
        if result.warnings:
            note = note + "；" + "；".join(result.warnings)
        self.queue.setItem(row, 4, QTableWidgetItem(note))

        if result.success and result.output_path:
            self.state.exported[result.episode_index] = (result.output_path, plan.version)

        self.summary_state.set_text("导出中")
        self.summary_state.set_tone("primary")
        self.summary_title.setText("正在导出分集成片")
        self.summary_detail.setText(
            f"第{result.episode_index:02d}集{'完成' if result.success else '失败'}；"
            "其余集按顺序处理。"
        )
        total = max(1, self.queue.rowCount())
        done = sum(1 for r in range(self.queue.rowCount())
                   if self.queue.item(r, 1) and self.queue.item(r, 1).text() in {"成功", "失败"})
        self.progress.setValue(int(done * 100 / total))

    def _on_completed(self, batch) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("关闭")
        self.btn_retry.setEnabled(bool(batch.failed))
        self.progress.setValue(100)
        if batch.cancelled:
            self.summary_state.set_text("已取消")
            self.summary_state.set_tone("warn")
            self.summary_title.setText("导出已取消")
            self.summary_detail.setText(batch.summary())
        elif batch.failed:
            self.summary_state.set_text("部分失败")
            self.summary_state.set_tone("danger")
            self.summary_title.setText("部分集导出失败")
            self.summary_detail.setText(batch.summary())
        else:
            self.summary_state.set_text("已完成")
            self.summary_state.set_tone("success")
            self.summary_title.setText("导出完成")
            self.summary_detail.setText(
                f"{batch.summary()} · 输出目录：{batch.plan_directory}"
            )
        if batch.cancelled:
            message.information(self, "已取消", batch.summary())
            return
        text = batch.summary() + f"\n输出目录：{batch.plan_directory}"
        recaps = [
            result
            for result in batch.succeeded
            if result.recap_seconds > 0
        ]
        if recaps:
            values = "、".join(
                f"第{result.episode_index:02d}集 {float(result.recap_seconds):.1f}s"
                for result in recaps
            )
            text += f"\n前情回顾：{values}"
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
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("关闭")
        self.summary_state.set_text("导出失败")
        self.summary_state.set_tone("danger")
        self.summary_title.setText("导出未能开始")
        self.summary_detail.setText(text)
        message.critical(self, "导出失败", text)

    def can_continue(self) -> tuple[bool, str]:
        if self.state.output_root is None:
            return False, "请先选择输出目录。"
        return True, ""
