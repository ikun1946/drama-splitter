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
# 这个常量是运行时的唯一来源：run.py 打印它、界面「关于」显示它、启动时的更新
# 检查拿它跟远端比。打包链路上的另外几处是**必须同步的副本**，改版本时要一起改，
# 否则会漂移：
#   - pyproject.toml 的 [project].version
#   - tools/installer.iss 的 AppVersion / AppVerName / VersionInfoVersion /
#     OutputBaseFilename
# 这些副本全由 tests/test_version.py 逐条盯着（历史上 run.py 与 pyproject.toml
# 漂移过 1.0.0 vs 0.1.0，installer.iss 的版本号也无人校验过）。
VERSION = "1.0.8"


def _print_version() -> int:
    import platform

    print(f"AI 短剧智能分集器 {VERSION}")
    print(f"Python {platform.python_version()} / {platform.system()} {platform.release()}")
    print(f"打包运行：{'是' if getattr(sys, 'frozen', False) else '否'}")
    print(f"程序目录：{getattr(sys, '_MEIPASS', ROOT)}")
    return 0


def _module_importable(name: str) -> bool:
    """该模块此刻能否导入（不触发重型顶层代码）。"""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _deps_report() -> int:
    """诊断外挂依赖的可用性（``--deps-report``）。

    为什么做成常驻命令：外挂依赖是在运行时从 exe 同级的 ``deps/`` 加载的，
    一旦某个包导入失败，界面只会含糊地说"推理栈不可用"，看不出是哪一层断的。
    这个入口把**每个关键包的导入结果与真实异常**逐条打出来，供用户贴回来排障。
    """
    import platform

    print(f"AI 短剧智能分集器 {VERSION} 外挂依赖诊断")
    print(f"Python {platform.python_version()} / 打包运行：{'是' if getattr(sys, 'frozen', False) else '否'}")
    try:
        from app.core.deps_manager import (
            REQUIRED_IMPORTS,
            default_deps_root,
            deps_present,
            register_deps,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  无法加载 deps_manager：{exc}")
        return 1

    root = default_deps_root()
    print(f"  依赖目录 : {root}")
    print(f"  存在     : {root.is_dir()}")
    print(f"  文件齐全 : {deps_present()}")
    if root.is_dir():
        print(f"  挂载结果 : {register_deps()}")

    print("  逐个导入：")
    ok = True
    probes = list(REQUIRED_IMPORTS) + ["llama_cpp", "scenedetect", "cv2", "diskcache"]
    for name in probes:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "?")
            print(f"    [OK]   {name} {version}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"    [FAIL] {name}: {type(exc).__name__}: {exc}")
            # 打印完整调用链——只报一句"找不到 X"往往看不出是哪一层断的
            import traceback

            for line in traceback.format_exc().splitlines()[-8:]:
                print(f"           {line}")

    # 顶层 import 成功**不等于**能用：真正的加载发生在取具体类/函数时
    # （llama_cpp 的 Llama、scenedetect 的 ContentDetector 都要在这一步才碰 DLL）。
    print("  关键接口：")
    for label, probe in (
        ("llama_cpp.Llama", lambda: __import__("llama_cpp", fromlist=["Llama"]).Llama),
        ("scenedetect.ContentDetector",
         lambda: __import__("scenedetect", fromlist=["ContentDetector"]).ContentDetector),
        ("faster_whisper.WhisperModel",
         lambda: __import__("faster_whisper", fromlist=["WhisperModel"]).WhisperModel),
    ):
        try:
            probe()
            print(f"    [OK]   {label}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"    [FAIL] {label}: {type(exc).__name__}: {exc}")

    # 语义判断的判定器链——这是自检「语义判断」那一行的真实依据，
    # 单独跑一遍才能对着它排障（它会**真的加载 GGUF 模型**）。
    print("  语义判定器：")
    try:
        from app.core.semantic import create_judge

        judge = create_judge()
        print(f"    [{'OK' if judge else 'FAIL'}]   create_judge() → "
              f"{judge.describe() if judge else 'None（降级为规则评分）'}")
        if judge is None:
            ok = False
            from app.core.semantic import LocalLlmJudge, locate_llm_model

            model = locate_llm_model()
            print(f"           本地模型：{model}")
            if model is not None:
                try:
                    LocalLlmJudge(model)
                    print("           直接构造 LocalLlmJudge：成功（那问题在 create_judge 的选择逻辑）")
                except Exception as exc:  # noqa: BLE001
                    import traceback

                    print(f"           直接构造 LocalLlmJudge：{type(exc).__name__}: {exc}")
                    for line in traceback.format_exc().splitlines()[-10:]:
                        print(f"              {line}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"    [FAIL] create_judge 异常：{type(exc).__name__}: {exc}")

    print("结论：" + ("外挂依赖可用" if ok else "有依赖导入失败（见上）"))
    return 0 if ok else 1


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
    # 转写引擎要单独报：打包版**有意不含** faster-whisper，此时模型就绪也转不了。
    # 用户可在应用内安装（「环境自检 → 安装转写引擎」），装好后落在 exe 同级的
    # deps/。这里如实区分"没有引擎"与"有引擎"两种处境。
    try:
        from app.core.deps_manager import deps_present, default_deps_root

        if deps_present():
            from app.core.deps_manager import deps_installed

            engine_state = "就绪（应用内安装）" if deps_installed() else "已下载但无法导入（可能缺 DLL）"
        elif _module_importable("faster_whisper"):
            engine_state = "就绪（系统 Python 环境）"
        else:
            engine_state = (
                "未安装 — 可在「帮助 → 环境自检」里点「安装转写引擎」一键装好"
                f"（约 80 MB）；引擎目录：{default_deps_root()}"
            )
        print(f"  转写引擎 : {engine_state}")
    except Exception as exc:  # noqa: BLE001
        print(f"  转写引擎 : 状态未知 — {exc}")

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

    # 三个对话框都必须能构造并显示（厂商已并入「模型管理」的页签；
    # 「模型设置」在下面单独构造——它必须用空配置，免得联网）
    for label, factory in (
        ("分集设置", window.build_settings_dialog),
        ("模型管理", window.build_model_dialog),
        ("导出", window.build_export_dialog),
    ):
        dialog = factory()
        dialog.show()
        app.processEvents()
        print(f"  {label}对话框：可构造、可显示（{dialog.width()}×{dialog.height()}）")
        dialog.close()

    # 「模型设置」用一份空配置构造：本自检是打包验收，绝不能发网络请求
    # （真实配置里若存了 Key，打开这个对话框就会去拉各家的 /models）
    import tempfile
    from pathlib import Path

    setup_dialog = window.build_model_setup_dialog(
        config_path=Path(tempfile.mkdtemp()) / "config.json"
    )
    setup_dialog.show()
    app.processEvents()
    setup_form = setup_dialog.form
    print(f"「模型设置」对话框：可构造、可显示（{setup_dialog.width()}×{setup_dialog.height()}）")
    for card in setup_form.cards():
        items = card.items()
        print(
            f"  {card.capability}：下拉 {len(items)} 项"
            f"（本地 {sum(1 for c in items if c.kind == 'local')} 个 /"
            f" 云端 {sum(1 for c in items if c.kind == 'cloud')} 个）"
            f"，徽章「{card.badge.text()}」"
        )
        print(f"    结论：{card.status_main.text()}")
    setup_dialog.close()

    # 「检查更新」对话框：网络检查要手动触发，这里只验证能构造、能显示
    update_dialog = window.build_update_dialog()
    update_dialog.show()
    app.processEvents()
    print(f"「检查更新」对话框：可构造、可显示（{update_dialog.width()}×{update_dialog.height()}）"
          f"  状态={update_dialog.status.text()[:28]}")
    update_dialog.close()

    # 模型管理：一个对话框两个页签（本地模型 / 云端厂商），两页都验一遍
    model_dialog = window.build_model_dialog()
    model_dialog.show()
    app.processEvents()
    print(f"  模型管理页签：{model_dialog.tabs.count()} 个"
          f"（{model_dialog.tabs.tabText(0)} / {model_dialog.tabs.tabText(1)}）")

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

    provider_form = model_dialog.providers
    count = provider_form.profile_list.count()
    print(f"厂商列表：{count} 家（预置目录 {len(DEFAULT_PROFILES)} 家 + 自定义）")
    print(f"  默认选中：{provider_form.selected_id()}")
    print(f"  模型表格行数：{len(provider_form.model_table.rows())}")
    provider_form.reload()
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
    if count < len(DEFAULT_PROFILES):
        print("界面自检失败：厂商列表少于预置目录")
        return 1
    model_dialog.close()
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
    if "--deps-report" in sys.argv:
        return _deps_report()

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("AI 短剧智能分集器")

    # 窗口图标必须在**第一个窗口出现前**设好，否则任务栏先显示默认图标再跳变。
    # 打包时的 --icon 只写 exe 的 PE 资源，Qt 主窗口是另一个窗口，得单独给。
    from app.gui.branding import apply_app_icon

    apply_app_icon(app)

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

    # 启动后静默检查更新。延后 2 秒是刻意的：避开启动高峰，也让主窗口先出画面。
    # 这一步**只有确有新版才会弹窗**，断网/限流一律静默（见
    # MainWindow._startup_update_check 的说明）。
    try:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(2000, window._startup_update_check)
    except Exception:  # noqa: BLE001 - 更新检查是可选便利，失败不该影响启动
        pass

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
