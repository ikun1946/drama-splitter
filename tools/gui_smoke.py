"""GUI 冒烟测试：离屏跑通「导入 → 分析 → 审核 → 导出」全链路（单页工作台）。

用离屏平台（QT_QPA_PLATFORM=offscreen）无需显示器即可运行。
所有弹窗都被替换为记录器，避免模态框在自动化环境中阻塞——离屏下 QMessageBox
的 exec() 会永久阻塞（静态方法与实例都一样，实测挂过 25 分钟）。

断言的是**结果**而不是"点了没报错"：导出后逐帧读回条码，确认帧身份正确。

单页工作台的阶段（``app/gui/stage.py``）
----------------------------------------
EMPTY（未导入）→ READY（素材就绪）→ ANALYZING（分析中）→ REVIEW（有方案）
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

from PySide6.QtWidgets import QApplication  # noqa: E402

import app.gui.main_window as mw  # noqa: E402
from app.core.ffmpeg import locate_ffmpeg  # noqa: E402
from app.gui.stage import Stage  # noqa: E402
from verify import measure_av_sync, probe_frame_count, read_frame_indices  # noqa: E402

DIALOGS: list[tuple[str, str, str]] = []


def _install_dialog_recorders() -> None:
    """把所有弹窗换成录制器（统一出口 app.gui.widgets.message）。"""
    from app.gui.widgets import message

    def record(kind):
        def inner(parent, title="", text="", *args, **kwargs):
            DIALOGS.append((kind, str(title), str(text)))
            return None

        return inner

    message.information = record("info")
    message.warning = record("warn")
    message.critical = record("critical")

    # §37 前置检查是对话框里的一个可覆写决策方法。覆写它模拟用户选「继续导出」——
    # 这是唯一的决策缝，绕开它就等于绕开模态阻塞。
    from app.gui.dialogs import ExportDialog

    def auto_continue(self, warnings, unreviewed):
        print(f"  [前置检查] 自动选择「继续导出」（未审核 {unreviewed} / 警告 {len(warnings)}）")
        return "continue"

    ExportDialog.request_precheck = auto_continue


def wait_until(app: QApplication, predicate, timeout: float = 180.0, label: str = "") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    print(f"  !! 等待超时：{label}")
    return False


def main() -> int:
    asset = ROOT / "testdata" / "cfr_120s_25fps.mp4"
    if not asset.exists():
        print("缺少测试素材，请先运行：python tools/make_test_assets.py testdata --seconds 120")
        return 2

    out_dir = ROOT / "testdata" / "_gui_out"
    # 必须清空输出目录：否则第二次运行时"目标已存在不覆盖"会让全部集导出失败，
    # 而断言读到的是上一轮遗留的成片 —— 测试会假通过，掩盖导出回归。
    if out_dir.exists():
        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    _install_dialog_recorders()

    window = mw.MainWindow()
    window.show()
    app.processEvents()
    workbench = window.workbench
    print("窗口构造成功：三栏工作台 +", len(window.menuBar().actions()), "组菜单")
    assert workbench.splitter.count() == 3, "工作台应由三栏组成"

    from app.core.ffmpeg import check_encoders

    binaries = locate_ffmpeg()
    check_encoders(binaries)
    window.state.binaries = binaries

    # ---- 空态：中栏是拖放 hero，底栏主动作是「选择源视频…」 ----------------
    print("[空态] 未导入源视频…")
    workbench.refresh()
    assert workbench.stage is Stage.EMPTY, f"空态阶段应为 EMPTY，实际 {workbench.stage}"
    assert window.bottom.action_text().startswith("选择源视频"), window.bottom.action_text()
    assert not workbench.left_stack.isVisible(), "空态应隐藏左栏（把中栏让给拖放区）"

    # ---- READY：导入并探测 -------------------------------------------------
    print("[就绪] 导入并探测…")
    workbench.import_pane.probe(str(asset))
    ok = wait_until(
        app,
        lambda: workbench.import_pane.worker and not workbench.import_pane.worker.isRunning(),
        label="媒体探测",
    )
    if not ok:
        return 1
    app.processEvents()
    media = window.state.media
    assert media is not None, "探测未产生媒体信息"
    assert workbench.stage is Stage.READY, f"应进入就绪阶段，实际 {workbench.stage}"
    print(f"  源片：{media.video.width}×{media.video.height} "
          f"{float(media.video.nominal_fps):.2f}fps {media.video.nb_frames} 帧")
    print(f"  音轨：{len(media.audio_tracks)} 条，已选：{window.state.audio_track_label}")
    ok, reason = workbench.can_start_analysis()
    assert ok, f"素材就绪却无法开始分析：{reason}"
    assert window.bottom.action_text().startswith("▶"), window.bottom.action_text()

    inspector = workbench.inspector
    print(f"  检查器：问题 {inspector.problem_count()} 条"
          f"（其中阻断 {inspector.blocking_count()}）")

    # ---- 设置对话框：目标 30 秒 / 0% 浮动 ----------------------------------
    print("[设置] 对话框：目标时长 30 秒，0% 浮动…")
    settings_dialog = window.build_settings_dialog()
    settings_dialog.show()
    app.processEvents()
    assert settings_dialog.isVisible(), "设置对话框未显示"
    page = settings_dialog.form
    page.radio_by_duration.setChecked(True)
    page.range_percent.setChecked(True)
    page.duration_spin.setValue(30.0)
    page.tolerance_spin.setValue(0.0)
    app.processEvents()

    text = page.feasibility.toPlainText()
    assert "可行集数范围：4 – 4 集" in text, f"可行性提示不符合预期：\n{text}"
    print("  可行性提示：")
    for line in text.strip().splitlines():
        print("    " + line)
    settings_dialog.apply_and_close()   # 提交参数（关闭=取消会回滚，见 SettingsDialog）
    app.processEvents()
    assert window.state.settings.split_mode.value == "target_duration", "参数未提交到状态"

    # ---- 分析：跑完得到方案 ------------------------------------------------
    print("[分析] 生成草案方案…")
    workbench.analysis_pane.start()
    ok = wait_until(
        app,
        lambda: window.state.current_plan is not None,
        label="方案生成",
    )
    assert ok, "未生成方案"
    app.processEvents()
    assert workbench.stage is Stage.REVIEW, f"有方案后应进入审核阶段，实际 {workbench.stage}"

    # 断言日志内容，而不是只看 current_plan：槽函数中途抛异常时方案仍已入状态，
    # 但日志会缺行（真实案例：签名漏了参数，函数体抛 NameError，界面完全看不出来）。
    log_text = inspector.log_text()
    # 「本地转写档位：whisper-X」必须在日志里：模型页的「使用中」不是装饰品，
    # 它得真的落到分析线程上——缺这一行时，用户与工具都看不出实际用了哪一档。
    for required in ("候选点：", "整集复核", "覆盖校验：", "方案层校验：", "本地转写档位："):
        assert required in log_text, (
            f"检查器日志缺少「{required}」——完成回调可能中途失败。"
            f"日志尾部：\n{log_text[-600:]}"
        )
    print(f"  检查器日志 {len(log_text.splitlines())} 行")

    plan = window.state.current_plan
    print(f"  方案 v{plan.version}：{plan.episode_count} 集，边界 {len(plan.boundary_ticks)} 个")
    for episode in plan.episodes():
        print(f"    第{episode.index:02d}集 "
              f"{float(episode.start_seconds):8.3f}s – {float(episode.end_seconds):8.3f}s "
              f"（{float(episode.duration_seconds):7.3f}s）")
    assert plan.coverage_valid(), "方案未通过覆盖校验"
    assert workbench.review_pane.episode_table.rowCount() == plan.episode_count, (
        "左栏集列表行数应与集数一致"
    )
    assert window.bottom.action_text().startswith("导出"), window.bottom.action_text()
    timeline = workbench.stage_view.timeline
    assert timeline._markers, "时间轴应显示内切点标记"

    # ---- 审核：预览、逐帧步进、切点合法性 ----------------------------------
    print("[审核] 预览与切点操作…")
    stage_view = workbench.stage_view
    episodes = plan.episodes()
    workbench.select_episode(2)
    app.processEvents()
    image = stage_view.decoder.frame_at(episodes[1].start_seconds)
    assert image is not None and not image.isNull(), "预览未取到解码帧"
    print(f"  预览帧尺寸：{image.width()}×{image.height()}")

    stage_view.goto(episodes[1].start_seconds)
    before = stage_view.position
    stage_view.step_frames(1)
    after = stage_view.position
    frame_duration = 1 / float(media.video.nominal_fps)
    assert abs(float(after - before) - frame_duration) < 1e-6, (
        f"逐帧步进跨度为 {float(after - before):.6f}s，应为 {frame_duration:.6f}s"
    )
    print(f"  逐帧步进：{float(before):.3f}s → {float(after):.3f}s（一帧 {frame_duration:.3f}s）")

    # 锁定第 2 集片尾切点：锁定值必须跟着边界迁移
    workbench.review_pane.lock_requested.emit(2, True)
    app.processEvents()
    assert episodes[1].end_ticks in plan.locked_ticks, "锁定未生效"
    print(f"  锁定切点：{len(plan.locked_ticks)} 个")

    # 非法移动：停在相邻边界上再「设为结束切点」必须被拒绝且不改动方案
    ticks_before = list(plan.boundary_ticks)
    stage_view.goto(episodes[0].end_seconds)      # 30s 正是第 2 集片尾的相邻边界
    workbench.review_pane.boundary_set_requested.emit(2)
    app.processEvents()
    assert plan.boundary_ticks == ticks_before, "非法移动竟然改动了方案"
    assert timeline._error_index == 2, "非法切点应在时间轴上标红"
    assert inspector.problem_count() > 0, "非法操作的原因应进检查器"
    print("  非法移动已被阻止：方案未改动、时间轴标红、问题入检查器")

    # 合法移动：把第 2 集片尾往后挪一点（仍落在帧起点上）
    stage_view.goto(plan.episodes()[1].end_seconds + media.frame_start_seconds(5))
    workbench.review_pane.boundary_set_requested.emit(2)
    app.processEvents()
    assert plan.boundary_ticks != ticks_before, "合法移动应改动方案"
    assert plan.source == "manual", "人工移动后方案来源应标记为 manual"
    assert timeline._error_index is None, "合法移动后不应留错误标记"
    print(f"  合法移动：第 2 集片尾 → {float(plan.episodes()[1].end_seconds):.3f}s"
          f"（source=manual）")

    workbench.confirm_current()
    app.processEvents()
    assert plan.semantic_review_status == "confirmed"
    print("  审核状态已置为已确认")

    # ---- 导出对话框 --------------------------------------------------------
    print("[导出] 对话框：开始导出…")
    export_dialog = window.build_export_dialog()
    export_dialog.show()
    app.processEvents()
    export = export_dialog.form
    window.state.output_root = out_dir
    export.output_edit.setText(str(out_dir))
    export.crf_spin.setValue(20)
    export.preset_combo.setCurrentText("veryfast")
    export.refresh()
    app.processEvents()
    print(f"  {export.estimate.text()}")
    assert export.queue.rowCount() == plan.episode_count

    plan.semantic_review_status = "pending"  # 复位以便观察导出前检查触发
    export_dialog.start_export()
    ok = wait_until(
        app,
        lambda: export.worker and not export.worker.isRunning(),
        label="导出",
    )
    assert ok, "导出未完成"
    app.processEvents()

    exported_dir = out_dir / "episodes" / plan.export_directory_name()
    files = sorted(exported_dir.glob("*.mp4"))
    print(f"  产物：{len(files)} 个文件于 {exported_dir}")
    assert len(files) == plan.episode_count, f"应有 {plan.episode_count} 个成片"

    # 必须逐行确认导出**本轮**成功，不能只看文件存在
    statuses = [
        export.queue.item(row, 1).text() for row in range(export.queue.rowCount())
    ]
    assert all(status == "成功" for status in statuses), (
        f"导出队列表存在非成功项：{statuses}"
    )
    print(f"  导出队列状态：{statuses}")

    # 逐帧读回条码，确认帧身份正确
    all_indices: list[int] = []
    for path in files:
        indices = read_frame_indices(path)
        all_indices.extend(indices)
        expected_frames = probe_frame_count(path)
        print(f"    {path.name}: {len(indices)} 帧（容器声明 {expected_frames}）"
              f" 首帧={indices[0]} 末帧={indices[-1]}")
        assert len(indices) == expected_frames, "解码帧数与容器声明不符"

    expected = list(range(media.video.nb_frames))
    assert all_indices == expected, (
        f"拼接后的帧序列不等于源片序列，首个偏差位置："
        f"{next((i for i, v in enumerate(all_indices) if v != expected[i]), None)}"
    )
    print(f"  帧身份校验通过：各集拼接后正好覆盖源片全部 {media.video.nb_frames} 帧")

    measurement = measure_av_sync(files[0], out_dir, fps=float(media.video.nominal_fps))
    print(f"  音画同步：{measurement.describe(float(media.video.nominal_fps))}")
    assert measurement.within(1.0 / float(media.video.nominal_fps)), measurement.describe()

    # ---- 模型对话框 --------------------------------------------------------
    print("[模型] 对话框：清单、状态、目录…")
    model_dialog = window.build_model_dialog()
    model_dialog.show()
    app.processEvents()
    assert model_dialog.isVisible(), "模型对话框未显示"
    models = model_dialog.form
    rows = models.rows()
    print(f"  模型行数：{len(rows)}（语音转写 / 语义判断两组卡片）")
    assert rows, "模型清单不应为空"
    for row in rows:
        print(
            f"    {row['label']:22s} {row['size_text']:16s} {row['state']:26s}"
            f" {'● 使用中' if row['in_use'] else '○':6s} [{row['action']}]"
        )
    assert all(row["state"] for row in rows), "每行都应报出安装状态"
    assert all(row["action"] in ("下载", "继续下载", "删除") for row in rows), (
        "每行都应带行内动作（未装→下载 / 已装→删除）"
    )
    asr_active = [row for row in rows if row["in_use"] and row["kind"] == "whisper"]
    assert len(asr_active) == 1, f"转写组「使用中」应恰有一个，实际 {len(asr_active)}"
    assert len([row for row in rows if row["in_use"] and row["kind"] == "llm"]) <= 1, (
        "语义组「使用中」最多一个"
    )
    # 标签文本自带"使用中：/语义判断后端："前缀，这里不要再加一次
    print(f"  {models.asr_status.text()}")
    print(f"          {models.llm_status.text()}")
    print(f"  {models.stack_label.text()}")
    installed = [row["label"] for row in rows if row["installed"]]
    if installed:
        print(f"  已安装模型：{'、'.join(installed)}")
    else:
        # 不是失败：模型体积大、不入版本库，干净机器上就是全未安装。
        print("  本机未安装任何模型（属正常）；状态徽章已按实际探测结果填充")
    print(f"  模型目录：{models.dir_edit.text()}")
    print(f"  {models.free_label.text()}")
    models._refresh()
    app.processEvents()
    print("  模型清单刷新正常")
    model_dialog.close()
    export_dialog.close()
    app.processEvents()

    # 字幕接线必须被执行：该素材无对白，应明确报"未生成"，而不是静默跳过。
    export_report = [text for kind, title, text in DIALOGS if title == "导出结束"]
    assert export_report, "未记录到导出结束对话框"
    assert "字幕" in export_report[-1], (
        f"导出结束后应汇报字幕状态，实际：{export_report[-1]}"
    )
    print("  字幕状态已汇报：", [
        line for line in export_report[-1].splitlines() if line.startswith("字幕")
    ])

    print("\n对话框记录：")
    for kind, title, text in DIALOGS:
        print(f"  [{kind}] {title}｜{text.splitlines()[0][:80] if text else ''}")
    assert not [d for d in DIALOGS if d[0] == "critical"], "出现了严重错误对话框"

    print("\nGUI 冒烟测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())