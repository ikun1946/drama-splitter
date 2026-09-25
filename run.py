"""AI 短剧智能分集器 —— 启动入口。

用法：
    python run.py               启动界面
    python run.py --version     输出版本与运行环境（打包后用于无界面自检）
    python run.py --self-check   检查 FFmpeg / 模型 / 缓存等运行前提
    python run.py --ui-smoke     离屏构建界面并自检（打包后验证界面是否完整）
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


VERSION = "1.0.0"


def _print_version() -> int:
    import platform

    print(f"AI 短剧智能分集器 {VERSION}")
    print(f"Python {platform.python_version()} / {platform.system()} {platform.release()}")
    print(f"打包运行：{'是' if getattr(sys, 'frozen', False) else '否'}")
    print(f"程序目录：{getattr(sys, '_MEIPASS', ROOT)}")
    return 0


def _self_check() -> int:
    """无界面自检：打包后确认运行前提是否齐备（§15 部署与硬件兼容）。"""
    import platform

    print(f"AI 短剧智能分集器 {VERSION} 自检")
    print(f"Python {platform.python_version()} / {platform.system()}")
    ok = True

    try:
        from app.core.ffmpeg import check_encoders, locate_ffmpeg, probe_hw_encoders

        binaries = locate_ffmpeg()
        check_encoders(binaries)
        print(f"  FFmpeg   : {binaries.ffmpeg}")
        print(f"  FFprobe  : {binaries.ffprobe}")
        hardware = probe_hw_encoders(binaries)
        print(
            "  硬件编码 : "
            + ("、".join(hardware.values()) if hardware else "未检测到（将使用 CPU 编码）")
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FFmpeg   : 不可用 — {exc}")

    from app.core.asr import locate_model, model_search_roots
    from app.core.semantic import locate_llm_model

    print("  模型查找 : " + " → ".join(str(p) for p in model_search_roots()))

    whisper = locate_model("small")
    print(
        "  转写模型 : "
        + (f"就绪 {whisper.describe()}" if whisper else "未下载（python tools/fetch_whisper_model.py small）")
    )
    llm = locate_llm_model()
    print("  语义模型 : " + (f"就绪 {llm.name}" if llm else "未下载（python tools/fetch_llm.py）"))
    if llm is not None:
        from app.core.semantic import create_judge

        judge = create_judge()
        print("  语义判断 : " + (judge.describe() if judge else "模型已就位但推理栈不可用（降级运行）"))

    print("结论：" + ("运行前提齐备" if ok else "缺少必需组件，请按上面提示处理"))
    return 0 if ok else 1


def _force_utf8_console() -> None:
    """把标准输出切到 UTF-8。

    Windows 控制台默认用 GBK 代码页，打包版的 `--self-check` 输出中文会变成
    乱码（实测如此）。重配置失败不影响功能，只是输出仍可能乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            continue


def _ui_smoke() -> int:
    """离屏构建整个界面并自检。

    打包版的界面问题（漏打模块、缺 Qt 插件）无法靠 `--self-check` 发现——
    那个入口根本不碰界面。这里把界面构建一遍并走一遍模型页，因此可以作为
    "打包产物是否完整"的验收手段。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.gui.main_window import MainWindow

    window = MainWindow()
    app.processEvents()
    print(f"界面构建成功：{window.stack.count()} 页")
    for index, name in enumerate(window.PAGES):
        print(f"  {index + 1}. {name}")

    page = window.model_page
    print(f"模型清单：{page.table.rowCount()} 项")
    for row in range(page.table.rowCount()):
        print(
            f"  {page.table.item(row, 0).text():34s} "
            f"{page.table.item(row, 2).text()}"
        )
    print(f"模型目录：{page.dir_edit.text()}")
    page._refresh()
    app.processEvents()

    if window.stack.count() != len(window.PAGES):
        print("界面自检失败：页面数与导航项不一致")
        return 1
    if page.table.rowCount() == 0:
        print("界面自检失败：模型清单为空")
        return 1
    print("界面自检通过")
    return 0


def main() -> int:
    if "--version" in sys.argv or "--self-check" in sys.argv:
        _force_utf8_console()
    if "--ui-smoke" in sys.argv:
        _force_utf8_console()
        return _ui_smoke()
    if "--version" in sys.argv:
        return _print_version()
    if "--self-check" in sys.argv:
        return _self_check()

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("AI 短剧智能分集器")

    try:
        from app.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001
        QMessageBox.critical(
            None,
            "启动失败",
            "无法加载界面模块：\n\n" + traceback.format_exc(limit=3),
        )
        return 1

    window = MainWindow()

    # 启动即检查 FFmpeg：这是整个工具的前提，缺失时不要等到用户点了导入才报错
    try:
        from app.core.ffmpeg import check_encoders, locate_ffmpeg

        binaries = locate_ffmpeg()
        check_encoders(binaries)
        window.state.binaries = binaries
        missing = [
            name for name in ("libx264", "aac") if name not in binaries.encoders
        ]
        if missing:
            QMessageBox.warning(
                window,
                "编码器缺失",
                "当前 FFmpeg 缺少以下编码器，精确导出不可用：\n"
                + "、".join(missing),
            )
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(
            window,
            "未找到 FFmpeg",
            f"{exc}\n\n导入与导出功能需要 FFmpeg，请先安装并加入 PATH，"
            "或设置环境变量 DRAMA_FFMPEG_DIR 指向其 bin 目录。",
        )

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
