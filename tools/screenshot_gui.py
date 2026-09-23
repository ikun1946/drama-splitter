"""离屏渲染界面截图，用于交付预览与版式回归。

QT_QPA_PLATFORM=offscreen 下 QWidget.grab() 仍会真实走一遍布局与绘制，
因此截图能反映实际版式，而不只是"控件都建出来了"。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.gui.main_window as mw  # noqa: E402
from app.core.ffmpeg import check_encoders, locate_ffmpeg  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def install_cjk_font(app: QApplication) -> str:
    """为离屏渲染注册一个中文字体。

    离屏平台的字体库是空的（QFontDatabase.families() 返回 0 个族），
    不注册的话所有中文都会渲染成方框，截图就无法用于交付预览。
    因此直接按文件路径注册，而不是按族名查找。
    """
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",    # 黑体
        "C:/Windows/Fonts/simsun.ttc",    # 宋体
    ]
    for path in candidates:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            continue
        app.setFont(QFont(families[0], 9))
        return families[0]
    return ""


def pump(app: QApplication, seconds: float = 0.3) -> None:
    """为离屏渲染注册一个中文字体。

    离屏平台的字体库是空的（QFontDatabase.families() 返回 0 个族），
    不注册的话所有中文都会渲染成方框，截图就无法用于交付预览。
    因此直接按文件路径注册，而不是按族名查找。
    """
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",    # 黑体
        "C:/Windows/Fonts/simsun.ttc",    # 宋体
    ]
    for path in candidates:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            continue
        app.setFont(QFont(families[0], 9))
        return families[0]
    return ""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_until(app: QApplication, predicate, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    asset = ROOT / "testdata" / "cfr_120s_25fps.mp4"
    if not asset.exists():
        print("缺少测试素材：python tools/make_test_assets.py testdata --seconds 120")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)

    # 截图时不要弹模态框
    mw.QMessageBox.information = staticmethod(lambda *a, **k: None)
    mw.QMessageBox.warning = staticmethod(lambda *a, **k: None)
    mw.QMessageBox.critical = staticmethod(lambda *a, **k: None)

    app = QApplication(sys.argv)
    family = install_cjk_font(app)
    print(f"截图字体：{family or '（未注册成功，中文可能显示为方框）'}")

    window = mw.MainWindow()
    window.resize(1440, 900)
    window.show()
    pump(app)

    binaries = locate_ffmpeg()
    check_encoders(binaries)
    window.state.binaries = binaries

    # 导入
    window.import_page._probe(str(asset))
    wait_until(app, lambda: window.import_page._worker and not window.import_page._worker.isRunning())
    pump(app)

    # 设置：目标 30 秒、0% 浮动 → 4 集
    window.nav.setCurrentRow(1)
    pump(app)
    page = window.settings_page
    page.radio_by_duration.setChecked(True)
    page.range_percent.setChecked(True)
    page.duration_spin.setValue(30.0)
    page.tolerance_spin.setValue(0.0)
    pump(app)

    def shot(name: str, page_widget) -> None:
        pixmap = page_widget.grab()
        target = OUT / name
        pixmap.save(str(target), "PNG")
        print(f"  {target.relative_to(ROOT)}  {pixmap.width()}×{pixmap.height()}")

    print("生成截图：")
    shot("01-导入.png", window)
    shot("02-设置.png", window)

    # 分析
    window.nav.setCurrentRow(2)
    pump(app)
    window.analysis_page._start()
    wait_until(app, lambda: window.state.current_plan is not None)
    pump(app, 0.5)
    shot("03-分析.png", window)

    # 审核
    window.nav.setCurrentRow(3)
    pump(app, 0.6)
    window.review_page.episode_table.selectRow(1)
    pump(app, 0.6)
    shot("04-审核.png", window)

    # 导出
    window.nav.setCurrentRow(4)
    pump(app)
    out_dir = ROOT / "testdata" / "_shot_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    window.state.output_root = out_dir
    window.export_page.output_edit.setText(str(out_dir))
    window.export_page.refresh()
    pump(app, 0.4)
    shot("05-导出.png", window)

    window.review_page._player.stop()
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
