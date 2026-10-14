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
import time
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
    #: 说明区默认折叠高度（约 5 行）与展开高度——整篇 Markdown 糊一屏体验很差
    NOTES_COLLAPSED = 108
    NOTES_EXPANDED = 360

    def __init__(self, parent: QWidget | None = None, *, current_version: str = "") -> None:
        super().__init__("检查更新", parent, width=760, height=560)
        self._version = current_version
        self._info = None
        self._downloaded: Path | None = None
        #: 下载起点（算速度与剩余时间用）与当前镜像前缀（空 = 直连）
        self._download_started = 0.0
        self._source_host = ""
        self._mirror = ""
        #: 免安装版解压出来的目录（没有安装器可启动，只能让用户自己替换）
        self._extracted: Path | None = None
        self._worker = None
        #: idle / checking / up_to_date / available / downloading / ready / failed
        self._state = "idle"
        self._notes_expanded = False

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
        self.btn_notes = QPushButton("展开完整说明", self)
        self.btn_notes.clicked.connect(self._toggle_notes)
        self.btn_notes.setVisible(False)
        self.btn_skip = QPushButton("跳过这一版", self)
        self.btn_skip.setToolTip("这一版不再提示；出了更新的版本仍会提示")
        self.btn_skip.clicked.connect(self._skip_version)
        self.add_footer(self.btn_notes)
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
        self.btn_notes.setVisible(False)
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
        self.btn_notes.setVisible(False)

    def _set_available(self) -> None:
        from app.core.updater import release_headline

        info = self._info
        self._state = "available"
        # 状态行先说清「这版改了什么」——用户第一眼要的是这个，不是一整篇说明
        headline = release_headline(info.notes)
        self.status.setText(
            f"发现新版本 {info.version}：{headline}"
            if headline
            else f"发现新版本 {info.version}。"
        )
        # Markdown 渲染：以前直接 setText，把 ## / ** / ``` 原样糊在界面上。
        # 注意 QLabel 没有 setMarkdown，要显式把文本格式设成 Markdown 再 setText。
        self.notes_body.setTextFormat(Qt.TextFormat.MarkdownText)
        self.notes_body.setText(info.notes or "（这次发布没有写说明）")
        self.notes.setVisible(True)
        self._set_notes_expanded(False)
        self.btn_primary.setText("下载并安装" if self._installed else "下载")
        self.btn_primary.setEnabled(True)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_skip.setVisible(True)

    def _set_notes_expanded(self, expanded: bool) -> None:
        """说明区默认只露几行，想看细节再展开——避免一屏全是字。"""
        self._notes_expanded = expanded
        self.notes.setMaximumHeight(
            self.NOTES_EXPANDED if expanded else self.NOTES_COLLAPSED
        )
        self.btn_notes.setText("收起说明" if expanded else "展开完整说明")
        # 用 isHidden() 而不是 isVisible()：对话框还没 show 时 isVisible() 恒为假，
        # 会把按钮错误地藏起来（离屏测试与延迟显示都会踩到）。
        self.btn_notes.setVisible(not self.notes.isHidden())

    def _toggle_notes(self) -> None:
        self._set_notes_expanded(not self._notes_expanded)

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
        elif self._state == "failed" and self._info is not None:
            # 下载失败/中断后的「继续下载」：直接接着下
            self._start_download()
        else:  # idle / up_to_date / 检查失败 —— 都是"再查一次"
            self._downloaded = None
            self.start_check()

    def _start_download(self) -> None:
        asset = self._info.asset_for(self._installed)
        if asset is None:
            self._set_failed("该版本没有可下载的文件。")
            return
        urls, size, digest = asset
        target = Path(tempfile.gettempdir()) / f"drama-splitter-{self._info.version}.exe"
        if not self._installed:
            target = target.with_suffix(".zip")

        self._state = "downloading"
        self._download_started = time.monotonic()
        self._mirror = ""
        self._source_host = ""
        try:
            from app.core.app_config import AppConfig

            self._mirror = str(AppConfig.load().update_mirror or "")
        except Exception:  # noqa: BLE001 - 配置读不出来就按直连处理
            self._mirror = ""
        from app.core.updater import format_size

        self.status.setText(
            f"正在下载 {self._info.version}（{format_size(size)}{"，经镜像" if self._mirror else ""}）…"
        )
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_primary.setEnabled(False)

        worker = UpdateDownloadWorker(urls, target, digest=digest, size=size, parent=self)
        # 以前这条线是断的（worker 也没上报），进度条从头到尾不动——慢链路上
        # 用户根本看不出是在跑还是卡死了。
        worker.progress.connect(self._on_progress)
        worker.source_changed.connect(self._on_source)
        worker.finished_with.connect(self._on_downloaded)
        self._worker = worker
        register_worker(self, "update_download", worker)
        worker.start()

    def _on_source(self, host: str) -> None:
        """记录这次实际用的下载来源，显示出来——走镜像时要让用户看见。"""
        self._source_host = str(host or "")

    def _on_progress(self, done: int, total: int) -> None:
        """下载进度：进度条 + 「已下载 / 速度 / 剩余」文案。"""
        from app.core.updater import download_status_text

        elapsed = max(0.0, time.monotonic() - self._download_started)
        text, determinate = download_status_text(done, total, elapsed)
        if determinate and total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(done * 100 / total)))
        else:
            # 发布页没给大小时不假装有百分比：转不确定进度，只报已下载量
            self.progress.setRange(0, 0)
        hint = ""
        speed = done / elapsed if elapsed > 3.0 and done else 0.0
        if not self._mirror and 0 < speed < 300 * 1024:
            hint = "\n直连较慢：可在 config.json 里填 update_mirror 走镜像加速（见 README）。"
        version = self._info.version if self._info is not None else ""
        source = f"（{self._source_host}）" if self._source_host else ""
        self.status.setText(f"正在下载 {version}{source}：{text}{hint}")

    def _on_downloaded(self, outcome) -> None:
        self._worker = None
        self.btn_cancel.setVisible(False)
        if not outcome.ok:
            self._set_failed(outcome.message or "下载失败。")
            if outcome.partial_bytes:
                # 慢链路一次下不完是常态：按钮要能直接接着下，
                # 而不是又走一遍「检查更新 → 下载」
                self.btn_primary.setText("继续下载")
            return
        self._downloaded = outcome.path
        self._state = "ready"
        self.progress.setValue(100)
        if self._installed:
            self.status.setText("已下载并通过完整性校验。点下面的按钮安装。")
            self.btn_primary.setText("立即重启安装")
        else:
            # 免安装版：解压到临时目录，用户拿现成的文件去替换即可。
            # 不自动覆盖——正在运行的 exe 覆盖不了自己，目标目录里还混着
            # models/、cache/、config.json，替不替、什么时候替由用户决定。
            from app.core.app_config import app_dir
            from app.core.updater import extract_portable_zip

            ok_zip, where, why = extract_portable_zip(outcome.path, self._info.version)
            self._extracted = where if ok_zip else None
            if ok_zip:
                self.status.setText(
                    f"已解压到：{where}\n"
                    f"把里面的文件覆盖到当前程序目录（{app_dir()}）即可完成升级——"
                    "覆盖前请先关掉本程序。"
                )
                self.btn_primary.setText("打开解压目录")
            else:
                self.status.setText(
                    f"已下载并通过完整性校验：{outcome.path}\n"
                    f"自动解压失败（{why}），请手动解压后覆盖到当前程序目录。"
                )
                self.btn_primary.setText("打开所在目录")
        self.btn_primary.setEnabled(True)

    def _apply_update(self) -> None:
        from app.core.updater import launch_installer

        if self._downloaded is None:
            return
        if not self._installed:
            # 免安装版没有安装器可启动：打开已解压好的目录（解压失败则回退到下载目录）
            import os

            os.startfile(str(self._extracted or self._downloaded.parent))  # noqa: S606
            return
        # 先记下「这次要装到哪个版本」：新版首次启动会拿它对账，报一句
        # 「已更新到 X」。写失败也继续装——标记只是提示，不该挡住升级。
        from app.core.updater import mark_update_pending

        mark_update_pending(self._info.version if self._info is not None else "")
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
        # 取消不丢进度：worker 那边会保留 .part，再点「继续下载」即可接着下
        self.status.setText("正在取消…已下载的部分会保留，可点「继续下载」接着下。")

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
