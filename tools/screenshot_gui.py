"""离屏渲染界面截图，用于交付预览与版式回归。

QT_QPA_PLATFORM=offscreen 下 QWidget.grab() 仍会真实走一遍布局与绘制，
因此截图能反映实际版式，而不只是"控件都建出来了"。

用法::

    python tools/screenshot_gui.py                # 浅色，输出到 docs/screenshots/
    python tools/screenshot_gui.py --theme dark    # 深色，输出到 docs/screenshots/dark/
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.gui.main_window as mw  # noqa: E402
from app.core.ffmpeg import check_encoders, locate_ffmpeg  # noqa: E402

OUT_DEFAULT = ROOT / "docs" / "screenshots"


def install_fonts(app: QApplication) -> str:
    """为离屏渲染注册中文字体与符号字体。

    离屏平台的字体库是空的（QFontDatabase.families() 返回 0 个族），
    不注册的话所有中文都会渲染成方框，截图无法用于交付预览。
    因此直接按文件路径注册，而不是按族名查找。

    符号字体（seguisym / seguiemj）同样必须注册：样式表里的
    ``FONT_SYMBOL`` 回退链会先找 "Segoe UI Symbol"，离屏平台找不到这个族时
    ⚙ / 🤖 会退回到无字形的字体，渲染成豆腐块。
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
        result = families[0]
        break
    else:
        result = ""

    for path in ("C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/seguiemj.ttf"):
        if Path(path).exists():
            QFontDatabase.addApplicationFont(path)

    return result


def pump(app: QApplication, seconds: float = 0.3) -> None:
    """跑一圈事件循环，让布局与动画推进到位。

    注：本函数此前被误粘贴成了 install_cjk_font 的副本，导致调用点
    全部失去作用——动画未推进，而且函数体的循环写在 return 之后，
    是永远到不了的死代码。
    """
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离屏渲染界面截图")
    parser.add_argument(
        "--theme", choices=("light", "dark"), default="light",
        help="主题（默认 light）。dark 输出到 docs/screenshots/dark/",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    theme_mode = args.theme
    out_dir = OUT_DEFAULT if theme_mode == "light" else OUT_DEFAULT / theme_mode
    # 抽屉拼接的画布底色必须跟着主题走，否则深色截图右侧会露一条白边
    canvas_bg = "#202226" if theme_mode == "dark" else "#ffffff"

    asset = ROOT / "testdata" / "cfr_120s_25fps.mp4"
    if not asset.exists():
        print("缺少测试素材：python tools/make_test_assets.py testdata --seconds 120")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    # 截图时不要弹模态框
    mw.QMessageBox.information = staticmethod(lambda *a, **k: None)
    mw.QMessageBox.warning = staticmethod(lambda *a, **k: None)
    mw.QMessageBox.critical = staticmethod(lambda *a, **k: None)

    app = QApplication(sys.argv[:1])
    family = install_fonts(app)
    print(f"主题：{theme_mode}    输出：{out_dir.relative_to(ROOT)}")
    print(f"截图字体：{family or '（未注册成功，中文可能显示为方框）'}")

    window = mw.MainWindow()
    # MainWindow.__init__ 里已按默认主题调用过 apply_theme()，这里覆盖成目标主题
    from app.gui.theme import apply_theme

    apply_theme(app, theme_mode)
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

    hidden: list[str] = []

    def shot(name: str, widget) -> None:
        """截取整个窗口。

        QMainWindow 的原生 QStatusBar 已在 MainWindow 里 hide()，不再单独裁剪；
        若哪天又冒出来，这里会记一笔而不是悄悄改变画面尺寸。
        """
        pixmap = widget.grab()
        status = getattr(window, "status_bar", None)
        if status is not None and status.isVisible():
            hidden.append("status_bar 可见，已从截图底部裁掉")
            pixmap = pixmap.copy(0, 0, pixmap.width(), status.y())
        target = out_dir / name
        pixmap.save(str(target), "PNG")
        print(f"  {target.relative_to(ROOT)}  {pixmap.width()}×{pixmap.height()}")

    def shot_drawer(name: str) -> None:
        """抽屉打开时，连同右侧面板一起截出来。

        窗口截图只包含 QMainWindow 的内容，而抽屉是 centralWidget 的子控件、
        挂在布局之外——单独截窗口会漏掉抽屉本身。因此做横向拼接：
        窗口左侧到抽屉左边界那一段，后面接抽屉面板。

        直接拼「整窗 + 抽屉」会出问题：抽屉是浮在窗口之上的，窗口截图里
        被遮住的区域仍然会被画进来，拼完就多出一份重复内容。
        """
        left = window.grab()
        drawers = [
            d for d in (window.settings_drawer, window.model_drawer) if d.isVisible()
        ]
        if not drawers:
            shot(name, window)
            return
        drawer = drawers[0]
        panel = drawer.grab()
        cut = max(0, min(drawer.x(), left.width()))
        canvas = QPixmap(cut + panel.width(), left.height())
        canvas.fill(QColor(canvas_bg))
        painter = QPainter(canvas)
        painter.drawPixmap(0, 0, left, 0, 0, cut, left.height())
        painter.drawPixmap(cut, 0, panel)
        painter.end()
        target = out_dir / name
        canvas.save(str(target), "PNG")
        print(
            f"  {target.relative_to(ROOT)}  {canvas.width()}×{canvas.height()}"
            f"（窗口 {cut} + 抽屉 {panel.width()}）"
        )

    def open_drawer(drawer) -> None:
        """打开抽屉并等滑动动画真正结束。

        抽屉的显示过程是 QPropertyAnimation（220ms）。此前截图工具只调用
        nav.setCurrentRow(1) 就立刻抓图：此时抽屉刚定位到父窗口右外侧
        （x = width），截图里只有占位页的提示文本。必须等动画 finished
        才能抓到实际内容。
        """
        wait_until(app, lambda: drawer.isVisible() and drawer.x() < window.width())
        pump(app, 0.4)

    print("生成截图：")
    # 工作台一页承担导入 + 分析：先抓「刚导入完」的状态（检查器已列兼容性问题）
    shot("01-工作台-导入.png", window)

    # 设置：目标 30 秒、0% 浮动 → 4 集
    page = window.settings_page
    page.radio_by_duration.setChecked(True)
    page.range_percent.setChecked(True)
    page.duration_spin.setValue(30.0)
    page.tolerance_spin.setValue(0.0)
    window.nav.setCurrentRow(1)
    open_drawer(window.settings_drawer)
    shot_drawer("02-设置.png")

    # 分析：同一页的下半区，跑完后抓「工作台 + 检查器」的完整形态
    window.nav.setCurrentRow(0)
    pump(app)
    window.analysis_page._start()
    wait_until(app, lambda: window.state.current_plan is not None)
    pump(app, 0.5)
    shot("03-工作台-分析.png", window)

    # 审核
    window.nav.setCurrentRow(2)
    pump(app, 0.6)
    window.review_page.episode_table.selectRow(1)
    pump(app, 0.6)
    shot("04-审核.png", window)

    # 导出
    window.nav.setCurrentRow(3)
    pump(app)
    export_dir = ROOT / "testdata" / "_shot_out"
    export_dir.mkdir(parents=True, exist_ok=True)
    window.state.output_root = export_dir
    window.export_page.output_edit.setText(str(export_dir))
    window.export_page.refresh()
    pump(app, 0.4)
    shot("05-导出.png", window)

    # 模型页：选中一个"未安装"的模型，让截图里能看到选择与状态的区别
    models = window.model_page
    uninstalled = [
        row for row in range(models.table.rowCount())
        if "未安装" in models.table.item(row, 2).text()
    ]
    if uninstalled:
        models.table.selectRow(uninstalled[0])
    models._append("模型页：可直接选择并下载所需模型，无需命令行。")
    window.nav.setCurrentRow(4)
    open_drawer(window.model_drawer)
    shot_drawer("06-模型.png")

    window.review_page._player.stop()
    for note in hidden:
        print(f"  提示：{note}")

    # 内容去重：抓错页面（例如两次都抓到同一页）会让截图表格静默变成错的，
    # 而这种错误靠人眼看文件名是发现不了的 —— 必须用内容哈希兜住。
    import hashlib

    digests: dict[str, str] = {}
    duplicates: list[str] = []
    for path in sorted(out_dir.glob("*.png")):
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        if digest in digests:
            duplicates.append(f"{digests[digest]} 与 {path.name} 内容完全相同")
        digests[digest] = path.name
    if duplicates:
        print("截图内容重复：")
        for item in duplicates:
            print("  !! " + item)
        return 1

    print(f"完成：{len(digests)} 张截图，内容互不相同。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
