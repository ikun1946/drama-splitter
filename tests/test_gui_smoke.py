"""界面层回归测试（离屏运行）。

只覆盖"构造 + 参数联动 + 可行性提示"这些廉价但易碎的部分；
包含真实编码的完整链路验证在 tools/gui_smoke.py，作为发布前的手工关卡。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面测试")

from fractions import Fraction  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.probe import probe_media  # noqa: E402
from app.core.model_manager import llm_spec, whisper_spec  # noqa: E402
from app.gui.stage import Stage  # noqa: E402
from app.core.settings import (
    CountPolicy,
    RangeMode,
    RangeSpec,
    SplitMode,
    SplitSettings,
)  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(scope="module")
def window(qapp):
    from app.gui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.workbench.stage_view.player.stop()
    win.close()


@pytest.fixture(autouse=True)
def message_recorder(monkeypatch):
    """把统一弹窗出口（app.gui.widgets.message）换成录制器。

    离屏环境里 QMessageBox 会永久阻塞——静态方法与实例 exec() 都一样，本
    项目实测挂过 25 分钟。因此整份界面测试都不允许真弹窗；需要断言弹窗内容
    的用例直接读这个夹具返回的列表。
    """
    from app.gui.widgets import message as message_mod

    recorded: list[tuple] = []

    def record(kind):
        def inner(*args, **kwargs):
            recorded.append((kind,) + args)
            return None

        return inner

    monkeypatch.setattr(message_mod, "information", record("information"))
    monkeypatch.setattr(message_mod, "warning", record("warning"))
    monkeypatch.setattr(message_mod, "critical", record("critical"))
    monkeypatch.setattr(message_mod, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(message_mod, "confirm_continue", lambda *a, **k: False)
    return recorded


class TestWindowConstruction:
    def test_single_page_workbench_has_three_columns(self, window):
        """单页工作台：一页三栏（左/中/右），没有页面栈与抽屉。"""
        wb = window.workbench
        assert wb.splitter.count() == 3, "工作台应由三栏组成"
        assert wb.inspector is not None, "右栏检查器常驻"
        assert not hasattr(window, "stack"), "页面栈已退场"
        assert window.menuBar().actions(), "菜单是设置/模型/导出的入口"

    def test_workbench_composes_import_and_analysis(self, window):
        wb = window.workbench
        assert wb.import_pane.parent() is not None
        assert wb.analysis_pane.parent() is not None
        assert wb.review_pane.parent() is not None
        assert wb.stage_view.parent() is not None

    def test_workbench_splitter_is_actually_resizable(self, window, qapp):
        """三栏分割条必须能被程序与用户调整。

        回归用例：子控件最小宽度之和超过可视宽度时，Qt 会静默忽略 setSizes()
        与 moveSplitter()——实测表现为 sizes() 恒等于均分。修法是左右两栏各套
        一层滚动容器。这里断言"能设得动"，而不是断言某个具体比例。
        """
        wb = window.workbench
        window.resize(1440, 900)
        window.show()          # 布局只在 show 之后才真正分配尺寸
        qapp.processEvents()
        try:
            wb.left_stack.setVisible(True)   # 空态会隐藏左栏；测分割器时显式显示
            qapp.processEvents()
            total = sum(wb.splitter.sizes())
            assert total > 0, "分割器未获得可用宽度"

            left = max(1, int(total * 0.2))
            center = int(total * 0.6)
            wb.splitter.setSizes([left, center, total - left - center])
            qapp.processEvents()
            got = wb.splitter.sizes()
            assert abs(got[0] - left) <= 4, (
                f"分割器忽略了 setSizes：请求左栏 {left}px，实际 {got[0]}px（{got}）"
                "——子控件最小宽度可能又超过了可用宽度"
            )
            assert got[1] > got[0] and got[1] > got[2], "中栏应拿到最多宽度"
        finally:
            window.hide()      # 恢复 fixture 原状，不影响同模块后续用例

    def test_dialog_does_not_replace_workbench(self, window, qapp):
        """设置 / 模型是对话框（真顶层窗口），打开时底下仍是工作台。"""
        dialog = window.build_settings_dialog()
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible(), "设置对话框应能直接显示（工厂 + show，不 exec）"
        assert window.workbench.isVisible() or not window.isVisible()
        dialog.close()

    def test_starts_in_empty_stage(self, window):
        """空态：阶段 EMPTY，底栏主动作是「选择源视频…」。"""
        window.state.reset_media()
        window.workbench.refresh()
        assert window.workbench.stage is Stage.EMPTY
        assert window.bottom.action_text().startswith("选择源视频")

    def test_analysis_requires_media(self, window, message_recorder):
        window.state.reset_media()
        ok, reason = window.workbench.can_start_analysis()
        assert not ok and "导入" in reason

        window.workbench.analysis_pane.start()
        assert any(kind == "warning" for kind, *_ in message_recorder), (
            "没有素材时应弹「尚未导入」而不是静默失败"
        )


class TestPlayback:
    """回归：播放按钮曾完全没有反应。

    根因是 ``StreamPlayer`` 里实例属性 ``self._frame_size = (0, 0)`` 与同名方法
    ``_frame_size()`` 撞名，属性把方法遮蔽，``self._frame_size()`` 抛 TypeError；
    异常发生在 Qt 槽里被打印后吞掉，界面上只表现为"点了没反应"。这段代码从最早
    的提交起就是这样，而当时的测试只调用单帧解码 ``frame_at``，从没真播过。
    """

    def test_frame_size_cache_and_source_switch(self, qapp):
        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_source("a.mp4", size=(1920, 1080))
        assert player._frame_size() == (960, 540), "已知分辨率时应直接算出输出尺寸"

        player.set_source("b.mp4", size=(640, 360))
        assert player._frame_size() == (640, 360), "换源必须清掉尺寸缓存，否则按旧尺寸解码"

        player.set_source(None)
        assert player._frame_size() == (0, 0)

    def test_play_streams_frames_and_advances(self, qapp, binaries, assets):
        """真播放 1.2 秒：必须产出画面且位置推进（不靠目测）。"""
        from PySide6.QtCore import QTimer

        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_source(assets["cfr"], size=(320, 180))
        player.set_fps(Fraction(25))
        frames: list = []
        errors: list[str] = []
        positions: list[float] = []
        player.frame_ready.connect(frames.append)
        player.error.connect(errors.append)
        player.position_changed.connect(positions.append)
        player.set_range(0.0, 2.0)
        player.play()

        QTimer.singleShot(1200, qapp.quit)
        qapp.exec()
        playing_position = player.position    # stop() 会把位置复位到起点，先记下来
        player.stop()

        assert not errors, f"播放不应报错：{errors}"
        assert frames, "播放应产出真实解码帧（此前一次都没有）"
        assert positions, "播放头位置应有回调（时间轴/底栏靠它刷新）"
        assert playing_position > 0.4, f"播放位置应推进，实际 {playing_position:.3f}s"
        assert max(positions) > playing_position - 0.2, "位置回调应覆盖播放过程"


def _import_and_wait(window, qapp, path, timeout: float = 30.0) -> None:
    """走真实的导入路径并等探针线程结束（离屏测试没有事件循环，只能手动泵）。"""
    import time

    window.workbench.import_pane.probe(str(path))
    worker = window.workbench.import_pane.worker
    deadline = time.time() + timeout
    while time.time() < deadline and worker.isRunning():
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()


class TestPreviewAfterImport:
    """回归：导入完成后预览区必须立刻有画面，且播放头在片头。

    此前 ``StageView.set_project()`` 只换了解码器/播放器源与时间轴，没有解一帧，
    画面区一直停在「尚未选择源文件」的占位文字上；要等用户点「下一帧」「−1s」
    或拖时间轴触发 ``goto()`` 才第一次出图——用户看到的就是"导入后必须点下一帧
    才显示视频画面"。这里直接复现导入路径并断言首帧已渲染。
    """

    @staticmethod
    def _clean(window):
        window.state.reset_media()
        window.workbench.refresh()

    def test_first_frame_rendered_without_stepping(self, window, qapp, binaries, assets):
        window.state.binaries = binaries
        self._clean(window)
        try:
            _import_and_wait(window, qapp, assets["cfr"])

            assert window.workbench.stage is Stage.READY, "前置：探测完成后应进入就绪态"
            preview = window.workbench.stage_view.preview
            assert preview._image is not None, (
                "导入完成后应立刻解出首帧，而不是等用户点「下一帧」"
            )
            assert preview.view.text() == "", "画面区不应仍显示占位文字"
            assert preview._frame_index == 0, "首帧应定位到第 0 帧"
        finally:
            # 窗口是 module 级共享的：导入会改变顶栏标题，必须复位，
            # 否则后面的用例（顶栏标题）会拿到被污染的初始值。
            self._clean(window)

    def test_switching_source_rewinds_playhead(self, window, qapp, binaries, assets):
        """换一部片子后播放头必须回到片头。

        回归：``StageView.position`` 原先跨换片保留，新片子会在上一部片子的
        播放头位置起播——预览/时间轴/播放起点三处同时停在错误位置。
        """
        window.state.binaries = binaries
        self._clean(window)
        try:
            _import_and_wait(window, qapp, assets["cfr"])
            stage = window.workbench.stage_view
            stage.goto(Fraction(60))
            assert stage.position == Fraction(60), "前置：播放头已移离片头"

            _import_and_wait(window, qapp, assets["nonzero_pts"])

            assert stage.position == Fraction(0), (
                f"换片后播放头应回到片头，实际 {float(stage.position):.3f}s"
            )
            assert stage.preview._frame_index == 0, "预览应显示新片子的首帧"
        finally:
            self._clean(window)


class TestPlayPauseToggle:
    """播放与暂停合并成一个键（按钮文案随状态变，暂停后再点续播）。

    原先控制条上是「▶ 播放本集」+「■ 停止」两个键，而 ``stop()`` 只停表、
    不复位播放头，和暂停没有可感知差别——用户要的就是一个键两态。
    """

    def test_no_stop_button_and_label_is_play(self, window):
        from PySide6.QtWidgets import QPushButton

        from app.gui.widgets.media_preview import MediaPreview

        preview = window.workbench.stage_view.preview
        labels = [b.text() for b in preview.findChildren(QPushButton)]
        assert "■ 停止" not in labels, "停止键应与暂停合并，不再单独存在"
        assert preview.btn_play.text() == MediaPreview.PLAY_TEXT

    def test_toggle_pauses_and_resumes_from_playhead(
        self, window, qapp, binaries, assets
    ):
        from PySide6.QtCore import QTimer

        from app.core.probe import probe_media
        from app.gui.widgets.media_preview import MediaPreview

        window.state.reset_media()
        window.state.media = probe_media(binaries, assets["cfr"])
        window.state.binaries = binaries
        window.workbench.refresh()

        stage = window.workbench.stage_view
        preview = stage.preview
        try:
            stage.goto(Fraction(10))
            stage.toggle_playback()
            assert stage.player.is_playing, "第一次点是播放"
            assert preview.btn_play.text() == MediaPreview.PAUSE_TEXT

            QTimer.singleShot(800, qapp.quit)
            qapp.exec()
            paused_at = stage.player.position

            stage.toggle_playback()          # 第二个状态：暂停
            assert not stage.player.is_playing
            assert preview.btn_play.text() == MediaPreview.PLAY_TEXT
            assert paused_at > 10.0, "播放头应真的往前走了"

            stage.toggle_playback()          # 再点：从暂停处续播
            assert stage.player.is_playing
            assert stage.player.position >= paused_at - 0.05, (
                "暂停后再点应接着播，而不是跳回片头/集首"
            )
        finally:
            stage.player.stop()
            window.state.reset_media()
            window.workbench.refresh()


class TestSettingsAutoFill:
    """设置表单：派生目标值只读并实时反算。

    - 按目标时长：时长是输入，**集数只读**且由 T÷D 反算（夹到可行区间）
    - 按目标集数：集数 是输入，**时长只读**且由 T÷N 反算
    反算值只用于展示/确认；内核在该模式下本来就只读另一个字段。
    """

    def _form_with_media(self, window, binaries, assets):
        window.state.binaries = binaries
        window.state.media = probe_media(binaries, assets["cfr"])   # 120 秒
        form = window.build_settings_dialog().form
        form.radio_by_duration.setChecked(True)
        form.range_percent.setChecked(True)
        form.tolerance_spin.setValue(0.0)
        return form

    def test_mode_a_fills_count_from_duration(self, window, binaries, assets):
        form = self._form_with_media(window, binaries, assets)
        form.duration_spin.setValue(30.0)

        assert not form.duration_spin.isReadOnly(), "目标时长是用户输入，应可编辑"
        assert form.count_spin.isReadOnly(), "按目标时长时集数是算出来的，应只读"
        assert form.count_spin.value() == 4, (
            f"120 秒 ÷ 30 秒应为 4 集，实际 {form.count_spin.value()}"
        )
        assert form._count_field._title.text() == "预计集数（自动）"
        assert "自动计算" in form._count_field._help.text()

    def test_mode_a_recomputes_live(self, window, binaries, assets):
        form = self._form_with_media(window, binaries, assets)
        for duration, expected in ((40.0, 3), (60.0, 2), (24.0, 5)):
            form.duration_spin.setValue(duration)
            assert form.count_spin.value() == expected, (
                f"D={duration} 应反算为 {expected} 集，实际 {form.count_spin.value()}"
            )

    def test_mode_b_fills_duration_from_count(self, window, binaries, assets):
        form = self._form_with_media(window, binaries, assets)
        form.radio_by_count.setChecked(True)
        form.count_spin.setValue(5)

        assert form.duration_spin.isReadOnly(), "按目标集数时时长是算出来的，应只读"
        assert not form.count_spin.isReadOnly(), "目标集数是用户输入，应可编辑"
        assert form.duration_spin.value() == pytest.approx(24.0, abs=1e-6), (
            f"120 秒 ÷ 5 集应为 24 秒，实际 {form.duration_spin.value()}"
        )
        assert form._duration_field._title.text() == "预计目标时长（自动）"
        assert form._count_field._title.text() == "目标集数 N"

    def test_no_solution_does_not_guess_a_value(self, window, binaries, assets):
        """无解时不猜集数：保留原值，理由交给可行性面板与 help 文案。"""
        form = self._form_with_media(window, binaries, assets)
        form.duration_spin.setValue(30.0)
        # 切到手动上下限并设成对 120 秒源片明显无解的区间（每集 200–300 秒）
        form.range_manual.setChecked(True)
        form.min_spin.setValue(200.0)
        form.max_spin.setValue(300.0)
        before = form.count_spin.value()
        assert "无解" in form._count_field._help.text()

        # 无解状态下继续动输入：不得写入任何猜出来的集数
        form.duration_spin.setValue(25.0)
        assert form.count_spin.value() == before, "无解时不应写入一个猜出来的集数"
        assert "无解" in form._count_field._help.text()

    def test_without_media_keeps_value_and_hints(self, window, qapp):
        window.state.reset_media()
        window.workbench.refresh()
        form = window.build_settings_dialog().form
        assert "导入源片后自动计算" in form._count_field._help.text()


class TestAllowedCountInterval:
    """允许集数区间：默认隐藏，只在「按目标集数 + 弹性集数」下出现且自洽。

    原先它一直显示但灰掉，默认值 58–62 对多数素材既无意义又与名义目标冲突
    （校验要求 N 落在区间内），一切到「弹性集数」就报"参数不完整"。
    """

    def _form(self, window, binaries, assets):
        window.state.binaries = binaries
        window.state.media = probe_media(binaries, assets["cfr"])
        return window.build_settings_dialog().form

    def test_hidden_unless_flexible_count_mode(self, window, binaries, assets):
        form = self._form(window, binaries, assets)
        assert form._flexible_row.isHidden(), "按目标时长模式下不该出现"

        form.radio_by_count.setChecked(True)
        assert form._flexible_row.isHidden(), "严格集数下也不该出现"

        form.policy_flexible.setChecked(True)
        assert not form._flexible_row.isHidden(), "弹性集数下应出现"

        form.radio_by_duration.setChecked(True)
        assert form._flexible_row.isHidden(), "切回按目标时长应再隐藏"

    def test_seeded_around_nominal_count(self, window, binaries, assets):
        form = self._form(window, binaries, assets)
        form.radio_by_count.setChecked(True)
        form.count_spin.setValue(5)
        form.policy_flexible.setChecked(True)

        assert (form.allowed_min.value(), form.allowed_max.value()) == (4, 6), (
            "刚切到弹性集数时应给出围绕名义目标的区间（N±1）"
        )
        text = form.feasibility.toPlainText()
        assert "参数不完整" not in text, "切到弹性集数不该立刻报参数不完整"
        assert "名义目标 5 集" in text

    def test_user_edit_is_not_overwritten(self, window, binaries, assets):
        form = self._form(window, binaries, assets)
        form.radio_by_count.setChecked(True)
        form.policy_flexible.setChecked(True)
        form.allowed_min.setValue(3)
        form.allowed_max.setValue(9)

        form.count_spin.setValue(6)   # 之后改名义目标，区间不应被自动覆盖
        assert (form.allowed_min.value(), form.allowed_max.value()) == (3, 9), (
            "用户亲手改过的区间不应再被自动跟随覆盖"
        )


class TestNoStrayTopLevelWindows:
    """回归：打开分集设置时不得弹出任何多余的小窗口。

    ``LabeledField`` 的后缀标签原先没有父级，构造时又调了
    ``setVisible(True)``——Qt 里这等于"显示一个真正的顶层窗口"，于是 4 个带
    单位（秒 / ％）的字段各弹出一个十几像素的小窗，这正是用户看到的"奇怪的小
    窗口"。这里用 app 级事件过滤器抓全过程被 show 过的窗口，比"事后看顶层窗口"
    更严：一闪而过的也逃不掉（事后它们已被重新设父并隐藏）。
    """

    def test_settings_dialog_shows_no_stray_window(self, window, qapp):
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtWidgets import QWidget

        shown: list[str] = []

        class _ShowSpy(QObject):
            def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
                # 离屏平台下同一事件也会发给 QWindow（没有 isWindow()），
                # 我们只关心"被显示成一个窗口的控件"
                if (
                    event.type() == QEvent.Type.Show
                    and isinstance(obj, QWidget)
                    and obj.isWindow()
                ):
                    shown.append(type(obj).__name__)
                return False

        spy = _ShowSpy()
        qapp.installEventFilter(spy)
        dialog = window.build_settings_dialog()
        dialog.show()
        qapp.processEvents()
        qapp.removeEventFilter(spy)
        try:
            assert set(shown) == {"SettingsDialog"}, (
                f"打开分集设置只应显示对话框本身，实际还弹出了：{shown}"
            )
        finally:
            dialog.close()
            qapp.processEvents()

    def test_suffix_still_lands_in_the_field_row(self, window):
        """后缀必须真的挂进字段行：修"小窗口"不能把单位（秒 / ％）修没了。"""
        from PySide6.QtWidgets import QLineEdit

        from app.gui.widgets import LabeledField

        field = LabeledField("目标时长", suffix="秒")
        field.attach(QLineEdit())
        assert field._suffix_lbl.text() == "秒"
        assert field._row.indexOf(field._suffix_lbl) >= 0, "后缀没挂进字段行"
        assert field._suffix_lbl.parent() is field, (
            "后缀标签必须有父级，否则 setVisible(True) 会把它变成顶层小窗口"
        )


class TestThemeAndTopBar:
    """第 3 步：运行时主题切换 + 顶栏动态标题。

    主题切换是全局 QSS 重注入，切不回去就是事故——这里两个方向各切一次。
    配置持久化用 tmp_path 替换 config_path，绝不污染开发机上的 config.json。
    """

    def test_theme_toggle_round_trip(self, window, qapp, monkeypatch, tmp_path):
        from app.gui import theme
        from app.gui.theme import apply_theme, current_mode

        # 配置写到临时目录，测试不碰真实 config.json
        monkeypatch.setattr(
            "app.core.app_config.config_path", lambda: tmp_path / "config.json"
        )
        start = current_mode()
        try:
            window._on_theme_toggled()
            qapp.processEvents()
            flipped = current_mode()
            assert flipped != start
            assert qapp.styleSheet() == theme.build_stylesheet(flipped)
            # 菜单里的勾选态要跟着走，否则用户看到的是错的当前主题
            assert window.action_dark.isChecked() == (flipped == "dark")

            # 再切回来，确认可逆
            window._on_theme_toggled()
            qapp.processEvents()
            assert current_mode() == start

            # 配置应记录最后一次的选择
            from app.core.app_config import AppConfig

            assert AppConfig.load().theme == start
        finally:
            # 无论断言成败，把主题拨回起点，不影响同模块其他用例
            apply_theme(mode="light")
            window.action_dark.setChecked(False)

    def test_theme_toggle_persists_to_config(self, monkeypatch, tmp_path):
        from app.core.app_config import AppConfig, config_path

        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("app.core.app_config.config_path", lambda: cfg_path)

        cfg = AppConfig()
        cfg.theme = "dark"
        cfg.save()
        reloaded = AppConfig.load()
        assert reloaded.theme == "dark"

        # 旧配置文件没有 theme 字段 → 回落浅色
        cfg_path.write_text('{"models_dir": null}', encoding="utf-8")
        assert AppConfig.load().theme == "light"
        # 非法值同样回落浅色
        cfg_path.write_text('{"theme": "solarized"}', encoding="utf-8")
        assert AppConfig.load().theme == "light"
        assert config_path()  # 引用存活，防误删

    def test_top_bar_shows_source_name(self, window):
        bar = window.app_bar
        app_title = bar.title_label.text()
        bar.set_source("某个很长很长的剧名_全集.mp4", "D:/drama/某个很长很长的剧名_全集.mp4")
        assert bar.title_label.text() == "某个很长很长的剧名_全集.mp4"
        assert "D:/drama" in bar.title_label.toolTip()
        bar.set_source(None)
        assert bar.title_label.text() == app_title

        # 超长文件名截断且带省略号
        long_name = "超" * 60 + ".mp4"
        bar.set_source(long_name, long_name)
        assert len(bar.title_label.text()) <= 37 and bar.title_label.text().endswith("…")
        bar.set_source(None)

    def test_model_button_targets_drawer_row(self, window, monkeypatch):
        """回归：设置/模型入口曾指向不存在的行，永远打不开。

        现在入口在「工具」菜单；这里断言菜单项存在且分别调到对应对话框工厂。
        """
        opened: list[str] = []
        monkeypatch.setattr(window, "open_settings_dialog", lambda: opened.append("settings"))
        monkeypatch.setattr(window, "open_model_dialog", lambda: opened.append("model"))

        tools_menu = next(
            a.menu() for a in window.menuBar().actions() if a.text() == "工具"
        )
        actions = {a.text(): a for a in tools_menu.actions()}
        assert "分集设置…" in actions and "模型管理…" in actions
        actions["分集设置…"].trigger()
        actions["模型管理…"].trigger()
        assert opened == ["settings", "model"]


class TestProjectPersistence:
    """第 4 步：项目保存 / 打开 / 自动保存（方案 §五十五 / §五十六）。

    open_project 的媒体恢复走真实 ffprobe（与既有 fixtures 同一素材），
    方案链重建在探针完成后异步发生——测试里用 wait_until 风格的事件泵等它。
    """

    @staticmethod
    def _wait(qapp, predicate, timeout=10.0):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            qapp.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _build_plan(media, cuts):
        from fractions import Fraction

        from app.core.plan import BoundaryPlan

        time_base = media.video_time_base
        ticks = [0] + [time_base.seconds_to_ticks(Fraction(str(c))) for c in cuts]
        ticks.append(media.duration_ticks())
        return BoundaryPlan(
            boundary_ticks=ticks, time_base=time_base, total_ticks=media.duration_ticks()
        )

    def test_save_then_open_roundtrip(self, window, qapp, binaries, assets, tmp_path):
        from fractions import Fraction

        from app.core.settings import RangeSpec, SplitSettings

        window.state.binaries = binaries
        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.workbench.import_pane.worker and not window.workbench.import_pane.worker.isRunning(),
        ), "探测超时"
        qapp.processEvents()

        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        window.state.settings = settings
        plan = self._build_plan(window.state.media, [30, 60, 90])
        window.state.add_plan(plan)
        window.state.audio_stream_index = 0

        path = tmp_path / "roundtrip.drama-project"
        assert window.save_project(path) == path
        assert path.exists()
        assert window.app_bar.save_label.text().startswith("已保存")

        # 破坏现场，再从项目文件恢复
        window.state.reset_media()
        window.state.settings = SplitSettings()
        window.open_project(path)
        assert self._wait(
            qapp,
            lambda: window.state.media is not None and bool(window.state.plans),
        ), "打开项目后未完成恢复"

        assert window.state.settings.target_duration_seconds == Fraction(30)
        assert window.state.settings.range.tolerance == Fraction(0)
        assert len(window.state.plans) == 1
        restored = window.state.plans[0]
        assert list(restored.boundary_ticks) == list(plan.boundary_ticks)
        assert window.state.project_path == path

    def test_autosave_only_writes_when_dirty(
        self, window, qapp, binaries, assets, tmp_path
    ):
        window.state.binaries = binaries
        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert self._wait(
            qapp,
            lambda: window.workbench.import_pane.worker and not window.workbench.import_pane.worker.isRunning(),
        )
        qapp.processEvents()

        target = tmp_path / "auto.drama-project"
        window.state.project_path = target
        window._autosave_tick()
        assert target.exists(), "首次自动保存应落盘"
        first = target.read_text(encoding="utf-8")

        # 内容没变 → 不重写
        window._autosave_tick()
        assert target.read_text(encoding="utf-8") == first

        # 方案变了 → 下一个 tick 落盘新内容
        window.state.add_plan(self._build_plan(window.state.media, [30, 60, 90]))
        window._autosave_tick()
        assert target.read_text(encoding="utf-8") != first

    def test_apply_settings_roundtrip(self, window):
        from fractions import Fraction

        from app.core.settings import (
            CountPolicy,
            RangeMode,
            RangeSpec,
            SplitMode,
            SplitSettings,
            Strategy,
        )

        original = SplitSettings(
            split_mode=SplitMode.TARGET_EPISODE_COUNT,
            target_episode_count=12,
            count_policy=CountPolicy.FLEXIBLE,
            allowed_count_min=10,
            allowed_count_max=15,
            target_duration_seconds=Fraction(45),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 10)),
            strategy=Strategy.SUSPENSE,
            allow_episode_exceptions=True,
        )
        form = window.build_settings_dialog().form
        form.apply_settings(original)
        collected = form.collect_settings()
        assert collected.split_mode == SplitMode.TARGET_EPISODE_COUNT
        assert collected.target_episode_count == 12
        assert collected.count_policy == CountPolicy.FLEXIBLE
        assert collected.allowed_count_min == 10
        assert collected.allowed_count_max == 15
        assert collected.range.tolerance == Fraction(1, 10)
        assert collected.strategy == Strategy.SUSPENSE
        assert collected.allow_episode_exceptions is True



class TestImportPreview:
    """回归：导入完成后预览必须出图，不能留着占位文案。

    用户报告「选好视频之后画面还写着尚未选择源文件」：绑定媒体时没有任何地方
    解码首帧（只有用户主动 seek 才有画面），而画面区最大的那块文字正好是这句
    占位提示——导入明明成功，看起来却像视频没选上。
    """

    def test_import_decodes_first_frame(self, window, qapp, binaries, assets):
        window.state.binaries = binaries
        window.state.reset_media()
        window.workbench.refresh()
        assert window.workbench.stage is Stage.EMPTY

        window.workbench.import_pane.probe(str(assets["cfr"]))
        assert TestProjectPersistence._wait(
            qapp,
            lambda: window.workbench.import_pane.worker
            and not window.workbench.import_pane.worker.isRunning(),
        ), "探测超时"
        qapp.processEvents()

        assert window.workbench.stage is Stage.READY
        preview = window.workbench.stage_view.preview
        assert preview.view.text() == "", (
            f"导入后画面仍显示占位文案：{preview.view.text()!r}——"
            "绑定媒体后应立即解出第一帧"
        )
        assert not preview.view.pixmap().isNull(), "导入后预览应显示真实解码帧"
        assert window.workbench.stage_view.position == 0, "新片源的播放头应归零"

        # 同一片源重刷（方案改动、锁切点都会走 set_project）不得把画面清回占位
        window.workbench.refresh()
        assert preview.view.text() == "" and not preview.view.pixmap().isNull()


class TestCloudModelCard:
    """云端模型（厂商 API）卡片：默认零外发，且"没测通不许启用"。

    连通性探测被替换成假实现——界面测试不允许真联网（也不该要求 Key）。
    """

    @staticmethod
    def _form(window, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.core.app_config.config_path", lambda: tmp_path / "config.json"
        )
        return window.build_model_dialog().form

    @staticmethod
    def _set_judge_model(form, model: str = "ep-test") -> None:
        """judge_model 已搬到「厂商管理」：测试里写进配置再回填本页控件。

        卡片上不再有"模型名"输入框（地址/协议/模型名统一在厂商管理里改），
        因此这里模拟"用户刚在厂商管理里填好并保存"的状态。
        """
        form._config.providers = [
            item for item in form._config.providers if str(item.get("id")) != "volc"
        ] + [{"id": "volc", "judge_model": model}]
        form._load_cloud_fields()

    def test_defaults_are_closed_and_local(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        assert form.cloud_enable.isChecked() is False
        assert form.cloud_allow.isChecked() is False
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND, "默认仍走本地"
        assert "未启用" in form.cloud_hint.text()
        assert "不含画面" in form.cloud_allow.text(), "外发范围必须在勾选那一刻就能读到"

    def test_probe_failure_refuses_to_enable(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND, AppConfig
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        self._set_judge_model(form)
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        monkeypatch.setattr(
            type(form),
            "_probe_cloud",
            lambda self, profile, capability: "鉴权失败（API Key 或资源权限）",
        )

        form._save_cloud()

        assert form.cloud_enable.isChecked() is False, "测不通就不该保持勾选"
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        assert "未通过" in form.log.toPlainText()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        profile = saved.profiles().get("volc")
        assert profile.enabled is False, "失败的档案不得被标记为已启用"
        assert profile.judge_model == "ep-test", "字段本身应已保存，用户不用重填"

    def test_probe_success_enables_and_persists(self, window, tmp_path, monkeypatch):
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        self._set_judge_model(form)
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)

        form._save_cloud()

        assert form.cloud_enable.isChecked() is True
        assert "已启用" in form.cloud_hint.text()
        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("judge") == "volc"
        profile = saved.profiles().get("volc")
        assert profile.enabled is True and profile.allow_upload is True
        assert profile.api_key == "sk-test"

    def test_asr_capability_switches_independently(self, window, tmp_path, monkeypatch):
        """两条能力各自测通、各自切换：只勾转写时语义判断仍走本地。"""
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_asr_enable.setChecked(True)      # 只勾转写
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)

        form._save_cloud()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("asr") == "volc"
        assert saved.backend_for("judge") == "local", "没勾的能力不许被切到云端"
        assert "转写" in form.cloud_hint.text() and "语义判断" not in form.cloud_hint.text()

    def test_asr_probe_failure_keeps_local_and_judge_stays(self, window, tmp_path, monkeypatch):
        """转写测不通只影响转写：语义判断已测通的不该被连带取消。"""
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        self._set_judge_model(form)
        form.cloud_key.setText("sk-test")
        form.cloud_allow.setChecked(True)
        form.cloud_enable.setChecked(True)
        form.cloud_asr_enable.setChecked(True)

        def probe(self, profile, capability):
            return "识别失败（45000151）" if capability == "asr" else None

        monkeypatch.setattr(type(form), "_probe_cloud", probe)
        form._save_cloud()

        saved = AppConfig.load(tmp_path / "config.json")
        assert saved.backend_for("asr") == "local"
        assert saved.backend_for("judge") == "volc"
        assert form.cloud_asr_enable.isChecked() is False
        assert form.cloud_enable.isChecked() is True

    def test_project_snapshot_never_carries_the_key(self, window, tmp_path, monkeypatch):
        """§6：方案快照只记"能否复现这次分集"，不含任何凭据。"""
        from app.core.app_config import AppConfig
        from app.gui.state import payload_text

        form = self._form(window, tmp_path, monkeypatch)
        self._set_judge_model(form)
        form.cloud_key.setText("sk-secret-value")
        monkeypatch.setattr(type(form), "_probe_cloud", lambda self, profile, capability: None)
        form.cloud_enable.setChecked(True)
        form.cloud_allow.setChecked(True)
        form._save_cloud()
        assert AppConfig.load(tmp_path / "config.json").providers, "前置：确实写进了配置"

        snapshot = payload_text(window.state.snapshot_payload())
        assert "sk-secret-value" not in snapshot


class TestWorkers:
    """工作线程的构造参数与信号契约。

    AnalysisWorker 一度漏传 settings，等到界面点「开始分析」才炸出来——
    那是用户最不想看到报错的地方。这里直接调用 run()（不经过 QThread.start），
    在同一线程内同步执行，异常不会丢失。
    """

    def test_analysis_worker_completes_and_emits_plan(self, binaries, assets):
        from fractions import Fraction

        from app.gui.workers import AnalysisWorker

        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        worker = AnalysisWorker(binaries, media, 0, settings)
        outcome: dict = {}

        worker.stage_started.connect(lambda name: outcome.setdefault("stages", []).append(name))
        worker.stage_log.connect(lambda text: outcome.setdefault("log", []).append(text))
        worker.completed.connect(
            lambda plan, report, problems, candidates, reviews: outcome.update(
                plan=plan, candidates=candidates, reviews=reviews
            )
        )
        worker.failed.connect(lambda message: outcome.setdefault("failed", message))

        worker.run()  # 同步执行，异常不会被线程吞掉

        assert "failed" not in outcome, f"分析线程失败：{outcome['failed']}"
        assert "plan" in outcome, f"未产出方案：{outcome.get('stages')}"
        assert outcome["candidates"] is not None
        assert "字幕解析" in outcome["stages"]
        assert "语音转写" in outcome["stages"]
        assert "候选点" in outcome["stages"]

    def test_analysis_worker_reports_missing_model_as_log_not_crash(
        self, binaries, assets, monkeypatch
    ):
        """模型缺失是可恢复状态：应作为日志说明跳过转写，而不是让分析失败。

        §7.2 要求"无音轨时跳过 ASR、启用画面分析和人工审核"——模型缺失同理。
        """
        import app.core.asr as asr_module
        from fractions import Fraction

        from app.gui.workers import AnalysisWorker

        media = probe_media(binaries, assets["cfr"])
        settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(30),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(0)),
        )
        worker = AnalysisWorker(binaries, media, 0, settings)
        outcome: dict = {}

        def refuse(*args, **kwargs):
            raise RuntimeError("模型未下载（测试模拟）")

        monkeypatch.setattr(asr_module.AsrEngine, "transcribe_cached", refuse)
        worker.stage_log.connect(lambda text: outcome.setdefault("log", []).append(text))
        worker.failed.connect(lambda message: outcome.setdefault("failed", message))
        worker.completed.connect(
            lambda plan, report, problems, candidates: outcome.update(plan=plan)
        )

        worker.run()

        assert "failed" not in outcome, "模型缺失不应让分析失败"
        assert any("跳过语音转写" in line for line in outcome.get("log", []))
        assert "plan" in outcome


class TestSettingsPageBehaviour:
    def test_feasibility_updates_live(self, window, binaries, assets):
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media
        window.state.binaries = binaries

        page = window.build_settings_dialog().form
        page.radio_by_duration.setChecked(True)
        page.range_percent.setChecked(True)
        page.duration_spin.setValue(30.0)
        page.tolerance_spin.setValue(0.0)

        text = page.feasibility.toPlainText()
        # 120 秒 / 目标是 30 秒、0% 浮动 → 只能分成 4 集
        assert "可行集数范围：4 – 4 集" in text
        assert "浮动比例为 0%" in text, "0% 浮动必须给出提示"
        assert "参数可行" in text

    def test_recommend_mode_takes_over_strategy_choice(self, window, binaries, assets):
        """推荐方案＝分析时自动比较三种策略，此时手选「策略」应置灰并说明原因。

        回归：这个选项原先只是换了个名字，内核与「按目标时长」一字不差，
        手选的「策略」照样生效——用户以为选了推荐，其实什么都没变。
        """
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form

        page.radio_recommend.setChecked(True)
        assert page.collect_settings().split_mode == SplitMode.RECOMMEND
        assert not page.strategy_combo.isEnabled(), "推荐模式下不该再手选策略"
        assert "自动比较三种策略" in page._strategy_field._help.text()

        page.radio_by_duration.setChecked(True)
        assert page.strategy_combo.isEnabled(), "切回按目标时长后策略要恢复可选"
        assert page._strategy_field._help.text() == ""

    def test_infeasible_parameters_block_progress(self, window, binaries, assets):
        """§5.1 参数无解必须在调用分析之前就拦住（§17.4），且提示要可操作。"""
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media

        page = window.build_settings_dialog().form
        page.radio_by_count.setChecked(True)
        page.policy_exact.setChecked(True)
        page.count_spin.setValue(60)
        page.range_manual.setChecked(True)
        page.min_spin.setValue(80.0)
        page.max_spin.setValue(100.0)

        text = page.feasibility.toPlainText()
        # 严格 60 集、每集不低于 80 秒 → 至少需要 4800 秒，而源片只有 120 秒
        assert "至少需要 4800.0 秒" in text, f"提示未给出具体的时长缺口：\n{text}"
        assert "当前只有 120.0 秒" in text
        assert "当前参数无解" in text

        ok, message = page.can_continue()
        assert not ok
        assert "至少需要 4800.0 秒" in message

    def test_empty_episode_range_message_is_meaningful(self, window, binaries, assets):
        """集长范围与总时长冲突时，不得打印 "2 – 1 集" 这类无意义区间。"""
        media = probe_media(binaries, assets["cfr"])
        window.state.media = media

        page = window.build_settings_dialog().form
        page.radio_by_duration.setChecked(True)
        page.range_manual.setChecked(True)
        page.min_spin.setValue(200.0)
        page.max_spin.setValue(300.0)

        text = page.feasibility.toPlainText()
        assert "可行集数范围：无" in text
        assert "– 1 集" not in text and "2 – " not in text
        assert "当前参数无解" in text

    def test_mode_switch_updates_settings_object(self, window, binaries, assets):
        """界面控件是唯一事实来源，切换后设置对象必须同步。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form

        page.radio_by_count.setChecked(True)
        page.policy_flexible.setChecked(True)
        settings = page.collect_settings()
        assert settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
        assert settings.count_policy == CountPolicy.FLEXIBLE

        page.range_manual.setChecked(True)
        settings = page.collect_settings()
        assert settings.range.mode == RangeMode.MANUAL

    def test_strict_mode_ignores_manual_range_for_target(self, window, binaries, assets):
        """§4.1 两种范围来源互斥：百分比模式下手动上下限不参与计算。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form
        page.radio_by_count.setChecked(True)
        page.range_percent.setChecked(True)
        page.tolerance_spin.setValue(20.0)
        page.count_spin.setValue(4)

        settings = page.collect_settings()
        derived = settings.derive(Fraction(120))
        assert derived.min_duration == Fraction(24)
        assert derived.max_duration == Fraction(36)


class TestSettingsDialogRollback:
    """设置对话框的「取消即回滚」语义。

    表单是实时写回 ``state.settings`` 的（控件一变就 collect），所以"取消"
    必须是显式回滚，否则用户试出来的参数会被当成确认值留下。
    """

    def test_cancel_restores_snapshot(self, window):
        from fractions import Fraction

        from app.core.settings import SplitSettings

        window.state.settings = SplitSettings(target_duration_seconds=Fraction(45))
        dialog = window.build_settings_dialog()
        form = dialog.form
        assert form.duration_spin.value() == 45.0, "打开时应把当前参数回填到控件"

        form.duration_spin.setValue(12.0)     # 用户试参数（实时写回）
        assert window.state.settings.target_duration_seconds == Fraction(12)

        dialog.cancel_and_close()
        assert window.state.settings.target_duration_seconds == Fraction(45), (
            "「取消」应把参数恢复成打开时的快照"
        )

    def test_close_button_also_rolls_back(self, window):
        """✕ / Esc 与「取消」同语义（reject 路径）。"""
        from fractions import Fraction

        from app.core.settings import SplitSettings

        window.state.settings = SplitSettings(target_duration_seconds=Fraction(30))
        dialog = window.build_settings_dialog()
        dialog.form.duration_spin.setValue(8.0)
        assert window.state.settings.target_duration_seconds == Fraction(8)

        dialog.reject()
        assert window.state.settings.target_duration_seconds == Fraction(30)


class TestWheelDoesNotChangeValues:
    """滚轮不改值：数值框与下拉框把滚轮量交给滚动区，页面照常滚动。

    回归用例：主题里 QLineEdit 与 QComboBox 外观一致，用户以为停在"文本框"
    上滚页面，实际停在值控件上。Qt 默认让 QComboBox / QAbstractSpinBox 用滚轮
    改值并**吞掉**事件，于是页面既滚不动、值还被悄悄改掉。NoWheel* 三个子类
    把滚轮量直接写进最近的滚动区。
    """

    @staticmethod
    def _wheel_event(widget, angle_y: int = -120):
        from PySide6.QtCore import QPoint, QPointF, Qt

        center = QPointF(widget.rect().center())
        return QWheelEvent(
            center, center, QPoint(0, 0), QPoint(0, angle_y),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )

    def _scroll_area_with(self, qapp, widget):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from app.gui.widgets import scrollable

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(widget)
        for index in range(30):
            layout.addWidget(QLabel(f"占位 {index}"))

        area = scrollable(content)
        area.resize(320, 200)
        area.show()
        qapp.processEvents()
        return area

    def test_spinbox_wheel_scrolls_instead_of_stepping(self, qapp):
        from app.gui.widgets import NoWheelSpinBox

        spin = NoWheelSpinBox()
        spin.setRange(1, 9999)
        spin.setValue(60)
        area = self._scroll_area_with(qapp, spin)
        try:
            bar = area.verticalScrollBar()
            bar.setValue(100)
            before_value, before_scroll = spin.value(), bar.value()

            qapp.sendEvent(spin, self._wheel_event(spin))
            qapp.processEvents()

            assert spin.value() == before_value, "滚轮不得改变数值"
            assert bar.value() == before_scroll + 120, "滚轮量应交给滚动区（页面照常滚）"
        finally:
            area.close()

    def test_combo_wheel_does_not_switch_option(self, qapp):
        from app.gui.widgets import NoWheelComboBox

        combo = NoWheelComboBox()
        combo.addItems(["a", "b", "c"])
        combo.setCurrentIndex(1)
        area = self._scroll_area_with(qapp, combo)
        try:
            bar = area.verticalScrollBar()
            bar.setValue(100)
            before_index, before_scroll = combo.currentIndex(), bar.value()

            qapp.sendEvent(combo, self._wheel_event(combo))
            qapp.processEvents()

            assert combo.currentIndex() == before_index, "滚轮不得切换下拉选项"
            assert bar.value() == before_scroll + 120
        finally:
            area.close()


class TestExportDialogContracts:
    """§37 的纯逻辑：返回审核时的定位目标、前置检查文案。"""

    def test_review_target_points_at_problem_episode(self):
        from app.core.plan import BoundaryPlan, PlanProblem
        from app.core.timebase import TimeBase
        from app.gui.panes.export_form import review_target

        plan = BoundaryPlan.uniform(4, TimeBase(1, 25), 3000)   # 0/30/60/90/120 秒
        episode, ms = review_target(
            plan, [PlanProblem(code="X", level="warn", message="超长", episode=3)]
        )
        assert episode == 3
        assert ms == 90_000, "问题落在第 3 集片尾（90 秒）"

    def test_review_target_falls_back_without_episode(self):
        from app.core.plan import BoundaryPlan, PlanProblem
        from app.core.timebase import TimeBase
        from app.gui.panes.export_form import review_target

        plan = BoundaryPlan.uniform(4, TimeBase(1, 25), 3000)
        episode, ms = review_target(
            plan, [PlanProblem(code="Y", level="warn", message="全局问题")]
        )
        assert (episode, ms) == (0, 0), "没有集号时只回审核态、不定位"

    def test_precheck_prompt_mentions_both_counts(self):
        from app.gui.panes.export_form import precheck_prompt

        text = precheck_prompt([], 3)
        assert "3 个切点未审核" in text
        assert "继续" in text and "返回审核" in text


class _FakeSignal:
    def connect(self, slot):
        pass


class _FakeWorker:
    """ExportWorker 替身：记录实例化入参，start() 不启动真线程。"""

    instances: list["_FakeWorker"] = []
    episode_done = _FakeSignal()
    completed = _FakeSignal()
    failed = _FakeSignal()
    finished_ok = _FakeSignal()

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _FakeWorker.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        pass

    def isRunning(self):
        return False

    def wait(self, ms=0):
        pass


class TestStep5Refinement:
    """第 5 步：导出前置检查（§37）、快捷键（§54）、检查器跳转、按钮压扁回归。"""

    def _seed(self, window, binaries, assets):
        """给共享的 module 级窗口塞一份干净的 4 集方案现场。"""
        window.state.reset_media()
        window.state.plans = []
        window.state.plan_cursor = 0
        window.state.exported = {}
        window.state.output_root = None
        media = probe_media(binaries, assets["cfr"])
        plan = TestProjectPersistence._build_plan(media, [30, 60, 90])
        window.state.media = media
        window.state.binaries = binaries
        window.state.add_plan(plan)
        window.workbench.refresh()
        return media, plan

    def test_export_precheck_counts_unreviewed_cuts(self, window, binaries, assets):
        """§37：未确认方案 → 未审核切点数 = 集数 - 1（首尾是片头片尾）。"""
        from app.gui.panes.export_form import export_precheck

        _, plan = self._seed(window, binaries, assets)
        blocking, warnings, unreviewed = export_precheck(plan, window.state.settings)
        assert blocking == []
        assert warnings == []
        assert unreviewed == 3

        plan.semantic_review_status = "confirmed"
        _, _, unreviewed = export_precheck(plan, window.state.settings)
        assert unreviewed == 0

    def test_precheck_dialog_back_to_review(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：前置检查选「返回审核」→ 关对话框、回审核态、不导出。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        assert plan.semantic_review_status == "pending"

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "review"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()
        qapp.processEvents()

        assert window.workbench.stage is Stage.REVIEW, "应回到审核态（左栏切回集列表）"
        assert not _FakeWorker.instances, "不应启动导出线程"
        assert not dialog.isVisible(), "选「返回审核」应关闭导出对话框"

    def test_precheck_dialog_continue_exports(self, window, qapp, binaries, assets, tmp_path, monkeypatch):
        """§37：选「继续导出」→ 走到启动导出（用户有意忽略未审核项）。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        self._seed(window, binaries, assets)
        window.state.output_root = tmp_path

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "continue"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()

        assert len(_FakeWorker.instances) == 1, "继续导出应启动一次导出线程"
        worker = _FakeWorker.instances[0]
        assert worker.kwargs.get("only_episodes") is None

    def test_hard_constraints_still_block_export(
        self, window, qapp, binaries, assets, tmp_path, monkeypatch, message_recorder
    ):
        """§37：硬约束问题仍直接拦截，连前置检查都不给。"""
        from app.gui.dialogs import ExportDialog
        from app.gui.panes import export_form as ep

        _, plan = self._seed(window, binaries, assets)
        window.state.output_root = tmp_path
        # 破坏硬约束：起点不再从 0 开始 → NOT_START_AT_ZERO（阻断）
        plan.boundary_ticks = [plan.boundary_ticks[0] + 1000, plan.boundary_ticks[-1]]

        monkeypatch.setattr(ep, "ExportWorker", _FakeWorker)
        _FakeWorker.instances = []
        monkeypatch.setattr(
            ExportDialog, "request_precheck", lambda self, warnings, unreviewed: "continue"
        )

        dialog = window.build_export_dialog()
        dialog.show()
        dialog.start_export()

        assert any(kind == "warning" for kind, *_ in message_recorder), "硬约束应弹警告"
        assert not _FakeWorker.instances, "硬约束问题必须拦住导出"

    def test_shortcut_sequences_registered(self, window):
        """§54：Ctrl+E / Enter / N / P / F11 / Esc 必须注册成快捷键。"""
        from PySide6.QtGui import QShortcut

        sequences = {sc.key().toString() for sc in window.findChildren(QShortcut)}
        for expected in (
            "Ctrl+E", "N", "P", "Return", "Enter", "Space", "Ctrl+S", "F11", "Esc"
        ):
            assert expected in sequences, f"缺少快捷键 {expected}（现有：{sorted(sequences)}）"

    def test_f11_toggles_fullscreen(self, window):
        """F11：全屏 ↔ 常规真实翻转（窗口状态标志在 show 时同步置位）。"""
        assert not window.isFullScreen()
        window._toggle_fullscreen()
        assert window.isFullScreen(), "第一次 F11 应进入全屏"
        window._toggle_fullscreen()
        assert not window.isFullScreen(), "第二次 F11 应退出全屏"

    def test_esc_exits_fullscreen_first(self, window, qapp):
        """Esc 分层：全屏时 Esc 只退全屏，不关对话框（优先级最高）。"""
        dialog = window.build_settings_dialog()
        dialog.show()
        qapp.processEvents()
        try:
            window._toggle_fullscreen()
            window._escape_pressed()
            assert not window.isFullScreen(), "全屏 + 对话框同开时 Esc 应先退全屏"
            assert dialog.isVisible(), "同一次 Esc 不该顺手把对话框也关掉"
        finally:
            dialog.close()
            qapp.processEvents()

    def test_esc_closes_open_dialog(self, window, qapp):
        """Esc 分层：非全屏时 Esc 关闭打开中的对话框。"""
        dialog = window.build_model_dialog()
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible(), "前置：对话框已打开"
        window._escape_pressed()
        qapp.processEvents()
        assert not dialog.isVisible(), "Esc 应关闭对话框"

    def test_ctrl_e_opens_export_dialog_and_warns_without_plan(
        self, window, qapp, monkeypatch, message_recorder
    ):
        """Ctrl+E：无方案时打开导出对话框并给「请先完成导入、分析与审核」。"""
        window.state.reset_media()
        window.state.plans = []
        window.state.plan_cursor = 0
        window.workbench.refresh()

        window._start_export()

        assert window.export_dialog is not None, "Ctrl+E 应打开导出对话框"
        assert any(
            "请先完成导入、分析与审核" in str(call) for call in message_recorder
        ), f"应给出可操作提示，实际弹窗：{message_recorder}"
        window.export_dialog.close()

    def test_return_confirms_episode(self, window, qapp, binaries, assets):
        """§54：回车 = 确认当前集（快捷键挂在工作台上，走同一条确认路径）。"""
        from PySide6.QtGui import QShortcut

        _, plan = self._seed(window, binaries, assets)
        assert plan.semantic_review_status == "pending"

        return_shortcuts = [
            sc for sc in window.findChildren(QShortcut)
            if sc.parent() is window.workbench and sc.key().toString() == "Return"
        ]
        assert len(return_shortcuts) == 1, "Return 快捷键应唯一且挂在工作台上"
        return_shortcuts[0].activated.emit()

        assert plan.semantic_review_status == "confirmed", "回车应触发确认当前集"

    def test_inspector_cycles_jumpable_issues(self, window):
        """N/P 巡航：只走有定位的问题项，N/P 方向相反且可循环。"""
        from app.gui.widgets import InspectorIssue

        inspector = window.workbench.inspector
        got = []
        inspector.jump_requested.connect(got.append)
        inspector.set_issues([
            InspectorIssue(title="a", target_ms=1000),
            InspectorIssue(title="b", target_ms=2000),
            InspectorIssue(title="c"),   # 无定位 → 巡航跳过
        ])

        window._cycle_issue(1)
        window._cycle_issue(1)
        window._cycle_issue(-1)
        assert got == [1000, 2000, 1000], f"巡航顺序错误：{got}"

    def test_jump_requested_opens_review_page_at_position(self, window, qapp, binaries, assets):
        """检查器「查看」：选中所在集、定位到问题时间点。"""
        self._seed(window, binaries, assets)

        window.workbench._on_jump_requested(45_000)
        assert window.workbench.selected_episode == 2, "45s 应落在第 2 集（30-60s）"
        assert window.workbench.stage_view.position == Fraction(45)

    def test_review_ops_buttons_have_hard_minimums(self, window):
        """回归：QSS min-height 不进 minimumSizeHint，按钮曾被布局压成细条、
        文字互相叠印（docs/screenshots/04-审核.png 缺陷）。"""
        page = window.workbench.review_pane
        for btn in (
            page.btn_set_boundary,
            page.btn_lock,
            page.btn_unlock,
            page.btn_confirm,
            page.btn_snap,
        ):
            assert btn.minimumHeight() >= 32, f"「{btn.text()}」缺少显式最小高度"


# ---------------------------------------------------------------------------
# 模型界面：两组卡片 + 「使用中」（用户曾看不出"选了没"与"在用哪个"）
# ---------------------------------------------------------------------------


def _touch_big_file(path: Path, megabytes: int) -> None:
    """快速造一个大文件（truncate 不实写内容，避免为体积下限白写几百 MB）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(megabytes * 1024 * 1024)


def _install_whisper(root: Path, tier: str) -> None:
    spec = whisper_spec(tier)
    directory = spec.target_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    _touch_big_file(directory / "model.bin", 1)
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    (directory / "vocabulary.txt").write_text("a", encoding="utf-8")


def _install_llm(root: Path, size: str, quant: str) -> None:
    spec = llm_spec(size, quant)
    _touch_big_file(spec.target_dir(root) / spec.big_file, 64)


class TestModelFormChoices:
    """模型界面：只测"看得清、控得住"这两件事——分组与「使用中」。"""

    @pytest.fixture
    def model_form(self, window, qapp, tmp_path, monkeypatch):
        """指向临时模型目录与临时配置：绝不碰真实的 config.json / models/。"""
        from app.core.app_config import AppConfig

        models_root = tmp_path / "models"
        models_root.mkdir()
        config_file = tmp_path / "config.json"
        real_load = AppConfig.load.__func__

        def fake_load(cls, path=None):
            return real_load(cls, config_file)

        monkeypatch.setattr(AppConfig, "load", classmethod(fake_load))

        dialog = window.build_model_dialog()
        form = dialog.form
        form._config.models_dir = str(models_root)
        form._config_path = config_file      # 保存也写到临时文件
        form.dir_edit.setText(str(models_root))
        form.refresh()
        yield form
        dialog.close()
        qapp.processEvents()

    def test_two_group_cards_with_explicit_actions(self, model_form):
        """两组卡片；每行都有行内动作与状态徽章，不再靠"选中态"猜。"""
        assert model_form.asr_card.title() == "语音转写"
        assert model_form.llm_card.title() == "语义判断"

        asr = [row for row in model_form.rows() if row["kind"] == "whisper"]
        assert [row["label"] for row in asr] == [
            "whisper-tiny", "whisper-base", "whisper-small",
            "whisper-medium", "whisper-large-v3",
        ], "五档必须全列出（未安装的也要看得见体积与下载入口）"
        assert all(row["action"] == "下载" for row in asr)
        assert all(not row["installed"] for row in asr), "临时目录里什么都没装"

    def test_default_tier_is_marked_in_use(self, model_form):
        """转写组必须恰有一个「使用中」，且默认落在默认档位。"""
        from app.core.model_manager import default_whisper_tier

        active = [row for row in model_form.rows() if row["in_use"]]
        assert len(active) == 1, f"「使用中」应唯一，实际 {len(active)}"
        assert active[0]["label"] == f"whisper-{default_whisper_tier()}"
        assert active[0]["kind"] == "whisper"
        # 没装也要写明后果，而不是让分析悄悄跳过转写
        assert "使用中：whisper-" in model_form.asr_status.text()
        assert "未安装" in model_form.asr_status.text()

    def test_uninstalled_rows_cannot_be_picked(self, model_form):
        """未安装的行不可设为「使用中」：避免"选了却用不了"的假状态。"""
        from app.core.model_manager import default_whisper_tier

        rows = model_form.rows()
        assert all(not row["pickable"] for row in rows if not row["installed"])
        tiny = next(row for row in model_form._rows if row.spec.key == "whisper:tiny")
        tiny.radio.click()   # 对 disabled 的圆钮无效
        assert model_form.active_whisper_tier() == default_whisper_tier()

    def test_pick_installed_tier_persists(self, model_form, window, qapp):
        """点已安装档位 → 立刻生效、写进配置，重开对话框仍是它。"""
        _install_whisper(model_form._models_root(), "tiny")
        model_form.refresh()

        tiny = next(row for row in model_form._rows if row.spec.key == "whisper:tiny")
        assert tiny.radio.isEnabled(), "已安装的档位应当可设为使用中"
        assert tiny.button.text() == "删除", "已安装的行给删除入口"
        tiny.radio.click()

        assert model_form.active_whisper_tier() == "tiny"
        assert "使用中：whisper-tiny" in model_form.asr_status.text()
        saved = json.loads(model_form._config_path.read_text(encoding="utf-8"))
        assert saved["asr_model_tier"] == "tiny", "必须落盘，重开才记得住"

        dialog = window.build_model_dialog()
        try:
            opened = dialog.form
            opened._config_path = model_form._config_path
            opened.dir_edit.setText(str(model_form._models_root()))
            opened.refresh()
            assert opened.active_whisper_tier() == "tiny"
            chosen = [row["label"] for row in opened.rows() if row["in_use"]]
            assert chosen == ["whisper-tiny"], f"重开后应仍指向 tiny，实际 {chosen}"
        finally:
            dialog.close()
            qapp.processEvents()

    def test_llm_rows_come_from_installed_gguf(self, model_form):
        """语义组列出本机实际装了哪些档位（清单里只有一个预置项，必须扫目录）。"""
        _install_llm(model_form._models_root(), "2B", "Q4_K_M")
        _install_llm(model_form._models_root(), "2B", "Q8_0")
        model_form.refresh()

        llm = [row for row in model_form.rows() if row["kind"] == "llm"]
        assert [row["label"] for row in llm] == [
            "Qwen3.5-2B Q4_K_M", "Qwen3.5-2B Q8_0",
        ]
        # 未显式指定时也要指出"正在用哪个"（否则用户又要猜）
        assert len([row for row in llm if row["in_use"]]) == 1
        assert "使用中：" in model_form.llm_status.text()

        target = next(row for row in model_form._rows if row.spec.key == "llm:2B:Q8_0")
        target.radio.click()
        assert model_form.active_llm_key() == "llm:2B:Q8_0"
        saved = json.loads(model_form._config_path.read_text(encoding="utf-8"))
        assert saved["llm_model_key"] == "llm:2B:Q8_0"
        assert "Q8_0" in model_form.llm_status.text()

    def test_unrecognized_gguf_is_reported(self, model_form):
        """解析不出档位的 gguf 不能静默消失：文件在、界面里看不见才最坑人。"""
        _install_llm(model_form._models_root(), "2B", "Q4_K_M")
        stray = model_form._models_root() / "llm" / "other-GGUF"
        _touch_big_file(stray / "my-custom.gguf", 64)
        model_form.refresh()

        # 未 show 的窗口上 isVisible() 恒为 False，所以看的是"有没有被显式藏起来"
        assert not model_form.unknown_label.isHidden()
        assert "my-custom.gguf" in model_form.unknown_label.text()
        assert [row["label"] for row in model_form.rows() if row["kind"] == "llm"] == [
            "Qwen3.5-2B Q4_K_M"
        ]


class TestActiveModelWiring:
    """「使用中」必须真的被分析线程采用——只写进配置好看是不算数的。"""

    @pytest.fixture
    def patched_config(self, tmp_path, monkeypatch):
        """把 AppConfig.load 指到临时配置，返回 (配置文件, 保存函数)。"""
        from app.core.app_config import AppConfig

        config_file = tmp_path / "config.json"
        real_load = AppConfig.load.__func__
        monkeypatch.setattr(
            AppConfig, "load",
            classmethod(lambda cls, path=None: real_load(cls, config_file)),
        )
        return config_file

    def test_active_whisper_tier_follows_config(self, patched_config):
        from app.core.app_config import AppConfig
        from app.core.model_manager import default_whisper_tier
        from app.gui.workers import active_whisper_tier

        assert active_whisper_tier() == default_whisper_tier(), "未配置时用默认档位"
        AppConfig(asr_model_tier="tiny").save(patched_config)
        assert active_whisper_tier() == "tiny", "配置里的档位必须被分析线程采用"

    def test_configured_ollama_model_follows_pinned_key(self, tmp_path, patched_config):
        """语义模型的「使用中」要落到 Ollama 模型名上（命名规则与自动路径一致）。"""
        from app.core.app_config import AppConfig
        from app.gui import workers

        _install_llm(tmp_path, "2B", "Q4_K_M")
        AppConfig(
            models_dir=str(tmp_path), llm_model_key="llm:2B:Q4_K_M"
        ).save(patched_config)
        assert workers.configured_ollama_model() == "qwen3.5-2b-judge"

    def test_pinned_model_reaches_the_judge_selector(self, tmp_path, patched_config, monkeypatch):
        """「使用中」不能只停在界面：select_judge 必须收到同一个 Ollama 名字。"""
        from app.core import semantic
        from app.core.app_config import AppConfig
        from app.gui import workers

        _install_llm(tmp_path, "2B", "Q4_K_M")
        AppConfig(
            models_dir=str(tmp_path), llm_model_key="llm:2B:Q4_K_M"
        ).save(patched_config)

        seen = {}

        def fake_local_judge(*, ollama_model=None, model_dir=None):
            seen["model"] = ollama_model
            return None

        # select_judge 在本地分支上调 local_judge（进程内 llama.cpp → Ollama）
        monkeypatch.setattr(semantic, "local_judge", fake_local_judge)
        semantic.select_judge(
            AppConfig.load(), ollama_model=workers.configured_ollama_model()
        )
        assert seen["model"] == "qwen3.5-2b-judge"

    def test_missing_pinned_model_falls_back_to_auto(self, tmp_path, patched_config):
        """指定的档位被删掉后不能卡死：退回自动规则（日志会如实报出实际用的是谁）。"""
        from app.core.app_config import AppConfig
        from app.gui import workers

        AppConfig(
            models_dir=str(tmp_path), llm_model_key="llm:2B:Q8_0"
        ).save(patched_config)
        assert workers.configured_ollama_model() is None, "未安装时应退回自动选择"


# ---------------------------------------------------------------------------
# 厂商管理：用户能自己增删改厂商，且连带处理不留"静默失效"
# ---------------------------------------------------------------------------


class TestProviderDialog:
    """厂商管理对话框：内置不可删、id 不可改，自定义可增删，删了要回落本地。"""

    @pytest.fixture
    def provider_form(self, window, qapp, tmp_path):
        from app.core.app_config import AppConfig
        from app.core.providers import DEFAULT_PROFILES

        config_file = tmp_path / "config.json"
        AppConfig().save(config_file)
        dialog = window.build_provider_dialog(config_path=config_file)
        dialog.show()
        qapp.processEvents()
        form = dialog.form
        yield form, config_file
        dialog.close()
        assert DEFAULT_PROFILES, "预置目录不该为空"

    @staticmethod
    def _pick(form, profile_id: str) -> None:
        assert form.select_profile(profile_id), f"列表里没有 {profile_id}"

    def test_lists_every_preset_and_marks_builtins(self, provider_form):
        from app.core.providers import DEFAULT_PROFILES

        form, _ = provider_form
        assert form.profile_list.count() == len(DEFAULT_PROFILES)
        labels = [form.profile_list.item(i).text() for i in range(form.profile_list.count())]
        assert all("（内置）" in text for text in labels)

    def test_builtin_id_is_locked_and_undeletable(self, provider_form):
        form, _ = provider_form
        self._pick(form, "volc")
        assert form.edit_id.isEnabled() is False, "内置厂商的 id 不可改"
        assert form.btn_delete.isEnabled() is False, "内置厂商的删除按钮必须置灰"
        assert form.btn_restore.isEnabled() is True, "内置厂商才能恢复默认"
        assert form.edit_base_url.text().startswith("https://ark."), "字段要回填出内置默认"

    def test_protocol_switching_shows_relevant_fields(self, provider_form):
        from app.core.providers import PROTO_DISABLED, PROTO_OPENAI, PROTO_VOLC_ARK

        form, _ = provider_form
        self._pick(form, "volc")
        assert not form.asr_endpoint_field.isHidden(), "火山转写要看 asr_endpoint"
        assert not form.asr_resource_field.isHidden()

        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_OPENAI)
        )
        assert form.asr_endpoint_field.isHidden(), "OpenAI 兼容转写没有 asr_endpoint"
        assert form.asr_resource_field.isHidden()
        assert not form.base_field.isHidden()

        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_DISABLED)
        )
        form.edit_judge_protocol.setCurrentIndex(
            form.edit_judge_protocol.findData(PROTO_DISABLED)
        )
        assert form.base_field.isHidden(), "两条能力都停用时，接口地址没有意义"
        assert form.asr_model_field.isHidden()

    def test_egress_preview_follows_protocol(self, provider_form):
        """外发预览必须跟着协议变——它抵掉了本页删掉的两个输入框。"""
        from app.core.providers import PROTO_OPENAI

        form, _ = provider_form
        self._pick(form, "volc")
        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_OPENAI)
        )
        preview = form.egress_preview.text()
        assert "openspeech" not in preview, "换成 OpenAI 兼容后不该再报火山主机"
        assert "ark.cn-beijing.volces.com" in preview

    def test_new_profile_is_validated_before_save(self, provider_form):
        from app.core.app_config import AppConfig

        form, config_file = provider_form
        form._on_new()
        form.edit_id.setText("Bad-ID")            # 大写不合法
        form.edit_label.setText("测试厂商")
        form._on_save()
        assert "保存失败" in form.hint_label.text()
        assert not AppConfig.load(config_file).providers, "校验不过就不该落盘"

    def test_save_custom_profile_roundtrips(self, provider_form):
        from app.core.app_config import AppConfig
        from app.core.providers import PROTO_OPENAI
        from app.gui.widgets import message

        form, config_file = provider_form
        form._on_new()
        form.edit_id.setText("my-gateway")
        form.edit_label.setText("自建网关")
        form.edit_base_url.setText("https://gateway.example.com/v1")
        form.edit_judge_protocol.setCurrentIndex(
            form.edit_judge_protocol.findData(PROTO_OPENAI)
        )
        form.edit_judge_model.setText("my-model")
        form._on_save()
        assert "已保存" in form.hint_label.text(), form.hint_label.text()

        profile = AppConfig.load(config_file).profiles().get("my-gateway")
        assert profile is not None
        assert profile.label == "自建网关"
        assert profile.judge_protocol == PROTO_OPENAI
        assert profile.supports("judge"), "协议 + 模型名齐了就该算可用"
        assert message is not None  # 用例里不弹窗，仅确认模块可导入

    def test_duplicate_prepares_new_id(self, provider_form):
        form, _ = provider_form
        self._pick(form, "deepseek")
        form._on_duplicate()
        assert form.edit_id.text() == "", "复制出的档案必须让用户自己填新 id"
        assert form.edit_id.isEnabled() is True
        assert "副本" in form.edit_label.text()
        assert "重复" in form.hint_label.text()

    def test_delete_reverts_referencing_capability(self, provider_form, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND, AppConfig
        from app.core.providers import CAP_JUDGE
        from app.gui.widgets import message

        form, config_file = provider_form
        config = AppConfig.load(config_file)
        config.providers = [
            {"id": "my-gateway", "label": "自建网关", "base_url": "https://g.example.com/v1",
             "judge_model": "m", "judge_protocol": "openai", "enabled": True,
             "allow_upload": True}
        ]
        config.backend_prefs = {CAP_JUDGE: "my-gateway"}
        config.save(config_file)
        form.reload(select="my-gateway")

        monkeypatch.setattr(message, "confirm", lambda *a, **k: True)
        form._on_delete()

        saved = AppConfig.load(config_file)
        assert saved.profiles().get("my-gateway") is None, "自定义档案应被删掉"
        assert saved.backend_for(CAP_JUDGE) == LOCAL_BACKEND, (
            "引用它的能力必须回落本地，否则用户只会看到云端静默失效"
        )

    def test_restore_builtin_defaults(self, provider_form, monkeypatch):
        from app.core.app_config import AppConfig
        from app.core.providers import DEFAULT_PROFILES
        from app.gui.widgets import message

        form, config_file = provider_form
        config = AppConfig.load(config_file)
        config.providers = [{"id": "volc", "judge_model": "ep-mine", "api_key": "sk-mine"}]
        config.save(config_file)
        form.reload(select="volc")
        assert form.edit_judge_model.text() == "ep-mine"

        monkeypatch.setattr(message, "confirm", lambda *a, **k: True)
        form._on_restore()

        saved = AppConfig.load(config_file)
        profile = saved.profiles().get("volc")
        assert profile.judge_model == DEFAULT_PROFILES[0].judge_model, "回到内置默认"
        assert profile.api_key == "", "恢复默认要清掉已保存的 Key"
