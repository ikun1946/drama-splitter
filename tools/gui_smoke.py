"""GUI 冒烟测试：离屏跑通「导入 → 设置 → 分析 → 审核 → 导出」全链路。

用离屏平台（QT_QPA_PLATFORM=offscreen）无需显示器即可运行。
所有 QMessageBox 被替换为记录器，避免模态对话框在自动化环境中阻塞。

断言的是**结果**而不是"点了没报错"：导出后逐帧读回条码，确认帧身份正确。
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
from verify import measure_av_sync, probe_frame_count, read_frame_indices  # noqa: E402

DIALOGS: list[tuple[str, str, str]] = []


def _install_dialog_recorders() -> None:
    def record(kind):
        def inner(*args, **kwargs):
            title = args[1] if len(args) > 1 else ""
            text = args[2] if len(args) > 2 else ""
            DIALOGS.append((kind, str(title), str(text)))
            return None

        return staticmethod(inner)

    mw.QMessageBox.information = record("info")
    mw.QMessageBox.warning = record("warn")
    mw.QMessageBox.critical = record("critical")


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
    print("窗口构造成功，共", window.stack.count(), "页")

    from app.core.ffmpeg import check_encoders

    binaries = locate_ffmpeg()
    check_encoders(binaries)
    window.state.binaries = binaries

    # ---- 页1 导入 ------------------------------------------------------
    print("[页1] 导入并探测…")
    window.import_page._probe(str(asset))
    ok = wait_until(
        app,
        lambda: window.import_page._worker and not window.import_page._worker.isRunning(),
        label="媒体探测",
    )
    if not ok:
        return 1
    app.processEvents()
    media = window.state.media
    assert media is not None, "探测未产生媒体信息"
    print(f"  源片：{media.video.width}×{media.video.height} "
          f"{float(media.video.nominal_fps):.2f}fps {media.video.nb_frames} 帧")
    print(f"  音轨：{len(media.audio_tracks)} 条，已选：{window.state.audio_track_label}")
    print(f"  兼容性问题：{window.import_page.issues.rowCount()} 条")
    ok, message = window.import_page.can_continue()
    assert ok, f"导入页无法继续：{message}"

    # ---- 页2 设置 ------------------------------------------------------
    print("[页2] 设置分集目标（目标时长 30 秒，0% 浮动）…")
    window.nav.setCurrentRow(1)
    app.processEvents()
    page = window.settings_page
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
    ok, message = page.can_continue()
    assert ok, f"设置页无法继续：{message}"

    # ---- 页3 分析 ------------------------------------------------------
    print("[页3] 生成规则草案方案…")
    window.nav.setCurrentRow(2)
    app.processEvents()
    window.analysis_page._start()
    ok = wait_until(
        app,
        lambda: window.state.current_plan is not None,
        label="方案生成",
    )
    assert ok, "未生成方案"
    plan = window.state.current_plan
    print(f"  方案 v{plan.version}：{plan.episode_count} 集，"
          f"边界 {len(plan.boundary_ticks)} 个")
    for episode in plan.episodes():
        print(f"    第{episode.index:02d}集 "
              f"{float(episode.start_seconds):8.3f}s – {float(episode.end_seconds):8.3f}s "
              f"（{float(episode.duration_seconds):7.3f}s）")
    assert plan.coverage_valid(), "方案未通过覆盖校验"
    ok, message = window.analysis_page.can_continue()
    assert ok, f"分析页无法继续：{message}"

    # ---- 页4 审核 ------------------------------------------------------
    print("[页4] 审核页：列表、预览、微调…")
    window.nav.setCurrentRow(3)
    app.processEvents()
    review = window.review_page
    assert review.episode_table.rowCount() == plan.episode_count, (
        f"集列表应有 {plan.episode_count} 行，实际 {review.episode_table.rowCount()}"
    )
    print(f"  集列表 {review.episode_table.rowCount()} 行")
    print(f"  方案层校验：{review.problem_box.toPlainText().strip()}")

    # 预览帧必须真的解出来
    review.episode_table.selectRow(1)
    app.processEvents()
    image = review._decoder.frame_at(plan.episodes()[1].start_seconds)
    assert image is not None and not image.isNull(), "预览未取到解码帧"
    print(f"  预览帧尺寸：{image.width()}×{image.height()}")

    # 逐帧步进必须落在帧起点上
    review._goto(plan.episodes()[1].start_seconds)
    before = review._position
    review._step_frames(1)
    after = review._position
    frame_duration = 1 / float(media.video.nominal_fps)
    assert abs(float(after - before) - frame_duration) < 1e-6, (
        f"逐帧步进跨度为 {float(after - before):.6f}s，应为 {frame_duration:.6f}s"
    )
    print(f"  逐帧步进：{float(before):.3f}s → {float(after):.3f}s（一帧 {frame_duration:.3f}s）")

    # 锁定切点后再移动，锁定必须跟着走
    review.episode_table.selectRow(0)
    app.processEvents()
    review._lock(True)
    app.processEvents()
    assert plan.episodes()[0].end_ticks in plan.locked_ticks, "锁定未生效"
    print(f"  锁定切点：{len(plan.locked_ticks)} 个")

    # 尝试一次非法移动：越过相邻边界必须被拒绝且不改动方案
    ticks_before = list(plan.boundary_ticks)
    review._goto(plan.episodes()[1].start_seconds)
    review._set_boundary()
    app.processEvents()
    assert plan.boundary_ticks == ticks_before, "非法移动竟然改动了方案"
    print("  非法移动已被阻止，方案未被改动")

    review._confirm_episode()
    app.processEvents()
    assert plan.semantic_review_status == "confirmed"
    print("  审核状态已置为已确认")

    ok, message = review.can_continue()
    assert ok, f"审核页无法继续：{message}"

    # ---- 页5 导出 ------------------------------------------------------
    print("[页5] 导出…")
    window.nav.setCurrentRow(4)
    app.processEvents()
    export = window.export_page
    window.state.output_root = out_dir
    export.output_edit.setText(str(out_dir))
    export.crf_spin.setValue(20)
    export.preset_combo.setCurrentText("veryfast")
    export.refresh()
    app.processEvents()
    print(f"  {export.estimate.text()}")
    assert export.queue.rowCount() == plan.episode_count

    plan.semantic_review_status = "pending"  # 复位以便观察导出后状态
    export._start(None)
    ok = wait_until(
        app,
        lambda: export._worker and not export._worker.isRunning(),
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

    print("[页6] 模型页：清单、状态、目录…")
    window.nav.setCurrentRow(5)
    app.processEvents()
    models = window.model_page
    print(f"  表格行数：{models.table.rowCount()}")
    assert models.table.rowCount() > 0, "模型清单不应为空"
    rows = []
    for row in range(models.table.rowCount()):
        label = models.table.item(row, 0).text()
        status = models.table.item(row, 2).text()
        rows.append((label, status))
        print(f"    {label:34s} {status}")
    assert any("已安装" in status for _, status in rows), "测试机上至少应有一个已安装模型"
    print(f"  模型目录：{models.dir_edit.text()}")
    print(f"  {models.free_label.text()}")

    uninstalled = [
        row for row in range(models.table.rowCount())
        if "未安装" in models.table.item(row, 2).text()
    ]
    if uninstalled:
        models.table.selectRow(uninstalled[0])
        app.processEvents()
        spec = models._selected_spec()
        print(f"  选中未安装模型：{spec.label}｜目标目录 {spec.target_dir(models._models_root())}")
        assert spec is not None
    models._refresh()
    app.processEvents()
    print("  模型页刷新正常")

    print("\n对话框记录：")
    for kind, title, text in DIALOGS:
        print(f"  [{kind}] {title}｜{text.splitlines()[0][:80] if text else ''}")
    assert not [d for d in DIALOGS if d[0] == "critical"], "出现了严重错误对话框"

    print("\nGUI 冒烟测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
