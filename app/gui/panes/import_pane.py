"""左栏「素材」pane：素材信息、主对白音轨、接下来。

只放"看一眼就知道素材是什么"的信息。兼容性问题不在这里重复列一遍——它们
统一进右栏检查器的「问题」页签（同一份 ``check_compatibility`` 结果原先在
导入区与检查器各显示一次，是明显的冗余）。

版式（v3）：
- 文件名升为卡片标题：长名按显示宽度截断（标题 QLabel 不换行，不截断就会把
  卡片最小宽度顶过栏宽、被 ``scrollable()`` 静默裁切），完整路径进 tooltip；
- 信息网格 5 行：画面 / 帧率 / 时长 / 大小 / 音轨——文件名与路径不再各占一行，
  编码并入音轨摘要，避免"8 行里三行说的是同一个文件"；
- 格式提示只在**未导入**时出现，导入后不再占位置；
- 「接下来」压成一行进度提示（三步用 → 连起来），详细说明进 tooltip。

拖放导入的 hero 区也不在这里：它在"未导入"阶段占据中栏，属于工作台的版式，
不属左栏（见 ``app/gui/workbench.py`` 的空态视图）。

对外只通过公开信号汇报结果（``probed`` / ``probe_failed``），不靠外部包装
私有回调——原先工作台把 ``_on_probed`` 换成自己的包装函数来同步顶栏，
一旦有人给控件改名就会静默失效。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.ffmpeg import check_encoders, locate_ffmpeg
from ...core.probe import check_compatibility
from ...core.timebase import format_timecode
from ..state import ProjectState
from ..theme import FONT_SIZE_TITLE, SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XS
from ..widgets import Card
from ..widgets import NoWheelComboBox
from ..widgets import message
from ..workers import ProbeWorker
from ._common import register_worker

__all__ = ["ImportPane", "elide_name", "human_size"]

#: 信息网格的行（标签, 内部键）——顺序就是核对素材时的顺序
INFO_ROWS: tuple[tuple[str, str], ...] = (
    ("画面", "resolution"),
    ("帧率", "fps"),
    ("时长", "duration"),
    ("大小", "size"),
    ("音轨", "audio"),
)

#: 「接下来」三步——第一次用的人照着点就行（卡片里连成一行，详见 tooltip）
#: 每步只留两个字的动词：三步连成一行要放进 ~240px 的卡片正文宽度，
#: "开始分析 / 核对切点" 这类全称会把这一行挤成两行（完整说法在 tooltip 里）。
STEP_TEXTS: tuple[str, ...] = (
    "选定源片",
    "分析",
    "核对",
)

#: 卡片标题里文件名的像素预算。标题 QLabel 不换行，超出就会把整张卡的最小
#: 宽度顶过栏宽（``scrollable()`` 关掉横条后就静默裁切）。
#: 340 栏里标题可用宽度约 280px，留出余量给更窄的拖动位置。
TITLE_BUDGET_PX = 250


def human_size(num_bytes: int) -> str:
    """字节数转成人读的大小（B / KB / MB / GB）。"""
    size = float(num_bytes or 0)
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"


def elide_name(name: str, max_px: int, metrics: QFontMetrics) -> str:
    """把长文件名截成 "头部…扩展名"，按**实际字体度量**算宽度。

    字符数估算不可靠：标题是 15pt，一个西文字符就要 ~19px，按"22 个字符预算"
    截出来的名字仍有 360px，照样把卡片顶宽。
    """
    if metrics.horizontalAdvance(name) <= max_px:
        return name
    stem, dot, suffix = name.rpartition(".")
    tail = f"{dot}{suffix}" if dot and suffix else ""
    if metrics.horizontalAdvance("…" + tail) > max_px // 2:
        tail = ""
    head = name[: len(name) - len(tail)]
    while head and metrics.horizontalAdvance(head + "…" + tail) > max_px:
        head = head[:-1]
    return f"{head.rstrip()}…{tail}" if head else f"…{tail}"


def audio_summary(track) -> str:
    """单条音轨的一行摘要（编码 · 采样率 · 声道）。"""
    layout = track.channel_layout or (
        f"{track.channels} 声道" if track.channels else ""
    )
    parts = [
        track.codec_name.upper() if track.codec_name else "",
        f"{track.sample_rate} Hz" if track.sample_rate else "",
        layout,
    ]
    return " · ".join(part for part in parts if part) or "—"


class ImportPane(QWidget):
    """素材信息 / 主对白音轨 / 接下来。"""

    probed = Signal(object, object)      # (MediaInfo, issues)
    probe_failed = Signal(str)

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: ProbeWorker | None = None
        self._full_path = ""

        # ---- 素材：标题 = 文件名，正文 5 行 ----------------------------
        self.file_card = Card("素材", parent=self)
        self.file_card.setToolTip("尚未选择源视频")

        self._fields: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE_SM)
        grid.setVerticalSpacing(SPACE_XS)
        for row, (label, key) in enumerate(INFO_ROWS):
            tag = QLabel(label, self.file_card)
            tag.setProperty("role", "muted")
            value = QLabel("—", self.file_card)
            if key == "duration":
                # 时间码用等宽（11pt）：13pt 正文里 "00:02:00.000" 要 204px，
                # 加标签列与两层内边距会把卡片顶到 310px（栏 340 的视口只有 330）。
                value.setProperty("role", "mono")
            value.setWordWrap(True)
            grid.addWidget(tag, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self._fields[key] = value
        grid.setColumnStretch(1, 1)
        self.file_card.body_layout.addLayout(grid)

        # 未导入时才提示支持的格式；导入后这一段是废话，不该继续占位置
        self.formats = QLabel(
            "支持 .mp4 / .mov / .mkv / .avi / .ts / .m4v，也可直接拖入", self.file_card
        )
        # 漏了 setWordWrap 就会在这一行被硬裁——左栏只有 256px 可用宽度
        self.formats.setWordWrap(True)
        self.formats.setProperty("role", "muted")
        self.file_card.body_layout.addWidget(self.formats)

        self.btn_browse = QPushButton("选择视频…", self.file_card)
        self.btn_browse.setProperty("role", "ghost")
        self.btn_browse.clicked.connect(self.choose_file)
        browse_row = QHBoxLayout()
        browse_row.setContentsMargins(0, 0, 0, 0)
        browse_row.addWidget(self.btn_browse)
        browse_row.addStretch(1)
        self.file_card.body_layout.addLayout(browse_row)

        self.status = QLabel("", self.file_card)
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)
        self.file_card.body_layout.addWidget(self.status)

        # ---- 主对白音轨 ------------------------------------------------
        # 只留选择器 + 一句说明：编码 / 采样率等统计已经在素材卡的「音轨」行，
        # 这里再列一遍是把同一份信息说两次。
        self.audio_combo = NoWheelComboBox()
        # QComboBox 默认按**最长条目**决定最小宽度：音轨描述
        # 「音轨1 aac 48000Hz mono [und]」实测 484px，会把整张卡顶出 340 栏
        # （横条已关 → 静默裁切）。按固定内容长度算尺寸，长条目在框内省略，
        # 下拉列表里仍是全文。
        self.audio_combo.setSizeAdjustPolicy(
            NoWheelComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.audio_combo.setMinimumContentsLength(8)
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        self.audio_hint = QLabel("导入后列出可选音轨。")
        self.audio_hint.setProperty("role", "muted")
        self.audio_hint.setWordWrap(True)
        self._audio_card = Card("主对白音轨", parent=self)
        self._audio_card.body_layout.addWidget(self.audio_combo)
        self._audio_card.body_layout.addWidget(self.audio_hint)

        # ---- 接下来（一行）--------------------------------------------
        self._steps_card = Card("接下来", parent=self)
        self._steps_label = QLabel(self._steps_text(), self._steps_card)
        self._steps_label.setWordWrap(True)
        self._steps_label.setProperty("role", "muted")
        self._steps_label.setToolTip(
            "选定源片 → 点右下角「开始分析」，AI 给出切点候选 → 在预览里逐条核对、锁定切点"
        )
        # 保留列表形态：外部按 ``_step_labels[0]`` 取第 1 步的状态
        self._step_labels: list[QLabel] = [self._steps_label]
        self._steps_card.body_layout.addWidget(self._steps_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_MD)
        outer.addWidget(self.file_card)
        outer.addWidget(self._audio_card)
        outer.addWidget(self._steps_card)
        outer.addStretch(1)

    # ---- 行为 ----------------------------------------------------------

    def _title_metrics(self) -> QFontMetrics:
        """卡片标题用的字体度量（QSS 里 title 是 15pt 加粗，控件自身字体是 13pt）。"""
        font = self.font()
        font.setPointSizeF(FONT_SIZE_TITLE)
        font.setBold(True)
        return QFontMetrics(font)

    def _set_path(self, path: str) -> None:
        """文件名进卡片标题（按像素截断），完整路径进 tooltip。"""
        self._full_path = str(path or "")
        if not self._full_path:
            self.file_card.set_title("素材")
            self.file_card.setToolTip("尚未选择源视频")
            return
        name = Path(self._full_path).name
        self.file_card.set_title(
            elide_name(name, TITLE_BUDGET_PX, self._title_metrics())
        )
        self.file_card.setToolTip(self._full_path)

    def full_path(self) -> str:
        """完整路径（标题里只显示截断后的文件名）。"""
        return self._full_path

    def _set_field(self, key: str, text: str) -> None:
        self._fields[key].setText(text or "—")

    def _fill_fields(self, info) -> None:
        """把探测结果填进信息网格。"""
        video = info.video
        self._set_path(info.path)
        if video is not None:
            self._set_field("resolution", f"{video.width} × {video.height}")
            fps = video.nominal_fps
            self._set_field("fps", f"{float(fps):.3f} fps" if fps else "—")
        else:
            self._set_field("resolution", "无视频轨")
            self._set_field("fps", "—")
        self._set_field("duration", format_timecode(info.timeline_duration))
        self._set_field("size", human_size(info.size_bytes))
        tracks = info.audio_tracks
        if not tracks:
            self._set_field("audio", "无音轨")
        elif len(tracks) == 1:
            self._set_field("audio", audio_summary(tracks[0]))
        else:
            self._set_field("audio", f"{len(tracks)} 条 · 主轨见下")

    def _steps_text(self, first_done: bool = False) -> str:
        # 一行要放进卡片正文（340 栏里约 268px）。"选定源片（已完成）"这种写法
        # 会折成两行，第一步用"已完成"前缀表达状态，字面仍含用例要的「已完成」。
        first = "选片已完成" if first_done else STEP_TEXTS[0]
        return " → ".join((first, STEP_TEXTS[1], STEP_TEXTS[2]))

    def _mark_first_step_done(self, done: bool) -> None:
        self._steps_label.setText(self._steps_text(done))

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择源视频",
            "",
            "视频文件 (*.mp4 *.mov *.mkv *.avi *.ts *.m4v);;所有文件 (*)",
        )
        if not path:
            return
        self.probe(path)

    def probe(self, path: str) -> None:
        if self.state.binaries is None:
            try:
                binaries = locate_ffmpeg()
                check_encoders(binaries)
                self.state.binaries = binaries
            except Exception as exc:  # noqa: BLE001 - 缺 FFmpeg 时给明确指引
                message.critical(self, "找不到 FFmpeg", str(exc))
                return

        self.status.setText("正在探测媒体信息…")
        self._set_path(path)
        self._mark_first_step_done(False)
        self.state.reset_media()
        self.worker = ProbeWorker(self.state.binaries, path, self)
        self.worker.completed.connect(self._on_probed)
        self.worker.failed.connect(self._on_failed)
        register_worker(self, "probe", self.worker)
        self.worker.start()

    def _on_probed(self, info, issues) -> None:
        self.state.media = info
        self._fill_fields(info)
        self.formats.setVisible(False)
        self.btn_browse.setText("更换视频…")
        self._mark_first_step_done(True)

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
            self.status.setText(
                f"探测完成，但有 {len(blocking)} 项阻断问题，见右侧检查器。"
            )
        else:
            self.status.setText("探测完成，可在「工具 → 分集设置」里指定分集目标。")

        self.probed.emit(info, issues)

    def _on_failed(self, text: str) -> None:
        self.status.setText("探测失败，见右侧检查器。")
        message.critical(self, "探测失败", text)
        self.probe_failed.emit(text)

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