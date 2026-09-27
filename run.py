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


# 应用版本号。
#
# 与 pyproject.toml 的 [project].version 保持一致：两处都是打包相关的元数据
# 入口（一处给 Python 打包工具读，一处给运行时自检打印）。改版本时两处都要改
# ——历史上它们漂移到了 1.0.0 / 0.1.0。
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

    # 云端档案的状态也要报：只看本地模型会给"已启用云端"的用户一个误导结论
    from app.core.app_config import LOCAL_BACKEND, AppConfig
    from app.core.semantic import cloud_block_reason

    config = AppConfig.load()
    profile = config.active_profile("judge")
    if profile is None:
        backend = config.backend_for("judge")
        if backend != LOCAL_BACKEND:
            print(f"  云端语义 : 选了 {backend} 但该档案没配语义判断，实际走本地")
    else:
        reason = cloud_block_reason(profile)
        print(
            "  云端语义 : "
            + (f"未生效 — {reason}" if reason else f"就绪 {profile.label}／{profile.judge_model}")
        )

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
    那个入口根本不碰界面。这里把界面构建一遍，并把五个对话框各构造一次、
    走一遍模型清单，因此可以作为"打包产物是否完整"的验收手段。

    对话框只 ``show()`` 不 ``exec()``：实例级模态在离屏环境会永久阻塞。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.core.providers import DEFAULT_PROFILES
    from app.gui.main_window import MainWindow

    window = MainWindow()
    app.processEvents()
    workbench = window.workbench
    print(f"界面构建成功：单页工作台（{workbench.splitter.count()} 栏）+ 菜单 "
          f"{len(window.menuBar().actions())} 组")

    # 三栏与底栏都必须就位（漏打模块/缺插件在这里暴露）
    assert workbench.splitter.count() == 3, "工作台应由三栏组成"
    assert workbench.inspector is not None, "右栏检查器应常驻"
    print(f"  底栏主动作：{window.bottom.action_text()}")

    # 四个对话框都必须能构造并显示
    for label, factory in (
        ("分集设置", window.build_settings_dialog),
        ("模型管理", window.build_model_dialog),
        ("厂商管理", window.build_provider_dialog),
        ("导出", window.build_export_dialog),
    ):
        dialog = factory()
        dialog.show()
        app.processEvents()
        print(f"  {label}对话框：可构造、可显示（{dialog.width()}×{dialog.height()}）")
        dialog.close()

    # 「选择云端模型」用一份空配置构造：本自检是打包验收，绝不能发网络请求
    # （真实配置里若存了 Key，打开这个对话框就会去拉各家的 /models）
    import tempfile
    from pathlib import Path

    pick_dialog = window.build_model_pick_dialog(
        config_path=Path(tempfile.mkdtemp()) / "config.json"
    )
    pick_dialog.show()
    app.processEvents()
    print(f"  选择云端模型对话框：可构造、可显示（{pick_dialog.width()}×{pick_dialog.height()}）")
    print(f"    厂商下拉：{pick_dialog.form.provider_combo.count()} 家（空配置下为 0）")
    print(f"    状态行：{pick_dialog.form.status_label.text()}")
    pick_dialog.close()

    provider_dialog = window.build_provider_dialog()
    provider_dialog.show()
    app.processEvents()
    provider_form = provider_dialog.form
    count = provider_form.profile_list.count()
    print(f"厂商列表：{count} 家（预置目录 {len(DEFAULT_PROFILES)} 家 + 自定义）")
    selected = provider_form.selected_id()
    print(f"  默认选中：{selected}")
    provider_form.reload()
    app.processEvents()
    provider_dialog.close()
    if count < len(DEFAULT_PROFILES):
        print("界面自检失败：厂商列表少于预置目录")
        return 1

    model_dialog = window.build_model_dialog()
    model_dialog.show()
    page = model_dialog.form
    rows = page.rows()
    print(f"模型清单：{len(rows)} 行（语音转写 {sum(1 for r in rows if r['kind'] == 'whisper')} 档"
          f" + 语义判断 {sum(1 for r in rows if r['kind'] == 'llm')} 档）")
    for row in rows:
        print(
            f"  {row['label']:22s} {row['size_text']:16s} {row['state']:24s} "
            f"{'● 使用中' if row['in_use'] else '○'}  [{row['action']}]"
        )
    print(f"模型目录：{page.dir_edit.text()}")
    page._refresh()
    app.processEvents()

    # 核心工作流是单页三栏；设置与模型是对话框，不占工作区。
    rows = page.rows()
    asr_rows = [row for row in rows if row["kind"] == "whisper"]
    llm_rows = [row for row in rows if row["kind"] == "llm"]
    if page.asr_card.title() != "语音转写" or page.llm_card.title() != "语义判断":
        print("界面自检失败：模型清单应分为「语音转写 / 语义判断」两组卡片")
        return 1
    if len(asr_rows) != 5:
        print(f"界面自检失败：转写档位应列出 5 档，实际 {len(asr_rows)}")
        return 1
    if sum(1 for row in asr_rows if row["in_use"]) != 1:
        print("界面自检失败：转写组「使用中」应恰有一个")
        return 1
    if sum(1 for row in llm_rows if row["in_use"]) > 1:
        print("界面自检失败：语义组「使用中」最多一个")
        return 1
    if not page.asr_status.text().startswith("使用中："):
        print(f"界面自检失败：转写组应写明使用中的档位，实际「{page.asr_status.text()}」")
        return 1
    if len(window.menuBar().actions()) != 4:
        print("界面自检失败：菜单应为 4 组（文件 / 工具 / 视图 / 帮助）")
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
