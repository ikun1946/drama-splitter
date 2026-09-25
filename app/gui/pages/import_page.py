"""页 1 导入：导入源视频、显示媒体信息、选择音轨、列出兼容性问题。"""

from __future__ import annotations


from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.ffmpeg import check_encoders, locate_ffmpeg
from ...core.probe import (
    check_compatibility,
    describe_media,
)
from ..state import ProjectState
from ..widgets import (
    Card,
    DropZone,
    IssueList,
    StatusPill,
)
from ..workers import (
    ProbeWorker,
)
from ._common import (
    register_worker,
)


__all__ = ["ImportPage"]


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
        # 换片时恢复 hero 形态，让「拖入 / 点选」的入口重新显眼
        self.drop_zone.set_compact(False)

        self._worker = ProbeWorker(self.state.binaries, path, self)
        self._worker.completed.connect(self._on_probed)
        self._worker.failed.connect(self._on_failed)
        register_worker(self, "probe", self._worker)
        self._worker.start()

    def _on_probed(self, info, issues) -> None:
        self.state.media = info
        self.status.setText("探测完成。")
        self.summary.setText(describe_media(info))
        # 导入完成后收掉 hero 拖放区：它的使命已经完成，140px 的高度会把
        # 下方的分析区顶出可视范围（工作台是导入 + 分析同屏的一页）。
        self.drop_zone.set_compact(True)

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
