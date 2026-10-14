"""左栏「素材」pane：素材信息网格、主对白音轨、接下来三步。

只放"看一眼就知道素材是什么"的信息。兼容性问题不在这里重复列一遍——它们
统一进右栏检查器的「问题」页签（同一份 ``check_compatibility`` 结果原先在
导入区与检查器各显示一次，是明显的冗余）。

版式（v2）：
- 信息按「标签 / 值」两列排（原来三张卡片各留一大片空白，内容还被裁）；
- 长路径折行显示，完整路径进 tooltip——QLabel 的 wordWrap 只在空格处断，
  而路径没有空格，所以在分隔符后插零宽空格造断点（实测最小宽 252px → 84px）；
- 格式提示只在**未导入**时出现，导入后不再占位置；
- 末尾补一张「接下来」三步卡，把第一次使用的人该点哪里写清楚。

拖放导入的 hero 区也不在这里：它在"未导入"阶段占据中栏，属于工作台的版式，
不属左栏（见 ``app/gui/workbench.py`` 的空态视图）。

对外只通过公开信号汇报结果（``probed`` / ``probe_failed``），不靠外部包装
私有回调——原先工作台把 ``_on_probed`` 换成自己的包装函数来同步顶栏，
一旦有人给控件改名就会静默失效。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
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
from ..widgets import Card
from ..widgets import NoWheelComboBox
from ..widgets import message
from ..workers import ProbeWorker
from ._common import register_worker

__all__ = ["ImportPane", "human_size"]

#: 信息网格的行（标签, 内部键）——顺序就是核对素材时的顺序
INFO_ROWS: tuple[tuple[str, str], ...] = (
    ("文件", "file"),
    ("路径", "path"),
    ("画面", "resolution"),
    ("帧率", "fps"),
    ("编码", "codec"),
    ("时长", "duration"),
    ("大小", "size"),
    ("音轨", "audio"),
)

#: 「接下来」三步——第一次用的人照着点就行
STEP_TEXTS: tuple[str, ...] = (
    "选定源片",
    "点右下角「开始分析」，AI 给出切点候选",
    "在预览里逐条核对、锁定切点",
)


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


def breakable_path(path: str) -> str:
    """在分隔符后插零宽空格，让长路径能在 QLabel 里折行。

    QLabel 的 wordWrap 只在空格/连字符处断行，而 Windows 路径里没有空格：
    实测不插的话最小宽度 252px（栏宽只有 360，减掉内边距就装不下），插了之后
    最小宽度降到 84px、按栏宽自然折成 2–3 行。零宽字符不影响视觉，也只在
    展示用文本里出现（tooltip 仍是原样的路径）。
    """
    return path.replace("\\", "\\\u200b").replace("/", "/\u200b")


class ImportPane(QWidget):
    """素材信息 / 主对白音轨 / 接下来。"""

    probed = Signal(object, object)      # (MediaInfo, issues)
    probe_failed = Signal(str)

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: ProbeWorker | None = None
        self._full_path = ""

        # ---- 素材：信息网格 -------------------------------------------
        self.file_card = Card("素材", parent=self, accent="primary")

        self.path_label = QLabel("尚未选择源视频", self.file_card)
        self.path_label.setProperty("role", "mono")
        self.path_label.setWordWrap(True)
        self.path_label.setToolTip("尚未选择源视频")

        self._fields: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        for row, (label, key) in enumerate(INFO_ROWS):
            tag = QLabel(label, self.file_card)
            tag.setProperty("role", "muted")
            value = self.path_label if key == "path" else QLabel("—", self.file_card)
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
        self.audio_combo = NoWheelComboBox()
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        self.audio_hint = QLabel("导入后列出可选音轨。")
        self.audio_hint.setProperty("role", "muted")
        self.audio_hint.setWordWrap(True)
        self._audio_card = Card("主对白音轨", parent=self)
        self._audio_card.body_layout.addWidget(self.audio_combo)
        self._audio_card.body_layout.addWidget(self.audio_hint)

        # ---- 接下来（三步）--------------------------------------------
        self._steps_card = Card("接下来", parent=self)
        self._step_labels: list[QLabel] = []
        for number, text in enumerate(STEP_TEXTS, start=1):
            label = QLabel(f"{number}  {text}", self._steps_card)
            label.setWordWrap(True)
            # 第 1 步做完只是加个后缀；第 2 步是"下一个动作"，用正常文字色抢一下注意力
            label.setProperty("role", "muted" if number != 2 else "")
            self._step_labels.append(label)
            self._steps_card.body_layout.addWidget(label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(self.file_card)
        outer.addWidget(self._audio_card)
        outer.addWidget(self._steps_card)
        outer.addStretch(1)

    # ---- 行为 ----------------------------------------------------------

    def _set_path(self, path: str) -> None:
        """把完整路径折行铺在「路径」那一格；tooltip 里放原样路径。"""
        self._full_path = str(path or "")
        if not self._full_path:
            self.path_label.setText("尚未选择源视频")
            self.path_label.setToolTip("尚未选择源视频")
            return
        self.path_label.setText(breakable_path(self._full_path))
        self.path_label.setToolTip(self._full_path)

    def full_path(self) -> str:
        """完整路径（界面里可能折行显示）。"""
        return self._full_path

    def _set_field(self, key: str, text: str) -> None:
        self._fields[key].setText(text or "—")

    def _fill_fields(self, info) -> None:
        """把探测结果填进信息网格。"""
        video = info.video
        self._set_field("file", Path(info.path).name)
        if video is not None:
            self._set_field("resolution", f"{video.width} × {video.height}")
            fps = video.nominal_fps
            self._set_field("fps", f"{float(fps):.3f} fps" if fps else "—")
            codec = (video.codec_name or "").upper()
            parts = [part for part in (codec, video.pix_fmt or "") if part]
            self._set_field("codec", " · ".join(parts))
        else:
            self._set_field("resolution", "无视频轨")
            self._set_field("fps", "—")
            self._set_field("codec", "—")
        self._set_field("duration", format_timecode(info.timeline_duration))
        self._set_field("size", human_size(info.size_bytes))
        count = len(info.audio_tracks)
        self._set_field("audio", f"{count} 条" if count else "无音轨")

    def _mark_first_step_done(self, done: bool) -> None:
        label = self._step_labels[0]
        label.setText(
            f"1  {STEP_TEXTS[0]}（已完成）" if done else f"1  {STEP_TEXTS[0]}"
        )

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
