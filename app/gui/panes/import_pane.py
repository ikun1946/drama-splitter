"""左栏「素材」pane：源文件、媒体摘要、主对白音轨。

只放"看一眼就知道素材是什么"的信息。兼容性问题不在这里重复列一遍——它们
统一进右栏检查器的「问题」页签（同一份 ``check_compatibility`` 结果原先在
导入区与检查器各显示一次，是明显的冗余）。

拖放导入的 hero 区也不在这里：它在"未导入"阶段占据中栏，属于工作台的版式，
不属左栏（见 ``app/gui/workbench.py`` 的空态视图）。

对外只通过公开信号汇报结果（``probed`` / ``probe_failed``），不靠外部包装
私有回调——原先工作台把 ``_on_probed`` 换成自己的包装函数来同步顶栏，
一旦有人给控件改名就会静默失效。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.ffmpeg import check_encoders, locate_ffmpeg
from ...core.probe import check_compatibility, describe_media
from ..state import ProjectState
from ..widgets import Card
from ..widgets import NoWheelComboBox
from ..widgets import message
from ..workers import ProbeWorker
from ._common import register_worker

__all__ = ["ImportPane"]


class ImportPane(QWidget):
    """源文件 / 媒体摘要 / 主对白音轨。"""

    probed = Signal(object, object)      # (MediaInfo, issues)
    probe_failed = Signal(str)

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.worker: ProbeWorker | None = None

        # ---- 源文件 ----
        # 路径用 QLabel + 手写省略号，不用 QLineEdit：后者装不下真实路径时会
        # 横向滚动，光标在末尾于是**只显示路径尾巴**（截图里是「1s 25fps.mp4」），
        # 看起来像文件被截断了。栏宽 320px 装不下完整路径，头部省略才是诚实的。
        self.path_label = QLabel("尚未选择源视频")
        self.path_label.setProperty("role", "mono")
        self.path_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.path_label.setToolTip("尚未选择源视频")
        self._full_path = ""
        self.btn_browse = QPushButton("更换视频…")
        self.btn_browse.setProperty("role", "ghost")
        self.btn_browse.clicked.connect(self.choose_file)
        self.status = QLabel("")
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)

        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        file_row.addWidget(self.path_label, 1)
        file_row.addWidget(self.btn_browse)
        file_card = Card("源文件", parent=self, accent="primary")
        file_card.body_layout.addLayout(file_row)
        formats = QLabel("支持 .mp4 / .mov / .mkv / .avi / .ts / .m4v，也可直接拖入")
        # 漏了 setWordWrap 就会在这一行被硬裁——左栏只有 256px 可用宽度
        formats.setWordWrap(True)
        formats.setProperty("role", "muted")
        file_card.body_layout.addWidget(formats)
        self.file_card = file_card

        # ---- 媒体摘要 ----
        self.summary = QLabel("尚未导入。")
        self.summary.setWordWrap(True)
        self.summary.setProperty("role", "muted")
        info_card = Card("媒体信息", parent=self)
        info_card.body_layout.addWidget(self.summary)
        self._info_card = info_card

        # ---- 主对白音轨 ----
        self.audio_combo = NoWheelComboBox()
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        self.audio_hint = QLabel("导入后列出可选音轨。")
        self.audio_hint.setProperty("role", "muted")
        self.audio_hint.setWordWrap(True)
        audio_card = Card("主对白音轨", parent=self)
        audio_card.body_layout.addWidget(self.audio_combo)
        audio_card.body_layout.addWidget(self.audio_hint)
        self._audio_card = audio_card

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(file_card)
        outer.addWidget(info_card)
        outer.addWidget(audio_card)
        outer.addWidget(self.status)
        outer.addStretch(1)

    # ---- 行为 ----------------------------------------------------------

    def _set_path(self, path: str) -> None:
        """显示**头部**省略的路径，完整路径进 tooltip。

        省略号放在中间而不是末尾：路径开头是盘符与目录（用户真正需要确认的
        「哪个盘」），文件名在末尾反而看得见。完整值由 ``self._full_path`` 持有。
        """
        self._full_path = str(path)
        if not self._full_path:
            self.path_label.setText("尚未选择源视频")
            self.path_label.setToolTip("尚未选择源视频")
            return
        self.path_label.setToolTip(self._full_path)
        metrics = self.path_label.fontMetrics()
        width = max(40, self.path_label.width() or 180)
        self.path_label.setText(
            metrics.elidedText(self._full_path, Qt.TextElideMode.ElideMiddle, width)
        )

    def full_path(self) -> str:
        """完整路径（界面只显示省略版）。"""
        return self._full_path

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
        self.state.reset_media()
        self.worker = ProbeWorker(self.state.binaries, path, self)
        self.worker.completed.connect(self._on_probed)
        self.worker.failed.connect(self._on_failed)
        register_worker(self, "probe", self.worker)
        self.worker.start()

    def _on_probed(self, info, issues) -> None:
        self.state.media = info
        self.summary.setText(describe_media(info))

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

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self._full_path:
            self._set_path(self._full_path)

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