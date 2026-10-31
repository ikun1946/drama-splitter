"""首次运行向导的界面层测试（离屏，不下载、不 exec）。

真实下载与校验在 tests/test_ffmpeg_setup.py 里用 fake 覆盖；这里只钉住
“自动开始 / 成功关闭 / 失败给重试 / 取消不崩”这几个界面契约。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from app.core.ffmpeg_setup import FfmpegInstallResult  # noqa: E402
from app.gui.dialogs.ffmpeg_setup import FfmpegSetupDialog  # noqa: E402

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app

def test_dialog_constructs_without_starting_download(qapp):
    dialog = FfmpegSetupDialog(auto_start=False)
    try:
        assert not dialog.btn_cancel.isHidden()
        assert dialog.btn_retry.isHidden()
        assert dialog.btn_choose.isEnabled()
        assert dialog.progress.maximum() == 1
    finally:
        dialog.close()

def test_success_closes_dialog(qapp):
    dialog = FfmpegSetupDialog(auto_start=False)
    try:
        dialog._on_finished(
            FfmpegInstallResult(
                ok=True,
                message="FFmpeg 已配置",
                directory=None,
                version="test",
            )
        )
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert "已配置完成" in dialog.status.text()
    finally:
        dialog.close()

def test_failure_offers_retry(qapp):
    dialog = FfmpegSetupDialog(auto_start=False)
    try:
        dialog._on_finished(FfmpegInstallResult(ok=False, message="网络不可达"))
        assert not dialog.btn_retry.isHidden()
        assert "网络不可达" in dialog.status.text()
    finally:
        dialog.close()

def test_cancel_result_shows_retry(qapp):
    dialog = FfmpegSetupDialog(auto_start=False)
    try:
        dialog._on_finished(FfmpegInstallResult(ok=False, cancelled=True, message="已取消下载"))
        assert not dialog.btn_retry.isHidden()
        assert "已取消" in dialog.status.text()
    finally:
        dialog.close()
