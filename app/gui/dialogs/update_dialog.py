"""「检查更新」对话框。

状态机
------
``idle → checking → up_to_date | available → downloading → ready | failed | cancelled``

两条不同的失败路径要说不同的话
---------------------------
启动时的静默检查失败**什么都不做**（弹「检查更新失败」是纯打扰）；
手动打开这个对话框时，任何结果都必须显示出来，包括失败。

免安装版的分支
--------------
免安装 zip 里没有安装器可启动（``unins000.exe`` 不存在），此时"下载并安装"
要改成"下载后打开所在目录"，让用户自己换文件——不能假装能自动升级。
"""

from __future__ import annotations

import tempfile
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..panes._common import register_worker
from ..widgets import message
from ..workers import UpdateCheckWorker, UpdateDownloadWorker
from .base import DialogShell

__all__ = ["UpdateDialog"]


class UpdateDialog(DialogShell):
    def __init__(self, parent: QWidget | None = None, *, current_version: str = "") -> None:
        super().__init__("检查更新", parent, width=760, height=560)
        self._version = current_version
        self._info = None
        self._downloaded: Path | None = None
        self._worker = None
        #: idle / checking / up_to_date / available / downloading / ready / failed
        self._state = "idle"

        self.status = QLabel("", self)
        self.status.setWordWrap(True)

        self.notes = QScrollArea(self)
        self.notes.setWidgetResizable(True)
        self.notes_body = QLabel("", self.notes)
        self.notes_body.setWordWrap(True)
        self.notes_body.setProperty("role", "subtitle")
        self.notes_body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.notes.setWidget(self.notes_body)
        self.notes.setVisible(False)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.btn_cancel = QPushButton("取消下载", self)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.btn_cancel)
        progress_host = QWidget(self)
        progress_host.setLayout(progress_row)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)
        body.addWidget(self.status)
        body.addWidget(self.notes, 1)
        body.addWidget(progress_host)
        holder = QWidget(self)
        holder.setLayout(body)
        self.add_body(holder)

        self.btn_primary = QPushButton("检查", self)
        self.btn_primary.setProperty("role", "primary")
        self.btn_primary.clicked.connect(self._on_primary)
        self.btn_release = QPushButton("发布页", self)
        self.btn_release.clicked.connect(self._open_release)
        self.btn_skip = QPushButton("跳过这一版", self)
        self.btn_skip.setToolTip("这一版不再提示；出了更新的版本仍会提示")
        self.btn_skip.clicked.connect(self._skip_version)
        self.add_footer(self.btn_release)
        self.add_footer(self.btn_skip)
        self.add_footer(self.btn_primary)
        self.btn_skip.setVisible(False)

        self.footer_hint.setText(
            "只在本程序手动触发或启动时后台检查时联网，不会静默下载或安装任何东西。"
        )
        self.add_close_button()
        self.install_footer()
        self._set_idle()

    # ---- 状态 ----------------------------------------------------------

    def _set_idle(self) -> None:
        self._state = "idle"
        self.status.setText(f"当前版本 {self._version}。点「检查」去 GitHub 看看有没有新版。")
        self.btn_primary.setText("检查")
        self.btn_primary.setEnabled(True)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.notes.setVisible(False)
        self.btn_skip.setVisible(False)

    def _skip_version(self) -> None:
        """记下"这一版不要再提"，写进 config.json 的 extra。

        只记版本号不记"永久不再提示"：出了更新的版本仍该提示，否则用户会
        卡在一个早就不合适的版本上却再也看不到提醒。
        """
        if self._info is None:
            return
        try:
            from app.core.app_config import AppConfig

            config = AppConfig.load()
            config.extra["update_dismissed"] = self._info.version
            config.save()
        except Exception as exc:  # noqa: BLE001 - 跳过失败不该让用户没法继续
            message.warning(self, "没能记住", f"跳过这一版的记录没有保存：{exc}")
        self.close()

    def _set_checking(self) -> None:
        self._state = "checking"
        self.status.setText("正在检查…")
        self.btn_primary.setEnabled(False)
        self.btn_primary.setText("检查中…")
        self.progress.setRange(0, 0)  # 不确定进度
        self.progress.setVisible(True)

    def _set_failed(self, why: str) -> None:
        self._state = "failed"
        self.status.setText(why)
        self.btn_primary.setText("重试")
        self.btn_primary.setEnabled(True)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_skip.setVisible(False)

    def _set_available(self) -> None:
        info = self._info
        self._state = "available"
        self.status.setText(f"发现新版本 {info.version}。")
        self.notes_body.setText(info.notes or "（这次发布没有写说明）")
        self.notes.setVisible(True)
        self.btn_primary.setText("下载并安装" if self._installed else "下载")
        self.btn_primary.setEnabled(True)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_skip.setVisible(True)

    def _set_up_to_date(self, why: str) -> None:
        self._state = "up_to_date"
        self.status.setText(why)
        self.btn_primary.setText("再检查一次")
        self.btn_primary.setEnabled(True)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)

    @property
    def _installed(self) -> bool:
        from app.core.updater import installed_copy

        return installed_copy()

    # ---- 动作 ----------------------------------------------------------

    def start_check(self) -> None:
        """发起一次检查。菜单与自动流程都走这里。"""
        self._set_checking()
        worker = UpdateCheckWorker(self._version, parent=self)
        worker.checked.connect(self._on_checked)
        self._worker = worker
        register_worker(self, "update_check", worker)
        worker.start()

    def _on_checked(self, result) -> None:
        self._worker = None
        if not result.ok:
            self._set_failed(result.message or "检查更新失败。")
            return
        if not result.has_update:
            self._set_up_to_date(result.message or "当前已是最新版本。")
            return
        self._info = result.info
        self._set_available()

    def _on_primary(self) -> None:
        """主按钮在每个状态下含义不同，用显式状态分派。

        之前靠 ``_downloaded is not None`` 推断是错的：下载完成后点主按钮会
        重新去检查，而不是执行安装。
        """
        if self._state in ("downloading", "checking"):
            return
        if self._state == "ready":
            self._apply_update()
        elif self._state == "available":
            self._start_download()
        else:  # idle / up_to_date / failed —— 都是"再查一次"
            self._downloaded = None
            self.start_check()

    def _start_download(self) -> None:
        asset = self._info.asset_for(self._installed)
        if asset is None:
            self._set_failed("该版本没有可下载的文件。")
            return
        url, size, digest = asset
        target = Path(tempfile.gettempdir()) / f"drama-splitter-{self._info.version}.exe"
        if not self._installed:
            target = target.with_suffix(".zip")

        self._state = "downloading"
        self.status.setText(f"正在下载 {self._info.version}（{size / 1024 / 1024:.0f} MB）…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_primary.setEnabled(False)

        worker = UpdateDownloadWorker(url, target, digest=digest, size=size, parent=self)
        worker.finished_with.connect(self._on_downloaded)
        self._worker = worker
        register_worker(self, "update_download", worker)
        worker.start()

    def _on_downloaded(self, outcome) -> None:
        self._worker = None
        self.btn_cancel.setVisible(False)
        if not outcome.ok:
            self._set_failed(outcome.message or "下载失败。")
            return
        self._downloaded = outcome.path
        self._state = "ready"
        self.progress.setValue(100)
        if self._installed:
            self.status.setText("已下载并通过完整性校验。点下面的按钮安装。")
            self.btn_primary.setText("立即重启安装")
        else:
            self.status.setText(
                f"已下载并通过完整性校验：{outcome.path}\n"
                "这是免安装版，没有安装器可用——请自己把新文件替换到旧版本所在目录。"
            )
            self.btn_primary.setText("打开所在目录")
        self.btn_primary.setEnabled(True)

    def _apply_update(self) -> None:
        from app.core.updater import launch_installer

        if self._downloaded is None:
            return
        if not self._installed:
            # 免安装版没有安装器可启动，只能把目录打开让用户自己换
            import os

            os.startfile(str(self._downloaded.parent))  # noqa: S606
            return
        ok, why = launch_installer(self._downloaded)
        if not ok:
            self._set_failed(why)
            return
        message.information(
            self,
            "开始安装",
            why + "\n\n本程序会自行关闭，装完重新打开即可。",
        )
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.status.setText("已取消下载。")

    def _open_release(self) -> None:
        from app.core.updater import RELEASES_PAGE

        target = self._info.url if self._info is not None else RELEASES_PAGE
        try:
            webbrowser.open(target)
        except Exception:  # noqa: BLE001
            message.warning(self, "打不开", f"请手动访问：\n{target}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._worker is not None:
            self._worker.cancel()
        super().closeEvent(event)
