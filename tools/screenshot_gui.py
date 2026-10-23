"""离屏渲染界面截图，用于交付预览与版式回归。

QT_QPA_PLATFORM=offscreen 下 QWidget.grab() 仍会真实走一遍布局与绘制，
因此截图能反映实际版式，而不只是"控件都建出来了"。

设置 / 模型 / 导出是对话框（真顶层窗口），直接对对话框 grab() 即可，不再需要
"窗口 + 抽屉"的横向拼接 hack——抽屉被删除后连它一起没了。

用法::

    python tools/screenshot_gui.py                # 浅色，输出到 docs/screenshots/
    python tools/screenshot_gui.py --theme dark    # 深色，输出到 docs/screenshots/dark/
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
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
from app.gui.stage import Stage  # noqa: E402

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

    asset = ROOT / "testdata" / "cfr_120s_25fps.mp4"
    if not asset.exists():
        print("缺少测试素材：python tools/make_test_assets.py testdata --seconds 120")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    # 截图时不要弹模态框：统一走 app.gui.widgets.message，patch 一处即可
    from app.gui.widgets import message as message_mod

    message_mod.information = lambda *a, **k: None
    message_mod.warning = lambda *a, **k: None
    message_mod.critical = lambda *a, **k: None
    message_mod.warning_with_action = lambda *a, **k: False

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

    hidden: list[str] = []

    def shot(name: str, widget) -> None:
        """截取整个控件（窗口或对话框）。

        QMainWindow 的原生 QStatusBar 已在 MainWindow 里 hide()，不再单独裁剪；
        若哪天又冒出来，这里会记一笔而不是悄悄改变画面尺寸。
        """
        pixmap = widget.grab()
        status = getattr(window, "status_bar", None)
        if widget is window and status is not None and status.isVisible():
            hidden.append("status_bar 可见，已从截图底部裁掉")
            pixmap = pixmap.copy(0, 0, pixmap.width(), status.y())
        target = out_dir / name
        pixmap.save(str(target), "PNG")
        print(f"  {target.relative_to(ROOT)}  {pixmap.width()}×{pixmap.height()}")

    def shot_dialog(name: str, factory) -> object:
        """打开对话框并截图。

        只允许 show() + pump()，禁止 exec()——实例级模态在离屏环境会永久阻塞
        （实测挂过 25 分钟）。
        """
        dialog = factory()
        dialog.show()
        pump(app, 0.4)
        shot(name, dialog)
        dialog.close()
        pump(app, 0.1)
        return dialog

    print("生成截图：")
    workbench = window.workbench

    # 先设定分集目标（目标 30 秒 / 0% 浮动 → 4 集）：分析需要完整参数才能跑，
    # 否则 start() 会以「参数不完整」提前返回，截出来的"分析中"其实还是就绪态。
    setup = window.build_settings_dialog()
    setup.form.radio_by_duration.setChecked(True)
    setup.form.range_percent.setChecked(True)
    setup.form.duration_spin.setValue(30.0)
    setup.form.tolerance_spin.setValue(0.0)
    setup.apply_and_close()

    # 01 空态：中栏是拖放 hero，左栏让位（单页工作台的起点）
    workbench.refresh()
    pump(app, 0.4)
    shot("01-工作台-空态.png", window)

    # 02 素材就绪：左栏素材信息 + 中栏预览与整片时间轴
    workbench.import_pane.probe(str(asset))
    wait_until(
        app,
        lambda: workbench.import_pane.worker and not workbench.import_pane.worker.isRunning(),
    )
    pump(app, 0.6)
    shot("02-工作台-素材就绪.png", window)

    # 03 分析中：中栏换成阶段 + 进度 + 最近输出，右栏自动切到日志页签。
    # 本素材分析很快（无对白，转写被跳过），因此只派发一次事件循环就抓图，
    # 不去 sleep；若这一瞬已经跑完，日志里会记一笔而不是把别的画面冒充成"分析中"。
    workbench.analysis_pane.start()
    app.processEvents()
    if workbench.stage is not Stage.ANALYZING:
        print("  提示：分析在本机过快，03 抓到的是分析完成后的画面")
    shot("03-工作台-分析中.png", window)
    wait_until(app, lambda: window.state.current_plan is not None)
    pump(app, 0.6)

    # 04 审核：左栏集列表 + 中栏预览与时间轴（含切点标记）+ 右栏检查器
    workbench.select_episode(2)
    pump(app, 0.6)
    shot("04-工作台-审核.png", window)

    # 05 设置对话框：目标 30 秒、0% 浮动 → 4 集
    def _settings_dialog():
        dialog = window.build_settings_dialog()
        form = dialog.form
        form.radio_by_duration.setChecked(True)
        form.range_percent.setChecked(True)
        form.duration_spin.setValue(30.0)
        form.tolerance_spin.setValue(0.0)
        pump(app, 0.2)
        return dialog

    shot_dialog("05-设置.png", _settings_dialog)

    # 06 模型对话框：两组卡片（语音转写 / 语义判断），每行都有「使用中」与行内动作
    def _model_dialog():
        dialog = window.build_model_dialog()
        form = dialog.form
        form.refresh()
        form._append("模型：在行内直接下载；标着「使用中」的那一行才是分析时用的。")
        pump(app, 0.2)
        return dialog

    shot_dialog("06-模型.png", _model_dialog)

    # 07 导出对话框：默认原生编码；前情回顾默认关闭，展开时另见说明文案
    export_dir = ROOT / "testdata" / "_shot_out"
    export_dir.mkdir(parents=True, exist_ok=True)
    window.state.output_root = export_dir

    def _export_dialog():
        dialog = window.build_export_dialog()
        dialog.form.output_edit.setText(str(export_dir))
        dialog.form.refresh()
        return dialog

    shot_dialog("07-导出.png", _export_dialog)

    # 08 云端模型页签：左列表 + 厂商连接首屏；进阶模型管理默认收起。
    # 选中 DeepSeek：首屏只展示连接信息，用途与模型留给「模型设置」。
    # 用空配置构造：本机若存了 API Key，切到这页就会真去拉各家的 /models。
    def _provider_dialog():
        dialog = window.build_model_dialog(
            config_path=Path(tempfile.mkdtemp()) / "config.json", providers_tab=True
        )
        form = dialog.providers
        form.select_profile("deepseek")
        pump(app, 0.2)
        return dialog

    shot_dialog("08-云端模型.png", _provider_dialog)

    # 09 模型设置：两条能力各选一个模型，本地 / 云端可以混着用。
    # 刻意用**空配置**构造——本机若存了 API Key，打开它就会真去拉各家的
    # /models；截图脚本不该联网花用户的额度。因此这张图是"还没配 Key"时的
    # 真实样子：状态行会说清下一步怎么做。
    def _setup_dialog():
        dialog = window.build_model_setup_dialog(
            config_path=Path(tempfile.mkdtemp()) / "config.json"
        )
        pump(app, 0.2)
        return dialog

    shot_dialog("09-模型设置.png", _setup_dialog)

    workbench.stage_view.player.stop()
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


def teardown_qt() -> None:
    """显式拆掉 Qt，再让解释器退出。

    留给解释器收尾会在 Qt 里 fail-fast（0xC0000409）——截图**已经写完了**，
    但脚本返回码会变成崩溃码，脚本化调用者会以为失败了。
    """
    try:
        import shiboken6
        from PySide6.QtWidgets import QApplication
    except ImportError:      # 没装 Qt
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:  # C++ 侧已释放
            continue
    app.processEvents()
    app.quit()
    try:
        shiboken6.delete(app)
    except Exception:  # noqa: BLE001 - 拆不掉也别再抛
        pass


if __name__ == "__main__":
    code = main()
    teardown_qt()
    raise SystemExit(code)
