"""首次运行向导：自动下载 FFmpeg，或选择用户已有的安装。

为什么在 MainWindow 之前弹出
----------------------------
预览解码器在 ``StageView.__init__`` 里就要求 FFmpeg 就绪，缺工具链时主窗口根本
构造不出来。把配置放在主窗口之前，既避免启动崩溃，也能在真正进入工作台前把
前提条件解决掉。

自动化约定
----------
生产代码在 ``run.py`` / 菜单处理器里 ``exec()``；测试只构造 + ``show()``，并用
``auto_start=False`` 跳过真实下载（避免在测试里下 100+ MB）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import AppConfig
from ...core.ffmpeg_setup import (
    managed_bin,
    remember_ffmpeg_dir,
    validate_ffmpeg_dir,
)
from ..widgets import Card
from ..workers import FfmpegSetupWorker
from .base import DialogShell

__all__ = ["FfmpegSetupDialog"]

def _human(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024 / 1024:.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"

class FfmpegSetupDialog(DialogShell):
    """首次运行 / 环境自检触发的 FFmpeg 配置向导。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config: AppConfig | None = None,
        auto_start: bool = True,
    ) -> None:
        super().__init__("配置运行环境", parent, width=760, height=520)

        self._config = config
        self._worker: FfmpegSetupWorker | None = None
        self._auto_start = auto_start
        self._total = 0

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 12, 16, 0)
        layout.setSpacing(10)

        headline = QLabel("首次运行需要先配置 FFmpeg")
        headline.setProperty("role", "subtitle")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        intro = QLabel(
            "FFmpeg 是导入、精确切分、导出与镜头检测的前提。安装包不捆绑它，"
            "程序现在会自动下载官方构建并放到应用目录，无需你手动安装或改 PATH。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        detail = QLabel(
            f"下载约 115–200 MB（取决于源），解压后占用约 0.5 GB；"
            f"目标目录：{managed_bin()}"
        )
        detail.setProperty("role", "subtitle")
        detail.setWordWrap(True)
        layout.addWidget(detail)

        card = Card("自动配置")
        self.status = QLabel("准备开始…")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        card.body_layout.addWidget(self.status)
        card.body_layout.addWidget(self.progress)
        layout.addWidget(card)

        manual_hint = QLabel(
            "已经装过 FFmpeg？可以跳过下载，直接选择 ffmpeg.exe 所在目录；"
            "离线环境也可以这样做。"
        )
        manual_hint.setProperty("role", "subtitle")
        manual_hint.setWordWrap(True)
        layout.addWidget(manual_hint)
        layout.addStretch(1)
        self.add_body(body)

        self.footer_hint.setText(
            "下载源：BtbN（win64 GPL）/ gyan.dev（release essentials）。"
            "FFmpeg 为第三方 GPL 构建，仅在本机使用。"
        )
        self.footer_hint.setWordWrap(True)

        self.btn_cancel = QPushButton("取消下载")
        self.btn_cancel.setMinimumHeight(32)
        self.btn_cancel.clicked.connect(self._cancel_download)
        self.add_footer(self.btn_cancel)

        self.btn_retry = QPushButton("重试下载")
        self.btn_retry.setMinimumHeight(32)
        self.btn_retry.clicked.connect(self.start_download)
        self.btn_retry.setVisible(False)
        self.add_footer(self.btn_retry)

        self.btn_choose = QPushButton("选择已有 FFmpeg…")
        self.btn_choose.setMinimumHeight(32)
        self.btn_choose.clicked.connect(self._choose_existing)
        self.add_footer(self.btn_choose)

        self.btn_quit = QPushButton("退出")
        self.btn_quit.setMinimumHeight(32)
        self.btn_quit.clicked.connect(self.reject)
        self.add_footer(self.btn_quit)
        self.install_footer()

        if self._auto_start:
            QTimer.singleShot(0, self.start_download)

    # ---- 下载 ----------------------------------------------------------

    def start_download(self) -> None:
        if self._worker is not None:
            return
        self.btn_retry.setVisible(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_choose.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.status.setText("正在下载 FFmpeg…")

        worker = FfmpegSetupWorker(parent=self)
        worker.progress.connect(self._on_progress)
        worker.finished_with.connect(self._on_finished)
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        self._total = total
        if total > 0:
            if self.progress.maximum() != total:
                self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
            self.status.setText(f"正在下载 FFmpeg… {_human(done)} / {_human(total)}")
        else:
            self.status.setText(f"正在下载 FFmpeg… {_human(done)}")

    def _on_finished(self, result) -> None:
        self._worker = None
        self.btn_cancel.setVisible(False)
        self.btn_choose.setEnabled(True)
        if result.cancelled:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.status.setText("下载已取消。可以重试，或选择已有 FFmpeg。")
            self.btn_retry.setVisible(True)
            return
        if not result.ok:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.status.setText(f"配置失败：{result.message}")
            self.btn_retry.setVisible(True)
            return
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        version = f"（{result.version}）" if result.version else ""
        self.status.setText(f"FFmpeg 已配置完成{version}：{result.directory}")
        self.accept()

    def _on_worker_failed(self, message: str) -> None:
        self._worker = None
        self.btn_cancel.setVisible(False)
        self.btn_choose.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText(f"配置失败：{message}")
        self.btn_retry.setVisible(True)

    def _cancel_download(self) -> None:
        if self._worker is None:
            return
        self.btn_cancel.setEnabled(False)
        self.status.setText("正在取消…（当前下载结束后停下）")
        self._worker.cancel()

    # ---- 手选已有 FFmpeg -----------------------------------------------

    def _choose_existing(self) -> None:
        if self._worker is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 ffmpeg.exe",
            "",
            "ffmpeg.exe (ffmpeg.exe);;可执行文件 (*.exe);;所有文件 (*)",
        )
        if not path:
            return
        directory = Path(path).parent
        ok, message = validate_ffmpeg_dir(directory)
        if not ok:
            QMessageBox.warning(self, "FFmpeg 不可用", message)
            return
        self._remember(directory)
        self.status.setText(f"已使用现有 FFmpeg：{directory}（{message}）")
        self.accept()

    def _remember(self, directory: Path) -> None:
        if self._config is None:
            remember_ffmpeg_dir(directory)
            return
        self._config.ffmpeg_dir = str(directory)
        self._config.save()

    # ---- 收尾 ----------------------------------------------------------

    def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        worker.wait(5000)

    def reject(self) -> None:  # noqa: D102 - QDialog 的取消入口（Esc / 退出）
        self._stop_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: D102
        self._stop_worker()
        super().closeEvent(event)
