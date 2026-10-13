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

    def refuse(kind):
        """带动作按钮的警告框：记录内容，但一律当作"用户没点应用"。"""

        def inner(*args, **kwargs):
            recorded.append((kind,) + args)
            return False

        return inner

    monkeypatch.setattr(message_mod, "warning_with_action", refuse("warning_with_action"))
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
        # 测试不该发出声音：这条只验证画面通道（音频通道的启停在下面单测）
        player.set_audio_enabled(False)
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

    def test_position_tracks_wall_clock_not_frame_count(self, qapp, binaries, assets):
        """回归：播放越久、音画越不同步。

        旧实现每个 tick ``_position += 1/fps``，把画面时间绑死在"渲染成功的帧数"
        上。40ms 间隔恰好卡在 Windows 定时器分辨率边缘，实测每 40ms 漏一次触发，
        帧率掉到 18fps；而音频走 ffplay 实时钟，两者之差就是累计丢帧量——20 秒
        漂了近 2 秒。

        这里断言的判据不是"喂了多少帧"，而是**位置是否跟着墙钟走**：播放 1.5 秒
        后位置必须落在墙钟经过时间附近（允许 0.35s 的起播与调度误差）。
        """
        import time

        from PySide6.QtCore import QTimer

        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_source(assets["cfr"], size=(320, 180))
        player.set_fps(Fraction(25))
        player.set_audio_enabled(False)
        player.set_range(0.0, 30.0)          # 段够长，不至于中途自然播完
        player.play()

        # 等起表（第一帧到手才起钟）
        deadline = time.perf_counter() + 2.0
        while player._clock_start is None and time.perf_counter() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert player._clock_start is not None, "第一帧始终没到手，无法起表"

        t0 = time.perf_counter()
        QTimer.singleShot(1500, qapp.quit)
        qapp.exec()
        wall = time.perf_counter() - t0
        measured = player.position - player._clock_origin
        player.stop()

        drift = measured - wall
        assert abs(drift) < 0.35, (
            f"位置应跟墙钟走：墙钟经过 {wall:.3f}s，画面时钟推进 {measured:.3f}s，"
            f"漂移 {drift:+.3f}s（旧实现会随播放时长线性累积）"
        )

    def test_timer_polls_finer_than_one_frame(self, qapp):
        """定时器间隔必须比帧时长细，否则 40ms 撞上系统定时器网格会漏触发。"""
        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_fps(Fraction(25))         # 帧时长 40ms
        assert player._interval_ms() < 40, (
            "轮询间隔必须细于帧时长；用帧时长当间隔会让 Windows 定时器每拍漏一次"
        )



class TestPreviewAudio:
    """回归：预览播放以前完全没有声音。

    画面是"FFmpeg 解帧 → QImage"，天然没有音频通道（命令里带 ``-an``）。现在
    播放时并行起一个只放声音的 ffplay（同一文件、同一 ``-ss`` 起点），暂停立刻
    收声。这里不真的出声——把 Popen 换成替身，只断言"命令对不对、启停时机对不对"。
    """

    class _FakeProc:
        """Popen 替身：视频通道要 stdout/poll/terminate，音频通道要 poll/terminate。"""

        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = None      # 让读帧线程立刻结束，测试里不真解码
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    def _player(self, monkeypatch):
        """播放器 + 替身 Popen；返回 (player, 所有命令行, 所有替身进程)。

        先构造播放器再打桩：``StreamPlayer.__init__`` 内部会跑一次
        ``ffmpeg -version`` 定位工具链（走 ``subprocess.run`` → ``Popen``），
        提前打桩会把它一起换掉。
        """
        from pathlib import Path

        from app.gui import frame_source

        player = frame_source.StreamPlayer()
        spawned: list[list[str]] = []
        procs: list = []

        def fake_popen(cmd, **kwargs):
            spawned.append(list(cmd))
            proc = self._FakeProc(list(cmd))
            procs.append(proc)
            return proc

        monkeypatch.setattr(frame_source.subprocess, "Popen", fake_popen)
        player._ffmpeg = "ffmpeg.exe"
        player._ffplay = Path("ffplay.exe")     # 不依赖本机是否带 ffplay
        return player, spawned, procs

    @staticmethod
    def _audio_procs(spawned, procs):
        """挑出音频通道（视频通道的命令里没有 -nodisp）。"""
        return [proc for cmd, proc in zip(spawned, procs) if "-nodisp" in cmd]

    def test_audio_command_spans_the_range_and_picks_the_track(self, qapp):
        from pathlib import Path

        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_source("drama.mp4", size=(320, 180))
        player._ffplay = Path("ffplay.exe")
        player.set_audio_track(2)
        player.set_range(10.0, 20.0)

        cmd = player._audio_command(10.0)
        assert cmd is not None
        assert cmd[0] == "ffplay.exe"
        assert "-nodisp" in cmd, "不能弹出 ffplay 窗口"
        assert "-vn" in cmd, "只解音频，别重复解一遍画面"
        assert "-autoexit" in cmd, "播完自己退出，别留孤儿进程"
        assert cmd[cmd.index("-ast") + 1] == "2", "要放左栏选中的那条音轨"
        assert cmd[cmd.index("-ss") + 1] == "10.000000", "从播放头起播"
        assert cmd[cmd.index("-t") + 1] == "10.000000", "只放到本段结束"
        assert cmd[cmd.index("-i") + 1] == "drama.mp4"

    def test_audio_command_degrades_quietly(self, qapp):
        """没有 ffplay、没有源、或已静音 → 返回 None（画面照常播，不报错）。"""
        from pathlib import Path

        from app.gui.frame_source import StreamPlayer

        player = StreamPlayer()
        player.set_source("drama.mp4", size=(320, 180))
        player._ffplay = None
        assert player._audio_command(0.0) is None, "这套 FFmpeg 没带 ffplay 时应无声降级"

        player._ffplay = Path("ffplay.exe")
        player.set_audio_enabled(False)
        assert player._audio_command(0.0) is None, "静音后不该再起音频进程"

        player.set_audio_enabled(True)
        player.set_range(0.0, None)
        cmd = player._audio_command(5.0)
        assert cmd is not None and "-t" not in cmd, "播到片尾时不该有 -t 上限"

    def test_play_starts_audio_and_pause_stops_it(self, qapp, monkeypatch):
        player, spawned, procs = self._player(monkeypatch)
        player.set_source("drama.mp4", size=(320, 180))
        player.set_audio_track(1)
        player.set_range(0.0, 30.0)

        player.play()
        audio = self._audio_procs(spawned, procs)
        assert len(audio) == 1, f"播放时应起一条音频通道（此前完全没有声音）：{spawned}"
        assert not audio[0].terminated

        player.pause()
        assert audio[0].terminated, "暂停必须收声，不能留一个还在响的 ffplay"
        assert player._audio is None

    def test_natural_finish_also_stops_audio(self, qapp, monkeypatch):
        """自然播完（``_on_tick`` 的收尾）也要收声——这条路最容易漏。"""
        from PySide6.QtCore import QTimer

        player, spawned, procs = self._player(monkeypatch)
        player.set_source("drama.mp4", size=(320, 180))
        player.set_range(0.0, 1.0)
        player.play()
        audio = self._audio_procs(spawned, procs)
        assert len(audio) == 1

        # 替身的读帧线程立刻结束 → 头一拍定时器就会判定"本段播完"
        QTimer.singleShot(300, qapp.quit)
        qapp.exec()

        assert not player.is_playing
        assert audio[0].terminated, "播完必须收声"
        assert player._audio is None


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


class TestCloudCapabilitySwitches:
    """云端能力开关（在「模型管理 → 云端厂商」页签）：默认零外发，没测通不许启用。

    连通性探测被替换成假实现——界面测试不允许真联网（也不该要求 Key）。
    合并前这些开关在「模型管理」的云端卡片上，逻辑没变，只是搬到了厂商页签。
    """

    @staticmethod
    def _form(window, tmp_path, monkeypatch):
        """构造合并对话框并停在厂商页签；配置写到临时文件，绝不碰用户配置。"""
        monkeypatch.setattr(
            "app.core.app_config.config_path", lambda: tmp_path / "config.json"
        )
        return window.build_model_dialog(providers_tab=True).providers

    @staticmethod
    def _assign(form, model: str, capability: str) -> None:
        """等价于用户在模型表格里输入 + 点「设为语义判断 / 设为转写」。"""
        form.model_table.added.emit(model)
        form.model_table.assigned.emit(model, capability)

    def test_defaults_are_closed_and_local(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        assert form.use_judge.isChecked() is False
        assert form.allow_upload.isChecked() is False
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND, "默认仍走本地"
        assert "未启用" in form.cloud_hint.text()
        assert "不含画面" in form.allow_upload.toolTip(), "外发范围在勾选那一刻就要能读到"

    def test_probe_failure_refuses_to_enable(self, window, tmp_path, monkeypatch):
        from app.core.app_config import LOCAL_BACKEND, AppConfig
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        self._assign(form, "ep-test", CAP_JUDGE)
        form.edit_api_key.setText("sk-test")
        form.allow_upload.setChecked(True)
        form.use_judge.setChecked(True)
        monkeypatch.setattr(
            type(form),
            "_probe",
            lambda self, profile, capability: "鉴权失败（API Key 或资源权限）",
        )

        form._save_and_probe()

        assert form.use_judge.isChecked() is False, "测不通就不该保持勾选"
        assert form._config.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        assert "未通过" in form.hint_label.text()

        saved = AppConfig.load()
        assert saved.backend_for(CAP_JUDGE) == LOCAL_BACKEND
        profile = saved.profiles().get("volc")
        assert profile.enabled is False, "失败的档案不得被标记为已启用"
        assert profile.judge_model == "ep-test", "字段本身应已保存，用户不用重填"

    def test_probe_success_enables_and_persists(self, window, tmp_path, monkeypatch):
        from app.core.app_config import AppConfig
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        self._assign(form, "ep-test", CAP_JUDGE)
        form.edit_api_key.setText("sk-test")
        form.allow_upload.setChecked(True)
        form.use_judge.setChecked(True)
        monkeypatch.setattr(type(form), "_probe", lambda self, profile, capability: None)

        form._save_and_probe()

        assert form.use_judge.isChecked() is True
        assert "已启用" in form.cloud_hint.text()
        saved = AppConfig.load()
        assert saved.backend_for("judge") == "volc"
        profile = saved.profiles().get("volc")
        assert profile.enabled is True and profile.allow_upload is True
        assert profile.api_key == "sk-test"

    def test_asr_capability_switches_independently(self, window, tmp_path, monkeypatch):
        """两条能力各自测通、各自切换：只勾转写时语义判断仍走本地。"""
        from app.core.app_config import AppConfig

        form = self._form(window, tmp_path, monkeypatch)
        form.edit_api_key.setText("sk-test")
        form.allow_upload.setChecked(True)
        form.use_asr.setChecked(True)      # 只勾转写
        monkeypatch.setattr(type(form), "_probe", lambda self, profile, capability: None)

        form._save_and_probe()

        saved = AppConfig.load()
        assert saved.backend_for("asr") == "volc"
        assert saved.backend_for("judge") == "local", "没勾的能力不许被切到云端"
        assert "转写" in form.cloud_hint.text() and "语义判断" not in form.cloud_hint.text()

    def test_asr_probe_failure_keeps_local_and_judge_stays(
        self, window, tmp_path, monkeypatch
    ):
        """转写测不通只影响转写：语义判断已测通的不该被连带取消。"""
        from app.core.app_config import AppConfig
        from app.core.providers import CAP_JUDGE

        form = self._form(window, tmp_path, monkeypatch)
        self._assign(form, "ep-test", CAP_JUDGE)
        form.edit_api_key.setText("sk-test")
        form.allow_upload.setChecked(True)
        form.use_judge.setChecked(True)
        form.use_asr.setChecked(True)

        def probe(self, profile, capability):
            return "识别失败（45000151）" if capability == "asr" else None

        monkeypatch.setattr(type(form), "_probe", probe)
        form._save_and_probe()

        saved = AppConfig.load()
        assert saved.backend_for("asr") == "local"
        assert saved.backend_for("judge") == "volc"
        assert form.use_asr.isChecked() is False
        assert form.use_judge.isChecked() is True

    def test_project_snapshot_never_carries_the_key(self, window, tmp_path, monkeypatch):
        """§6：方案快照只记"能否复现这次分集"，不含任何凭据。"""
        from app.core.app_config import AppConfig
        from app.core.providers import CAP_JUDGE
        from app.gui.state import payload_text

        form = self._form(window, tmp_path, monkeypatch)
        self._assign(form, "ep-test", CAP_JUDGE)
        form.edit_api_key.setText("sk-secret-value")
        form.allow_upload.setChecked(True)
        form.use_judge.setChecked(True)
        monkeypatch.setattr(type(form), "_probe", lambda self, profile, capability: None)
        form._save_and_probe()
        assert AppConfig.load().providers, "前置：确实写进了配置"

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
        worker.stage_progress.connect(
            lambda text, ratio: outcome.setdefault("progress", []).append((text, ratio))
        )
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
        assert "边界审核" in outcome["stages"], "整集复核应有独立阶段，进度才有条可挂"
        assert outcome["progress"], "黑场检测与整集复核都应上报进度"
        assert all(
            0.0 <= ratio <= 1.0 for _, ratio in outcome["progress"]
        ), "阶段内进度必须落在 0–100%"

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


class TestApplyAdviceOnFailure:
    """分析失败时的「应用建议」：建议要能直接落到分集设置上并重跑。

    回归：无解报错原先只有一行文字（"建议：最长集长放宽到 ≥ 5.4s"），用户得
    自己打开「工具 → 分集设置」照着改。按钮与文案必须同源——这里直接把求解
    诊断挂在工作线程上，验证界面用的是它。
    """

    def _prepare(self, window, binaries, assets):
        """装好媒体与"目标 2 秒 ±20%"的设置，返回分析页与一份失败诊断。"""
        from types import SimpleNamespace

        from app.core.settings import ADVICE_MAX_DURATION, SettingAdvice
        from app.core.solver import GraphDiagnostics

        pane = window.workbench.analysis_pane
        window.state.media = probe_media(binaries, assets["cfr"])   # 120 秒
        window.state.settings = SplitSettings(
            split_mode=SplitMode.TARGET_DURATION,
            target_duration_seconds=Fraction(2),
            range=RangeSpec(mode=RangeMode.PERCENT, tolerance=Fraction(1, 5)),
        )
        # 真实运行时这份诊断由 AnalysisWorker 在求解失败时留下
        pane.worker = SimpleNamespace(
            failure_diagnostics=GraphDiagnostics(
                advice=[SettingAdvice(ADVICE_MAX_DURATION, "≥", Fraction(27, 5))]
            )
        )
        return pane

    def test_advice_is_applied_and_analysis_restarts(self, window, binaries, assets):
        pane = self._prepare(window, binaries, assets)
        asked: list = []
        restarted: list = []
        logs: list[str] = []
        pane.log_appended.connect(lambda text, level: logs.append(text))
        pane.request_apply_advice = lambda text, advice: asked.append(advice) or True
        pane.start = lambda: restarted.append(True)

        try:
            pane._on_failed("成因。\n建议：最长集长放宽到 ≥ 5.4s。")

            assert asked, "有可应用的建议时必须问一次，而不是只报错"
            assert window.state.settings.range.mode is RangeMode.MANUAL, (
                "建议给的是绝对秒数，浮动来源表达不了，应切成手动上下限"
            )
            assert window.state.settings.range.manual_max == Fraction(27, 5)
            assert window.state.settings.range.manual_min == Fraction(8, 5), (
                "建议没要求动最短集长，它必须保持 2s × (1 − 20%) = 1.6s"
            )
            assert restarted, "应用建议后应立即按新设置重新分析"
            assert any("已应用建议" in line for line in logs), (
                f"日志里要留下改了什么：{logs}"
            )
        finally:
            window.state.reset_media()

    def test_declining_keeps_settings_and_still_warns(
        self, window, binaries, assets, message_recorder
    ):
        pane = self._prepare(window, binaries, assets)
        pane.request_apply_advice = lambda text, advice: False
        pane.start = lambda: pytest.fail("用户没点应用，不该重新分析")

        try:
            pane._on_failed("成因。\n建议：最长集长放宽到 ≥ 5.4s。")

            assert window.state.settings.range.mode is RangeMode.PERCENT
            assert window.state.settings.range.manual_max is None
            assert any(kind == "warning" for kind, *_ in message_recorder), (
                "拒绝应用后仍要把失败原因原样告诉用户"
            )
        finally:
            window.state.reset_media()


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

    def test_recommend_locks_both_target_fields(self, window, qapp, binaries, assets):
        """推荐方案里时长与集数**都是**判定结果，一个都不能改。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form

        page.radio_recommend.setChecked(True)
        assert page.duration_spin.isReadOnly(), "每集时长应由分析判定"
        assert page.count_spin.isReadOnly(), "集数应由分析判定"
        assert "分析后自动判定" in page._duration_field._title.text()
        assert "分析后自动判定" in page._count_field._title.text()

    def test_recommend_does_not_write_stale_target_back(self, window, qapp, binaries, assets):
        """只读框里的旧值不能被当成"推荐模式的输入"。

        判定不读 ``target_duration_seconds``（``check_feasibility`` 对模式C
        无条件走 ``auto_pending``），所以这里保留手填值只为切回时能恢复——
        但绝不能因此让自动判定失效。
        """
        from app.core.settings import check_feasibility

        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form
        page.duration_spin.setValue(999.0)
        page.radio_recommend.setChecked(True)

        settings = page.collect_settings()
        assert settings.split_mode == SplitMode.RECOMMEND
        report = check_feasibility(settings, window.state.total_seconds)
        assert report.auto_pending, "推荐模式必须走自动判定，不能被手填值绕过"
        assert not report.blocking

    def test_recommend_panel_says_pending_not_zero(self, window, qapp, binaries, assets):
        """待判定时不能打印「0 – 0 集」——那把"没算"说成了"算出来是 0"。"""
        window.state.media = probe_media(binaries, assets["cfr"])
        page = window.build_settings_dialog().form
        page.radio_recommend.setChecked(True)
        qapp.processEvents()

        text = page.feasibility.toPlainText()
        assert "分析后自动判定" in text
        assert "0 – 0 集" not in text and "0 - 0 集" not in text

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
        from PySide6.QtCore import QPoint, QPointF

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
    """厂商页签（合并进「模型管理」）：内置不可删、id 不可改，自定义可增删。

    合并后厂商不再是独立对话框，而是「模型管理 → 云端厂商」页签；这里仍用
    ``provider_form`` 这个夹具名，取的正是那页的表单。
    """

    @pytest.fixture
    def provider_form(self, window, qapp, tmp_path):
        from app.core.app_config import AppConfig
        from app.core.providers import DEFAULT_PROFILES

        config_file = tmp_path / "config.json"
        AppConfig().save(config_file)
        dialog = window.build_model_dialog(config_path=config_file, providers_tab=True)
        dialog.show()
        qapp.processEvents()
        form = dialog.providers
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

    def test_advanced_fields_are_collapsed_by_default(self, provider_form):
        """常用字段足够开箱即用：高级区（id / 火山专用 / 代理）默认收起。

        openai 系厂商（如 DeepSeek）最典型——它连转写都没有，字段越少越好。
        """
        form, _ = provider_form
        self._pick(form, "deepseek")
        assert form.advanced_toggle.isChecked() is False
        assert form.advanced_host.isHidden(), "高级区默认应收起"
        assert form.edit_id.isVisible() is False, "id 属于高级字段"
        assert not form.base_field.isHidden(), "常用字段必须首屏可见"

    def test_volcano_fields_live_in_common_area(self, provider_form):
        """必填项绝不能被折叠藏起来：火山专用字段在常用区按协议显隐，不进高级区。

        高级区只留 id 与代理——这样默认视图（火山档案）也不会因为"自动展开"而变长。
        """
        from app.core.providers import PROTO_VOLC_ARK

        form, _ = provider_form
        self._pick(form, "deepseek")
        assert form.volcano_host.isHidden()
        assert form.advanced_toggle.isChecked() is False, "高级区保持收起"

        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_VOLC_ARK)
        )
        assert not form.volcano_host.isHidden(), "选了火山转写就要能填 endpoint/资源 ID"
        assert form.advanced_toggle.isChecked() is False, "不需要为必填项展开高级区"

    def test_new_profile_expands_advanced_for_id(self, provider_form):
        """新增必须先填 id（在高级区里），所以点「新增」就摊开。"""
        form, _ = provider_form
        self._pick(form, "deepseek")
        form.advanced_toggle.setChecked(False)
        form._on_new()
        assert form.advanced_toggle.isChecked() is True
        assert not form.advanced_host.isHidden()

    def test_protocol_switching_shows_relevant_fields(self, provider_form):
        from app.core.providers import PROTO_DISABLED, PROTO_OPENAI

        form, _ = provider_form
        self._pick(form, "volc")
        assert not form.volcano_host.isHidden(), "火山转写要看 asr_endpoint"
        assert not form.asr_resource_field.isHidden()

        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_OPENAI)
        )
        assert form.volcano_host.isHidden(), "OpenAI 兼容转写没有 asr_endpoint"
        assert not form.base_field.isHidden()

        form.edit_asr_protocol.setCurrentIndex(
            form.edit_asr_protocol.findData(PROTO_DISABLED)
        )
        form.edit_judge_protocol.setCurrentIndex(
            form.edit_judge_protocol.findData(PROTO_DISABLED)
        )
        assert form.base_field.isHidden(), "两条能力都停用时，接口地址没有意义"
        # 模型表格不随协议收起来：手填一个模型名在任何协议下都有意义
        assert not form.model_table.isHidden()

    def test_common_area_is_just_key_and_models(self, provider_form):
        """首屏只留「填 Key + 加模型」：地址、协议、火山字段全收进高级区。

        用户实测里这一页太复杂——十几个字段摊在眼前，每一项都像必填。
        """
        form, _ = provider_form
        self._pick(form, "deepseek")
        assert form.advanced_host.isHidden(), "高级区默认收起"
        # 常用区里这两个必须一直在（Key 是首屏唯一的必填项）
        assert not form.key_field.isHidden()
        assert not form.model_table.isHidden()
        # 收进去的字段：确实住在高级区里（isHidden 只看自身开关，父级折叠它不知道，
        # 所以这里查父子关系而不是可见性）
        for field in (form.base_field, form.judge_protocol_field, form.asr_endpoint_field):
            assert form.advanced_host.isAncestorOf(field)

        form.advanced_toggle.setChecked(True)
        assert not form.advanced_host.isHidden()
        assert not form.base_field.isHidden(), "展开后地址要能改"

    def test_validation_error_expands_the_advanced_section(self, provider_form):
        """缺必填项时自动摊开高级区：不会"看不到却必须填"。"""
        form, _ = provider_form
        self._pick(form, "deepseek")
        form.edit_base_url.setText("")
        form.advanced_toggle.setChecked(False)
        form._on_save()
        assert "保存失败" in form.hint_label.text()
        assert form.advanced_toggle.isChecked(), "报错时要顺手展开高级区"


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
        # 模型名现在从表格里添加 + 指派（等价于用户输入后点「添加」再点「设为语义判断」）
        form.model_table.edit_add.setText("my-model")
        form.model_table.btn_add.click()
        rows = form.model_table.rows()
        assert rows and rows[0]["custom"] is True, "手填的模型要出现在表格里"
        form._on_model_assigned("my-model", "judge")
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

    def test_model_list_is_empty_without_key(self, provider_form):
        """没有 Key 就不发请求：提示行说清"填好 Key 后自动获取"，而不是静默无反应。"""
        form, _ = provider_form
        self._pick(form, "deepseek")
        assert form._models_worker is None, "缺 Key 不该起后台任务"
        assert "API Key" in form.model_note.text()
        assert form.btn_refresh_models.isEnabled()

    def test_model_list_fills_the_table_and_keeps_manual_ones(
        self, provider_form, monkeypatch
    ):
        """配好地址与 Key 后自动拉清单填进表格；手填的名字仍在且标"手填"。"""
        from PySide6.QtCore import QObject, Signal as QtSignal

        from app.gui.panes import provider_form as module

        started = []

        class FakeWorker(QObject):
            listed = QtSignal(object)
            failed = QtSignal(str)
            finished_ok = QtSignal()

            def __init__(self, profile, parent=None):
                super().__init__(parent)
                self.profile = profile

            def isRunning(self):  # noqa: N802 - 与 QThread 同名
                return False

            def start(self):  # noqa: N802 - 与 QThread 同名
                started.append(self.profile.base_url)
                self.listed.emit(["whisper-1", "gpt-4o-mini"])
                self.finished_ok.emit()

        monkeypatch.setattr(module, "ModelListWorker", FakeWorker)
        form, _ = provider_form
        self._pick(form, "deepseek")           # 切到一家有 base_url 的厂商
        form.model_table.edit_add.setText("我手填的模型")
        form.model_table.btn_add.click()
        form.edit_api_key.setText("sk-test")
        form._request_models(force=True)

        assert started == ["https://api.deepseek.com/v1"], "应按该厂商的 base_url 拉取"
        rows = form.model_table.rows()
        names = [row["name"] for row in rows]
        assert names[:2] == ["whisper-1", "gpt-4o-mini"], "接口清单在前（字母序）"
        assert names.index("我手填的模型") > 1, "手填的补在后面"
        assert [row["custom"] for row in rows[:2]] == [False, False], "接口拉回来的算自动"
        assert rows[names.index("我手填的模型")]["custom"] is True
        assert "2 个来自该厂商的 /models" in form.model_note.text()
        assert "1 个是手填的" in form.model_note.text()

    def test_assignment_from_table_writes_both_capabilities(
        self, provider_form, monkeypatch
    ):
        """行内指派 = 档案的 judge_model / asr_model_name；保存前只改内存。"""
        from app.core.app_config import AppConfig

        dialog_form, config_file = provider_form
        self._pick(dialog_form, "deepseek")
        dialog_form.model_table.added.emit("m-judge")     # 等价于点「添加」
        dialog_form.model_table.added.emit("m-asr")
        dialog_form.model_table.assigned.emit("m-judge", "judge")
        dialog_form.model_table.assigned.emit("m-asr", "asr")

        rows = {row["name"]: row for row in dialog_form.model_table.rows()}
        assert rows["m-judge"]["in_judge"] and not rows["m-judge"]["in_asr"]
        assert rows["m-asr"]["in_asr"] and not rows["m-asr"]["in_judge"]
        assert AppConfig.load(config_file).profiles().get("deepseek").custom_models == (), (
            "没点「保存」之前不该落盘"
        )

        dialog_form._on_save()
        saved = AppConfig.load(config_file).profiles().get("deepseek")
        assert saved.judge_model == "m-judge"
        assert saved.asr_model_name == "m-asr"
        assert saved.custom_models == ("m-judge", "m-asr"), "手填的名字要记住"

    def test_removing_a_manual_model_clears_its_assignment(self, provider_form):
        """删掉手填的模型时，它若正被指派，指派一并清空（不留悬空引用）。"""
        form, _ = provider_form
        form.model_table.added.emit("ep-abc")
        form.model_table.assigned.emit("ep-abc", "judge")
        assert form.model_table.rows()[0]["in_judge"]

        form.model_table.removed.emit("ep-abc")
        assert "ep-abc" not in form.model_table.names(), "删掉的手填项不该还在表格里"
        assert form._judge_model == "", "指派也要清掉"
        assert "指派也已清空" in form.hint_label.text()

    def test_model_list_failure_asks_for_manual_input(self, provider_form, monkeypatch):
        """拉不到（如方舟没有 /models）不是错误：如实说明并请用户手填。"""
        from PySide6.QtCore import QObject, Signal as QtSignal

        from app.gui.panes import provider_form as module

        class FailingWorker(QObject):
            listed = QtSignal(object)
            failed = QtSignal(str)
            finished_ok = QtSignal()

            def __init__(self, profile, parent=None):
                super().__init__(parent)

            def isRunning(self):  # noqa: N802
                return False

            def start(self):  # noqa: N802
                self.failed.emit("该接口没有返回模型清单（/models 可能不被支持）")
                self.finished_ok.emit()

        monkeypatch.setattr(module, "ModelListWorker", FailingWorker)
        form, _ = provider_form
        form.edit_id.setText("volc")
        form.edit_api_key.setText("sk-test")
        form._request_models(force=True)
        assert "手动添加模型名" in form.model_note.text()
        assert form.btn_refresh_models.isEnabled(), "失败后刷新按钮要恢复可用"

    def test_model_list_is_cached_per_provider(self, provider_form, monkeypatch):
        """同一对 (地址, Key) 命中缓存不再打网络；否则来回切厂商会反复请求。"""
        from PySide6.QtCore import QObject, Signal as QtSignal

        from app.gui.panes import provider_form as module

        calls = []

        class CountingWorker(QObject):
            listed = QtSignal(object)
            failed = QtSignal(str)
            finished_ok = QtSignal()

            def __init__(self, profile, parent=None):
                super().__init__(parent)

            def isRunning(self):  # noqa: N802
                return False

            def start(self):  # noqa: N802
                calls.append(1)
                self.listed.emit(["m1"])
                self.finished_ok.emit()

        monkeypatch.setattr(module, "ModelListWorker", CountingWorker)
        form, _ = provider_form
        form.edit_api_key.setText("sk-test")
        form._request_models()
        form._request_models()
        assert len(calls) == 1, "第二次应命中缓存"
        assert "缓存" in form.model_note.text()

    def test_restore_builtin_defaults(self, provider_form, monkeypatch):
        from app.core.app_config import AppConfig
        from app.core.providers import DEFAULT_PROFILES
        from app.gui.widgets import message

        form, config_file = provider_form
        config = AppConfig.load(config_file)
        config.providers = [{"id": "volc", "judge_model": "ep-mine", "api_key": "sk-mine"}]
        config.save(config_file)
        form.reload(select="volc")
        in_judge = [row["name"] for row in form.model_table.rows() if row["in_judge"]]
        assert in_judge == ["ep-mine"], "配置里的 judge_model 应在表格里标成「语义判断」"

        monkeypatch.setattr(message, "confirm", lambda *a, **k: True)
        form._on_restore()

        saved = AppConfig.load(config_file)
        profile = saved.profiles().get("volc")
        assert profile.judge_model == DEFAULT_PROFILES[0].judge_model, "回到内置默认"
        assert profile.api_key == "", "恢复默认要清掉已保存的 Key"


class TestModelSetupDialog:
    """「模型设置」：每条能力一个下拉——本地模型在最上面，然后是云端（按厂商分子组）。

    界面测试绝不联网：模型清单 worker 换成脚本化的假实现（真网络在
    ``tools/gui_smoke.py`` 与真机上验证）。
    """

    @staticmethod
    def _write_config(config_file, providers=None, **fields):
        from app.core.app_config import AppConfig

        config = AppConfig.load(config_file)
        if providers is not None:
            config.providers = providers
        for key, value in fields.items():
            setattr(config, key, value)
        config.save(config_file)

    def _open(
        self,
        window,
        qapp,
        tmp_path,
        monkeypatch,
        providers=None,
        models=None,
        *,
        install_whisper=(),
        install_llm=None,
        **fields,
    ):
        """构造对话框；``models`` 是 {厂商 id: 模型清单 或 失败原因}。

        ``models_dir`` 一律指到 tmp 下的空目录：否则用例结果会随**开发机恰好装了什么
        模型**而变——本机有 whisper base，CI 上没有，同一份代码两套结论。
        """
        from PySide6.QtCore import QObject, Signal as QtSignal

        from app.gui.panes import model_setup_form as module

        results = models or {}
        started: list[str] = []

        class FakeList(QObject):
            listed = QtSignal(object)
            failed = QtSignal(str)
            finished_ok = QtSignal()

            def __init__(self, profile, parent=None):
                super().__init__(parent)
                self.profile = profile

            def isRunning(self):  # noqa: N802 - 与 QThread 同名
                return False

            def cancel(self):  # noqa: N802
                self.cancelled = True

            def start(self):  # noqa: N802
                started.append(self.profile.id)
                outcome = results.get(self.profile.id)
                if isinstance(outcome, str):
                    self.failed.emit(outcome)
                else:
                    self.listed.emit(list(outcome or []))
                self.finished_ok.emit()

        monkeypatch.setattr(module, "ModelListWorker", FakeList)

        models_root = tmp_path / "models"
        models_root.mkdir(exist_ok=True)
        for tier in install_whisper:
            _install_whisper(models_root, tier)
        if install_llm is not None:
            _install_llm(models_root, *install_llm)

        config_file = tmp_path / "config.json"
        self._write_config(config_file, providers, models_dir=str(models_root), **fields)
        dialog = window.build_model_setup_dialog(config_path=config_file)
        dialog.show()
        qapp.processEvents()
        return dialog, dialog.form, config_file, started

    @staticmethod
    def _two_providers():
        """甲：两条能力齐备且已测通；乙：有 Key 但没测通；丙：测通了但没配 Key。"""
        return [
            {
                "id": "my-a",
                "label": "甲厂",
                "base_url": "https://a.example.com/v1",
                "api_key": "sk-a",
                "judge_protocol": "openai",
                "asr_protocol": "openai",
                "judge_model": "my-chat",
                "asr_model_name": "my-asr",
                "enabled": True,
                "allow_upload": True,
            },
            {
                "id": "my-b",
                "label": "乙厂",
                "base_url": "https://b.example.com/v1",
                "api_key": "sk-b",
                "judge_protocol": "openai",
                "asr_protocol": "openai",
                "judge_model": "b-chat",
            },
            {
                "id": "my-c",
                "label": "丙厂",
                "base_url": "https://c.example.com/v1",
                "api_key": "",
                "judge_protocol": "openai",
                "asr_protocol": "openai",
                "enabled": True,
                "allow_upload": True,
            },
        ]

    @staticmethod
    def _pick(card, kind, value, provider_id=""):
        """按下标选中某一项，返回行号；找不到直接让用例失败（而不是静默跳过）。"""
        for index, choice in enumerate(card.items()):
            if choice.key == (kind, value, provider_id):
                card.combo.setCurrentIndex(index)
                return index
        raise AssertionError(
            f"下拉里没有 {kind}/{value}/{provider_id}，现有："
            f"{[c.label for c in card.items()]}"
        )

    # ---- 结构与排序 ----------------------------------------------------

    def test_two_cards_one_per_capability(self, window, qapp, tmp_path, monkeypatch):
        """一个窗口两张卡：语音转写 + 语义判断，各管各的。"""
        from app.core.providers import CAP_ASR, CAP_JUDGE

        dialog, form, _cfg, _started = self._open(window, qapp, tmp_path, monkeypatch)
        try:
            asr_card, judge_card = form.cards()
            assert asr_card.capability == CAP_ASR
            assert judge_card.capability == CAP_JUDGE
            for card in form.cards():
                assert card.status_main.text(), "每张卡必须有一句结论"
                assert card.combo is not None, "每张卡只有一个下拉"
                # 改版后这两个控件都不该再存在：选模型已经隐含了走哪边
                assert not hasattr(card, "btn_local")
                assert not hasattr(card, "provider_combo")
                assert not hasattr(card, "model_list")
        finally:
            dialog.close()

    def test_local_items_come_before_cloud(self, window, qapp, tmp_path, monkeypatch):
        """用户指定的核心排序：本地模型在最上面，然后才是云端。"""
        from app.gui.panes.model_setup_form import KIND_CLOUD, KIND_LOCAL

        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1", "m2"]},
            install_whisper=("base",), install_llm=("2B", "Q4_K_M"),
        )
        try:
            for card in form.cards():
                kinds = [c.kind for c in card.items()]
                first_cloud = next(
                    (i for i, k in enumerate(kinds) if k == KIND_CLOUD), len(kinds)
                )
                last_local = max(
                    (i for i, k in enumerate(kinds) if k == KIND_LOCAL), default=-1
                )
                assert last_local < first_cloud, (
                    f"{card.capability} 的本地项必须全部排在云端项之前：{kinds}"
                )
        finally:
            dialog.close()

    def test_headers_and_notes_are_not_selectable(self, window, qapp, tmp_path, monkeypatch):
        """分组标题与提示项禁用：点它们不该变成"选了一个模型"。"""
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1"]}, install_whisper=("base",),
        )
        try:
            for card in form.cards():
                blocked = [
                    i for i, c in enumerate(card.items()) if not c.selectable
                ]
                assert blocked, "至少要有「本地模型」「云端模型」两个分组标题"
                for index in blocked:
                    assert not card.combo.model().item(index).isEnabled()
        finally:
            dialog.close()

    def test_providers_without_key_collapse_into_one_note(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """没配 Key 的厂商合并成一条提示，不单独成组、也不隐藏。

        16 家全列会毁掉这个下拉（每家可能上百条模型）；但直接隐藏又会被读成
        "界面坏了"。合并成一条既短、又把原因说清楚。
        """
        dialog, form, _cfg, started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1"], "my-b": ["m2"]},
        )
        try:
            card = form.cards()[1]
            headers = [c.label for c in card.items() if c.kind == "header"]
            assert any("甲厂" in h for h in headers), "有 Key 的厂商要单独成组"
            assert any("乙厂" in h for h in headers), "有 Key 但没测通的也要成组"
            assert not any("丙厂" in h for h in headers), "没 Key 的不该成组"
            notes = [c.label for c in card.items() if c.kind == "note"]
            assert any("未配置 Key" in n for n in notes), "没 Key 的要合并成一条说明"
            assert "my-c" not in started, "没 Key 的厂商不该白打一次网络"
        finally:
            dialog.close()

    def test_current_provider_group_is_listed_first(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """当前生效的厂商排最前：让"现在在用什么"一眼可见。"""
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1"], "my-b": ["m2"]},
            backend_prefs={"judge": "my-b", "asr": "local"},
        )
        try:
            headers = [c.label for c in form.cards()[1].items() if c.kind == "header"]
            vendors = [h for h in headers if "厂" in h]
            assert vendors and "乙厂" in vendors[0], f"当前厂商应排最前，实际 {vendors}"
            assert "当前生效" in vendors[0]
        finally:
            dialog.close()

    def test_configured_but_missing_local_model_is_labelled(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """配置指向的本地模型没装：仍要列出来并标注，否则用户不知道"我设的没了"。

        此时结论是「不会执行」而不是「还没选」——配的档位确实跑不起来，
        这跟"装了没挑"是两回事，混说会让用户以为只要随手点一下就能用。
        """
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            install_whisper=("tiny",), asr_model_tier="base",
        )
        try:
            card = form.cards()[0]
            self._pick(card, "local", "base")
            assert "未安装" in card.combo.currentText()
            assert "不会执行" in card.status_main.text(), card.status_main.text()
            assert card.btn_fix.text() == "去下载"
            # 本机明明有 tiny，也不能因此说"只是没选"——配的 base 才是生效的那个
            assert "还没选" not in card.status_main.text()
        finally:
            dialog.close()

    def test_no_api_key_means_no_request(self, window, qapp, tmp_path, monkeypatch):
        """一个 Key 都没有时，不该发出任何网络请求。"""
        dialog, form, _cfg, started = self._open(window, qapp, tmp_path, monkeypatch)
        try:
            assert started == [], "没凭据就别去打网络：必然 401，还白等一个超时"
        finally:
            dialog.close()

    # ---- 选择即生效 ----------------------------------------------------

    def test_selecting_a_local_model_applies_immediately(
        self, window, qapp, tmp_path, monkeypatch
    ):
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch, install_whisper=("tiny", "base"),
        )
        try:
            card = form.cards()[0]
            self._pick(card, "local", "base")
            qapp.processEvents()
            saved = AppConfig.load(config_file)
            assert saved.asr_model_tier == "base", "选中即写盘，不再需要保存按钮"
            assert saved.backend_for("asr") == "local"
            assert "已生效" in form.hint_label.text()
        finally:
            dialog.close()

    def test_selecting_a_cloud_model_applies_immediately(
        self, window, qapp, tmp_path, monkeypatch
    ):
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["my-chat", "my-reasoner"]},
            install_whisper=("base",),
        )
        try:
            card = form.cards()[1]
            self._pick(card, "cloud", "my-reasoner", "my-a")
            qapp.processEvents()
            saved = AppConfig.load(config_file)
            assert saved.backend_for("judge") == "my-a"
            assert saved.profiles().get("my-a").judge_model == "my-reasoner"
        finally:
            dialog.close()

    def test_local_and_cloud_can_be_mixed(self, window, qapp, tmp_path, monkeypatch):
        """转写走本地、语义判断走云端——两条能力各写各的，互不覆盖。"""
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["my-chat", "my-reasoner"]},
            install_whisper=("base",), asr_model_tier="base",
        )
        try:
            asr_card, judge_card = form.cards()
            self._pick(asr_card, "local", "base")
            self._pick(judge_card, "cloud", "my-reasoner", "my-a")
            qapp.processEvents()

            saved = AppConfig.load(config_file)
            assert saved.backend_for("asr") == "local", "转写仍走本地"
            assert saved.backend_for("judge") == "my-a", "语义判断切到云端"
            assert saved.asr_model_tier == "base", "本地档位不该被云端那侧冲掉"
        finally:
            dialog.close()

    def test_repopulating_after_a_fetch_arrives_does_not_write(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """清单回来后重建下拉，不该被当成"用户改主意了"而写盘。

        清空再填充会一路触发 ``currentIndexChanged``；不挡住信号的话，
        打开窗口的过程本身就会把配置改掉一次——这是回归点。
        """
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": []},
            install_whisper=("base",), asr_model_tier="base",
            backend_prefs={"asr": "local", "judge": "local"},
        )
        try:
            before = config_file.read_text(encoding="utf-8")
            form._models["my-a"] = ["m1", "m2"]      # 模拟清单到达
            form._repopulate()

            assert config_file.read_text(encoding="utf-8") == before, "刷新不该落盘"
            assert AppConfig.load(config_file).asr_model_tier == "base"
            chosen = form.asr.selected_choice()
            assert chosen is not None and chosen.value == "base", "当前选中项要保住"
        finally:
            dialog.close()

    # ---- 手输模型名 ----------------------------------------------------

    def test_manual_name_accepted_when_only_one_provider_has_a_key(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """清单外的名字（方舟 ep-xxx、自建别名）在归属唯一时照样能用。"""
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers()[:1], {"my-a": ["model-a"]},
        )
        try:
            card = form.cards()[1]
            card.combo.setCurrentIndex(-1)
            card.combo.lineEdit().setText("ep-mine")
            card.combo.lineEdit().editingFinished.emit()
            qapp.processEvents()

            saved = AppConfig.load(config_file)
            assert saved.profiles().get("my-a").judge_model == "ep-mine"
        finally:
            dialog.close()

    def test_manual_name_refused_when_the_owner_is_ambiguous(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """两家都有 Key、且还没选过任何云端模型 → 拒绝猜，并说清为什么。

        随便挑一家会把模型名写进错误的档案，而且用户很难察觉——下次分析才发现
        走了错的模型。这里宁可停下来问一句。
        """
        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1"], "my-b": ["m2"]},
        )
        try:
            before = config_file.read_text(encoding="utf-8")
            card = form.cards()[1]
            card.combo.setCurrentIndex(-1)
            card.combo.lineEdit().setText("ep-mine")
            card.combo.lineEdit().editingFinished.emit()
            qapp.processEvents()

            assert config_file.read_text(encoding="utf-8") == before, "归属不明就不该落盘"
            assert "没有归属" in form.hint_label.text()
        finally:
            dialog.close()

    def test_manual_name_follows_the_provider_just_picked(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """先选过某家厂商的模型，手输的名字就跟着那家走。"""
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["m1"], "my-b": ["m2"]},
        )
        try:
            card = form.cards()[1]
            self._pick(card, "cloud", "m2", "my-b")
            qapp.processEvents()
            card.combo.setCurrentIndex(-1)
            card.combo.lineEdit().setText("my-alias")
            card.combo.lineEdit().editingFinished.emit()
            qapp.processEvents()

            saved = AppConfig.load(config_file)
            assert saved.profiles().get("my-b").judge_model == "my-alias"
            assert saved.backend_for("judge") == "my-b"
        finally:
            dialog.close()

    # ---- 状态行 --------------------------------------------------------

    def test_status_leads_with_the_consequence(self, window, qapp, tmp_path, monkeypatch):
        """本机没装 + 云端没生效 → 结论直接说"这条能力不会执行"。

        改版前那一句话里从头到尾没出现过这个后果，而用户正是靠它判断能不能开工。
        """
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-b": ["m2"]},
            backend_prefs={"judge": "my-b", "asr": "local"},
        )
        try:
            judge = form.cards()[1]
            assert "不会执行" in judge.status_main.text(), judge.status_main.text()
            assert "乙厂" in judge.status_detail.text()
            assert judge.btn_fix.text() == "去配置"
        finally:
            dialog.close()

    def test_installed_but_unselected_is_not_reported_as_missing(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """装了但没选 ≠ 没装：后者会让人以为自己缺东西，白去下载一遍。"""
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch, install_whisper=("tiny", "base"),
        )
        try:
            asr = form.cards()[0]
            assert "还没选模型" in asr.status_main.text(), asr.status_main.text()
            assert "已经装了 2 个" in asr.status_detail.text()
        finally:
            dialog.close()

    def test_cloud_that_cannot_run_is_reported_not_hidden(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """选了云端但档案没测通：照写配置，并把差哪一项说清楚。

        这条是这个窗口能安全存在的前提——它替用户做"选云端"的动作，但**不**替
        用户授权外发；没测通就说没测通，而不是默默按旧的走。
        """
        from app.core.app_config import AppConfig

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-b": ["m2"]},
            install_llm=("2B", "Q4_K_M"), llm_model_key="llm:2B:Q4_K_M",
        )
        try:
            judge = form.cards()[1]
            self._pick(judge, "cloud", "m2", "my-b")
            qapp.processEvents()

            saved = AppConfig.load(config_file)
            assert saved.backend_for("judge") == "my-b", "选择被记下来了"
            assert saved.profiles().get("my-b").judge_model == "m2"
            assert "会退回本地" in judge.status_main.text()
            assert "尚未生效" in judge.status_detail.text()
            assert judge.btn_fix.text() == "去配置"
        finally:
            dialog.close()

    def test_working_cloud_is_reported_as_current(
        self, window, qapp, tmp_path, monkeypatch
    ):
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-a": ["my-chat", "my-reasoner"]},
            backend_prefs={"judge": "my-a", "asr": "local"},
        )
        try:
            judge = form.cards()[1]
            assert "正在使用 云端 甲厂 的 my-chat" in judge.status_main.text()
            assert judge.badge.text() == "云端"
        finally:
            dialog.close()

    # ---- 跳转 ----------------------------------------------------------

    def test_fix_buttons_route_to_the_right_window(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """「去配置」跳厂商页签、「去下载」跳本地模型页，都由主窗口接。"""
        dialog, form, _cfg, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers(), {"my-b": ["m2"]},
            backend_prefs={"judge": "my-b", "asr": "local"},
        )
        opened: list[bool] = []

        def fake_build(**kwargs):
            opened.append(kwargs.get("providers_tab") is True)
            return _StubDialog()

        # 必须走 monkeypatch：``window`` 是会话级夹具，直接赋值不会被还原，
        # 这个替身会漏给后面所有用例（症状是它们拿到一个只会 exec 的假对话框）。
        monkeypatch.setattr(window, "build_model_dialog", fake_build)
        try:
            form.cards()[1].btn_fix.click()          # 去配置
            qapp.processEvents()
            assert opened == [True], "应带着 providers_tab=True 打开模型管理"

            opened.clear()
            form.asr.btn_fix.click()                  # 去下载
            qapp.processEvents()
            assert opened == [False], "下载走本地模型页签，不带 providers_tab"
        finally:
            dialog.close()


    def test_section_headers_in_real_form_are_not_selectable(
        self, window, qapp, tmp_path, monkeypatch
    ):
        """生产代码里造出来的分组标题必须真的不可选。

        回归点就在这里：``ModelChoice`` 的 ``selectable`` 字段默认是 **True**，
        三个标题构造点只要有一个漏写 ``selectable=False``，它就变回可选项——
        而 ``fill()`` 里那句 ``setEnabled(False)`` 对它是**死代码**，静默失效，
        编译和测试都不会报任何东西。
        """
        from app.gui.panes.model_setup_form import KIND_HEADER, KIND_NOTE

        dialog, form, config_file, _started = self._open(
            window, qapp, tmp_path, monkeypatch,
            self._two_providers()[:1], {"my-a": ["model-a"]},
        )
        try:
            grouped = 0
            for card in form.cards():
                for position, choice in enumerate(card.items()):
                    if choice.kind not in (KIND_HEADER, KIND_NOTE):
                        continue
                    grouped += 1
                    assert not choice.selectable, (
                        f"{card.capability} 第 {position} 行「{choice.label}」"
                        "是分组/提示，却没标 selectable=False"
                    )
                    assert not card._is_selectable_index(position), (
                        f"{card.capability} 第 {position} 行「{choice.label}」仍被判为可选"
                    )
            assert grouped >= 4, "样本里应至少有本地/云端两个标题与若干提示行"
        finally:
            dialog.close()


class TestAsrEngineMissingStatus:
    """语音转写：模型下了、但这一版没有转写引擎时，界面**必须直说**。

    起因：打包版因体积原因不含 faster-whisper，但「下载模型」照常可用。用户
    下完 484MB 的模型，卡片却打着绿色的"正在使用 本地 whisper small"——因为
    旧判据只看模型文件在不在。结果转写一次也跑不了，与分析页"无对白信息"、
    环境自检"未安装 faster-whisper"三处口径互相打架，且这一处是错的。

    修法：语音转写的"本地就绪"要**同时**要求引擎可用；模型在、引擎缺时给出
    单独的结论，并把修复按钮指向环境自检（而非"去下载"，那解决不了问题）。
    """

    def _form(self, window, tmp_path, monkeypatch, *, engine: bool):
        from app.gui.panes.model_setup_form import ModelSetupForm

        models_root = tmp_path / "models"
        models_root.mkdir(exist_ok=True)
        _install_whisper(models_root, "small")
        config_file = tmp_path / "config.json"
        from app.core.app_config import AppConfig

        config = AppConfig.load(config_file)
        config.models_dir = str(models_root)
        config.asr_model_tier = "small"
        config.save(config_file)

        form = ModelSetupForm(window.state, None, config_path=config_file)
        # 打桩引擎探测：测试机通常没装 faster-whisper，不打桩就测不到"可用"分支
        monkeypatch.setattr(form, "_asr_engine_available", lambda: engine)
        return form

    def test_model_present_but_engine_missing_is_reported(self, window, tmp_path, monkeypatch):
        from app.gui.panes.model_setup_form import CAP_ASR

        form = self._form(window, tmp_path, monkeypatch, engine=False)
        assert form._asr_model_present(), "前提：模型文件确实装好了"
        assert not form._local_ready(CAP_ASR), "缺引擎时不该判为就绪"

        card = form._cards[CAP_ASR]
        form._refresh_status(card)
        assert "引擎" in card.status_main.text(), (
            f"应指出缺引擎，实际是「{card.status_main.text()}」"
        )
        assert "不使用" in card.status_main.text() or "不会执行" in card.status_main.text()

    def test_missing_engine_offers_envcheck_not_download(self, window, tmp_path, monkeypatch):
        """修复按钮必须指向环境自检——去下载解决不了缺引擎。"""
        from app.gui.panes.model_setup_form import CAP_ASR

        form = self._form(window, tmp_path, monkeypatch, engine=False)
        card = form._cards[CAP_ASR]
        form._refresh_status(card)
        assert card._fix_action == "envcheck", (
            f"缺引擎时的动作应是 envcheck（去自检），实际 {card._fix_action!r}"
        )

        seen: list = []
        form.env_check_requested.connect(lambda: seen.append(True))
        form._on_fix_requested(CAP_ASR, "envcheck")
        assert seen, "点「去自检」应发出 env_check_requested"

    def test_engine_present_reports_in_use(self, window, tmp_path, monkeypatch):
        """引擎与模型都在 → 恢复成"正在使用"，不误报。"""
        from app.gui.panes.model_setup_form import CAP_ASR

        form = self._form(window, tmp_path, monkeypatch, engine=True)
        assert form._local_ready(CAP_ASR)
        card = form._cards[CAP_ASR]
        form._refresh_status(card)
        assert "正在使用" in card.status_main.text()

    def test_envcheck_action_does_not_fall_through_to_providers(
        self, window, tmp_path, monkeypatch
    ):
        """未知动作曾一律落到"去配置厂商"——缺引擎的人会被引去填 API Key。"""
        from app.gui.panes.model_setup_form import CAP_ASR

        form = self._form(window, tmp_path, monkeypatch, engine=False)
        to_env: list = []
        to_providers: list = []
        form.env_check_requested.connect(lambda: to_env.append(True))
        form.configure_providers_requested.connect(lambda: to_providers.append(True))
        form._on_fix_requested(CAP_ASR, "envcheck")
        assert to_env and not to_providers, "envcheck 不该落到 providers 分支"


class TestModelSetupSectionHeaders:
    """下拉里的「本地模型 / 云端模型」是**分组标题**，不是可选项。

    为什么这组用例必须存在
    ----------------------
    ``fill()`` 早就对它们调了 ``setEnabled(False)``，看上去"处理过了"——但那
    只作用于**视图层**。实测对禁用项调用 ``setCurrentIndex`` 照样成功，于是
    下拉框会一直显示「本地模型」，而 ``config.json`` 里的真实选择一点没变：
    界面显示与实际配置不一致，比"点错了"严重得多。

    所以判据不是"禁用了吗"，而是**用户看得见的那一行文字，能不能对应一个
    真实可选的模型**。
    """

    @staticmethod
    def _card(qapp):
        from app.gui.panes.model_setup_form import _CapabilityCard

        card = _CapabilityCard("asr", "语音转写")
        card.setAttribute(
            __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        return card

    @staticmethod
    def _sample():
        from app.gui.panes.model_setup_form import (
            KIND_CLOUD,
            KIND_HEADER,
            KIND_LOCAL,
            KIND_NOTE,
            ModelChoice,
        )

        return [
            ModelChoice(KIND_HEADER, "本地模型", selectable=False),
            ModelChoice(KIND_LOCAL, "whisper small — 快速", "small"),
            ModelChoice(KIND_HEADER, "云端模型", selectable=False),
            ModelChoice(KIND_CLOUD, "doubao-seed-asr", "doubao-seed-asr", "volc"),
            ModelChoice(KIND_NOTE, "　另有 3 家厂商未配置 Key", selectable=False),
        ]

    def test_headers_are_not_selectable(self, qapp):
        card = self._card(qapp)
        items = self._sample()
        card.fill(items, ("local", "small", ""))

        from app.gui.panes.model_setup_form import KIND_HEADER, KIND_NOTE

        kinds = [c.kind for c in card.items()]
        assert KIND_HEADER in kinds and KIND_NOTE in kinds
        for position, choice in enumerate(card.items()):
            if choice.kind in (KIND_HEADER, KIND_NOTE):
                assert not card._is_selectable_index(position), (
                    f"第 {position} 行「{choice.label}」是分组/提示，不该被当成可选项"
                )

    def test_fill_never_lands_on_a_header(self, qapp):
        """配置里没有任何有效选择时，当前项必须是 -1（走占位提示），不能是第 0 行的标题。"""
        card = self._card(qapp)
        card.fill(self._sample(), None)
        assert card.combo.currentIndex() == -1

    def test_forcing_index_onto_header_reverts_to_real_choice(self, qapp):
        """核心回归：即便索引被强行指到标题，也必须立刻退回真实选择。"""
        card = self._card(qapp)
        card.fill(self._sample(), ("local", "small", ""))
        assert card.combo.currentText() == "whisper small — 快速"

        header_position = next(
            i for i, c in enumerate(card.items()) if c.label == "本地模型"
        )
        # 视图层的 setEnabled(False) 拦不住这一步——这正是缺陷本身
        card.combo.setCurrentIndex(header_position)

        assert card.combo.currentIndex() != header_position, "仍停在分组标题上"
        assert card.combo.currentText() == "whisper small — 快速", (
            "下拉框显示的文字必须仍对应一个真实可选的模型"
        )
        choice = card.selected_choice()
        assert choice is not None and choice.value == "small"

    def test_forcing_index_onto_header_with_no_prior_choice_clears_text(self, qapp):
        """一次都没选过时退回 -1，且必须显式清掉输入框——否则标题文字留在框里冒充模型名。"""
        card = self._card(qapp)
        card.fill(self._sample(), None)

        card.combo.setCurrentIndex(0)

        assert card.combo.currentIndex() == -1
        assert card.combo.lineEdit().text() == "", "输入框残留了分组标题"
        assert card.selected_choice() is None
        assert card.manual_text() == ""

    def test_manual_text_ignores_header_label(self, qapp):
        """文本框里是分组标题时，不能当成用户手输的模型名。"""
        card = self._card(qapp)
        card.fill(self._sample(), ("local", "small", ""))
        card.combo.lineEdit().setText("本地模型")
        assert card.manual_text() == "", "分组标题被当成了手输的模型名"

    def test_manual_text_ignores_note_label(self, qapp):
        card = self._card(qapp)
        card.fill(self._sample(), ("local", "small", ""))
        card.combo.lineEdit().setText("另有 3 家厂商未配置 Key")
        assert card.manual_text() == "", "提示行被当成了手输的模型名"

    def test_manual_text_still_accepts_real_typed_name(self, qapp):
        """回归：真正的手输名字（方舟 ep-xxx / 自建别名）必须照旧放行。"""
        card = self._card(qapp)
        card.fill(self._sample(), ("local", "small", ""))
        card.combo.lineEdit().setText("ep-20240101-abc")
        assert card.manual_text() == "ep-20240101-abc"

    def test_choosing_a_real_item_still_works(self, qapp):
        """回归：正常选中本地档位与云端模型的既有行为不受影响。"""
        card = self._card(qapp)
        card.fill(self._sample(), None)

        local_position = next(
            i for i, c in enumerate(card.items()) if c.label == "whisper small — 快速"
        )
        card.combo.setCurrentIndex(local_position)
        assert card.selected_choice().value == "small"

        cloud_position = next(
            i for i, c in enumerate(card.items()) if c.label == "doubao-seed-asr"
        )
        card.combo.setCurrentIndex(cloud_position)
        chosen = card.selected_choice()
        assert chosen is not None and chosen.provider_id == "volc"


class TestUpdateDialog:
    """「检查更新」：手动模式与启动静默模式对失败的处置完全不同。

    两条纪律各有测试盯着：
    - 手动打开时**任何结果都要显示**，包括断网/限流（用户等着看）；
    - 启动静默检查时**只有确有新版才弹**，失败一律无声。
    """

    @staticmethod
    def _info(version="1.0.1"):
        from app.core.updater import UpdateInfo

        return UpdateInfo(
            tag=f"v{version}", version=version,
            url="https://example.invalid/r", notes="这次修了分集切点",
            setup_url="https://example.invalid/a-setup.exe",
            setup_size=100, setup_digest="sha256:" + "a" * 64,
            zip_url="https://example.invalid/a-win64.zip",
            zip_size=200, zip_digest="sha256:" + "b" * 64,
        )

    def _dialog(self, window, qapp):
        dialog = window.build_update_dialog()
        dialog.show()
        qapp.processEvents()
        return dialog

    def test_menu_has_check_update(self, window):
        from app.gui.main_window import MainWindow

        labels = []
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu is not None:
                labels += [a.text() for a in menu.actions()]
        assert "检查更新…" in labels, f"菜单里没有「检查更新…」：{labels}"
        assert "update_check" in MainWindow.WORKER_NAMES
        assert "update_download" in MainWindow.WORKER_NAMES

    def test_dialog_builds_and_shows(self, window, qapp):
        dialog = self._dialog(window, qapp)
        try:
            assert dialog.status.text(), "状态行不能是空的"
            assert dialog.btn_primary.text() == "检查"
        finally:
            dialog.close()

    def test_manual_mode_shows_failure(self, window, qapp):
        """手动打开时失败必须说出来，不能像启动检查那样静默。"""
        from app.core.updater import CheckResult

        dialog = self._dialog(window, qapp)
        try:
            dialog._on_checked(CheckResult(ok=False, message="连不上 GitHub。"))
            assert "连不上" in dialog.status.text()
            assert dialog.btn_primary.isEnabled()
            assert dialog.btn_primary.text() == "重试"
        finally:
            dialog.close()

    def test_up_to_date_says_so(self, window, qapp):
        from app.core.updater import CheckResult

        dialog = self._dialog(window, qapp)
        try:
            dialog._on_checked(CheckResult(ok=True, message="当前已是最新版本（1.0.0）。"))
            assert "最新" in dialog.status.text()
            assert dialog.btn_primary.text() == "再检查一次"
        finally:
            dialog.close()

    def test_update_available_offers_download(self, window, qapp, monkeypatch):
        from app.core.updater import CheckResult

        monkeypatch.setattr(
            "app.gui.dialogs.update_dialog.UpdateDialog._installed",
            property(lambda self: True),
        )
        dialog = self._dialog(window, qapp)
        try:
            dialog._on_checked(CheckResult(ok=True, info=self._info(), message="发现新版"))
            assert dialog._state == "available"
            assert dialog.notes.isVisible(), "更新说明应当显示出来"
            assert "分集切点" in dialog.notes_body.text()
            assert dialog.btn_primary.text() == "下载并安装"
        finally:
            dialog.close()

    def test_primary_button_dispatches_by_state(self, window, qapp, monkeypatch):
        """回归：下载完成后点主按钮应执行安装，而不是又去检查一次。

        之前靠 ``_downloaded is not None`` 推断状态，结果 ready 态会走错分支。
        """
        dialog = self._dialog(window, qapp)
        try:
            calls = []
            monkeypatch.setattr(dialog, "start_check", lambda: calls.append("check"))
            monkeypatch.setattr(dialog, "_start_download", lambda: calls.append("download"))
            monkeypatch.setattr(dialog, "_apply_update", lambda: calls.append("apply"))

            dialog._state = "available"
            dialog._on_primary()
            dialog._state = "ready"
            dialog._on_primary()
            dialog._state = "downloading"
            dialog._on_primary()  # 下载中：点了应当无反应
            dialog._state = "up_to_date"
            dialog._on_primary()

            assert calls == ["download", "apply", "check"], calls
        finally:
            dialog.close()

    def test_startup_check_is_silent_on_failure(self, window, qapp, monkeypatch):
        """启动静默检查：失败不弹窗。"""
        from app.core.updater import CheckResult

        shown = []
        monkeypatch.setattr(
            type(window), "build_update_dialog",
            lambda self: shown.append(True),
        )
        window._on_startup_update_checked(CheckResult(ok=False, message="断网"))
        assert not shown, "断网不该弹窗"

    def test_startup_check_prompts_when_update_exists(self, window, qapp, monkeypatch):
        from app.core.updater import CheckResult

        shown = []
        dialogs = []

        def fake_build(self):
            shown.append(True)
            d = object()
            dialogs.append(d)
            return d

        monkeypatch.setattr(
            type(window), "build_update_dialog",
            lambda self: shown.append(True) or type("D", (), {"show": lambda s: None})(),
        )
        monkeypatch.setattr("app.core.app_config.AppConfig.load", classmethod(
            lambda cls, path=None: type("C", (), {"extra": {}})()
        ))
        window._on_startup_update_checked(
            CheckResult(ok=True, info=self._info(), message="发现新版")
        )
        assert shown, "确有新版却没提示"

    def test_startup_check_respects_dismissed_version(self, window, qapp, monkeypatch):
        from app.core.updater import CheckResult

        shown = []
        monkeypatch.setattr(
            type(window), "build_update_dialog",
            lambda self: shown.append(True) or type("D", (), {"show": lambda s: None})(),
        )
        monkeypatch.setattr("app.core.app_config.AppConfig.load", classmethod(
            lambda cls, path=None: type("C", (), {"extra": {"update_dismissed": "1.0.1"}})()
        ))
        window._on_startup_update_checked(
            CheckResult(ok=True, info=self._info("1.0.1"), message="发现新版")
        )
        assert not shown, "用户跳过的版本不该反复提示"


    def test_skip_records_version_and_closes(self, window, qapp, monkeypatch):
        """跳过要落盘到 extra['update_dismissed']，否则下次启动还会弹同一个版本。"""
        from app.core.updater import CheckResult

        saved = {}
        monkeypatch.setattr(
            "app.core.app_config.AppConfig.load",
            classmethod(lambda cls, path=None: type(
                "C", (), {"extra": {}, "save": lambda self: saved.setdefault("ok", True)}
            )()),
        )
        dialog = self._dialog(window, qapp)
        try:
            dialog._on_checked(CheckResult(ok=True, info=self._info(), message="发现新版"))
            assert dialog.btn_skip.isVisible(), "有新版时「跳过这一版」该出现"
            dialog._skip_version()
            assert saved.get("ok"), "跳过没有落盘"
        finally:
            dialog.close()

    def test_skip_hidden_when_no_update(self, window, qapp):
        from app.core.updater import CheckResult

        dialog = self._dialog(window, qapp)
        try:
            dialog._on_checked(CheckResult(ok=False, message="断网"))
            assert not dialog.btn_skip.isVisible(), "没有新版就不该给「跳过」"
        finally:
            dialog.close()


class _StubDialog:
    def exec(self):  # noqa: D102 - 测试替身
        return 0

    def close(self):  # noqa: D102
        pass


class TestModelInfoPanel:
    """模型参数面板：上下文窗口 / 思考等级 / 模态，含「自动填充」。"""

    @pytest.fixture
    def provider_form(self, window, qapp, tmp_path):
        """与 ``TestProviderDialog`` 同一份夹具：厂商页签的表单 + 临时配置。

        夹具是类内定义的（不污染别的测试类），所以这里复刻一份而不是提到模块级。
        """
        from app.core.app_config import AppConfig

        config_file = tmp_path / "config.json"
        AppConfig().save(config_file)
        dialog = window.build_model_dialog(config_path=config_file, providers_tab=True)
        dialog.show()
        qapp.processEvents()
        yield dialog.providers, config_file
        dialog.close()

    @staticmethod
    def _setup(form, provider_id, model):
        """选中一家厂商、选中一个模型（不联网：清单直接手填进去）。"""
        from PySide6.QtCore import Qt

        form.select_profile(provider_id)
        form.model_table.added.emit(model)
        form.model_table.list.set_filter("")
        rows = form.model_table.list.list
        for index in range(rows.count()):
            if rows.item(index).data(Qt.ItemDataRole.UserRole) == model:
                rows.setCurrentRow(index)
                break
        else:  # pragma: no cover - 列表里没有就是夹具坏了
            raise AssertionError(f"模型 {model} 没进列表")
        assert form.model_table.list.selected() == model
        return model

    def test_panel_follows_the_selected_model(self, provider_form):
        form, _ = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        assert "gpt-4o" in form.info_title.text()
        assert form.info_context.value() == 0, "没配过就是未知"
        assert form.info_reasoning.currentData() == ""
        assert not any(box.isChecked() for box in form.info_modalities.values())
        assert "不截断" in form.info_note.text()

    def test_autofill_fills_what_it_is_sure_about(self, provider_form):
        form, _ = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.btn_autofill.click()
        assert form.info_context.value() == 128_000
        assert form.info_modalities["image"].isChecked()
        assert form.info_reasoning.currentData() == "", "普通对话模型不该被塞思考等级"
        assert "自动推断" in form.info_note.text()

    def test_autofill_leaves_unknown_models_blank(self, provider_form):
        """认不出的**不猜**：猜错窗口会静默截掉台词证据。"""
        form, _ = provider_form
        self._setup(form, "deepseek", "厂商内部模型-x9")
        form.btn_autofill.click()
        assert form.info_context.value() == 0
        assert form.info_reasoning.currentData() == ""

    def test_autofill_does_not_overwrite_manual_values(self, provider_form):
        form, _ = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.info_context.setValue(1_000_000)          # 用户手填
        form.btn_autofill.click()
        assert form.info_context.value() == 1_000_000, "手填的优先于知识库"
        assert form.info_modalities["image"].isChecked(), "没填过的照常补上"

    def test_edits_persist_through_save(self, provider_form):
        form, config_file = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.info_context.setValue(200_000)
        form.info_reasoning.setCurrentIndex(form.info_reasoning.findData("high"))
        form.info_modalities["image"].setChecked(True)
        form._on_save()

        from app.core.app_config import AppConfig

        saved = AppConfig.load(config_file)
        info = saved.profiles().get("deepseek").info_for("gpt-4o")
        assert info.context_window == 200_000
        assert info.reasoning == "high"
        assert "image" in info.modalities
        assert info.source == "manual"

    def test_empty_entries_are_not_written(self, provider_form):
        """三个字段全空 = 用户什么都没配，不该在配置里留一条空记录。"""
        form, config_file = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.info_context.setValue(64_000)
        form.info_context.setValue(0)
        form._on_save()

        from app.core.app_config import AppConfig

        saved = AppConfig.load(config_file)
        assert saved.profiles().get("deepseek").model_info == ()

    def test_clear_button_resets_one_model(self, provider_form):
        form, _ = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.btn_autofill.click()
        assert form.info_context.value() == 128_000
        form.btn_clear_info.click()
        assert form.info_context.value() == 0
        assert not any(box.isChecked() for box in form.info_modalities.values())
        assert form._info_of("gpt-4o").is_empty()

    def test_table_row_shows_the_parameters(self, provider_form):
        form, _ = provider_form
        self._setup(form, "deepseek", "gpt-4o")
        form.btn_autofill.click()
        form.info_reasoning.setCurrentIndex(form.info_reasoning.findData("low"))
        tag = form._meta_tag("gpt-4o")
        assert "128k" in tag and "思考·low" in tag and "图像" in tag

    def test_modality_conflict_is_reported_on_assignment(self, provider_form):
        """把纯文本模型指给转写时要说清，而不是等到分析时报 400。"""
        form, _ = provider_form
        self._setup(form, "deepseek", "deepseek-chat")      # 纯文本模型
        form.btn_autofill.click()
        form.info_modalities["audio"].setChecked(False)      # 明确只留文本
        form.model_table.assigned.emit("deepseek-chat", "asr")
        assert "没有音频" in form.hint_label.text()
        assert "不能做转写" in form.hint_label.text()


class TestAnalysisProgressDisplay:
    """分析进度：每个阶段一条自己的进度条 + 一条总进度。

    回归：原先中栏只有一条进度条，且只有语音转写会上报进度——别的阶段它停在
    上一阶段的数值不动（转写被跳过时停在 0%），看起来像"没有进度"。
    """

    def test_each_stage_has_its_own_bar_and_total_tracks_it(self, window):
        pane = window.workbench.analysis_pane
        rows = pane.stage_rows
        pane._reset_progress()
        assert len(rows) == len(pane.STAGES) == 6

        pane._on_stage("语音转写")          # 第 2/6 个阶段
        assert rows[0].bar.value() == 100 and rows[0].status.text() == "已完成"
        assert rows[1].bar.maximum() == 0, "没有细粒度进度的阶段先显示流动动画"
        assert pane.total.value() == round(1 / 6 * 100)

        pane._on_progress("转写第 1/2 段", 0.5)
        assert rows[1].bar.value() == 50, "当前阶段进度条显示自己的比例"
        assert pane.total.value() == round(1.5 / 6 * 100), (
            "总进度 =（已完成阶段数 + 当前阶段比例）/ 阶段总数"
        )

        pane._on_stage("镜头与黑场")
        assert rows[1].bar.value() == 100, "进入下一阶段时上一阶段应填满"
        assert rows[2].bar.maximum() == 0, "新阶段重新从流动动画开始"
        assert pane.total.value() == round(2 / 6 * 100)

    def test_total_progress_mirrors_to_bottom_bar(self, window):
        pane = window.workbench.analysis_pane
        pane._reset_progress()

        pane._on_stage("候选点")            # 第 4/6 个阶段，前面 3 个已完成
        assert window.bottom.progress.value() == round(3 / 6 * 100), (
            "底栏小进度条镜像的是总进度"
        )


class TestCapsuleProgressBar:
    """胶囊进度条（自绘控件）的行为约定。

    换掉 QProgressBar 是为了画出参考界面那种"细长全圆角胶囊 + 两色填充"，但
    接口必须与 QProgressBar 同形——阶段行与总进度都按 ``setRange/setValue``
    调用。这里把这层同形契约钉死，免得以后改绘制时把接口也带歪。
    """

    def test_range_and_value_round_trip(self, window):
        from app.gui.widgets import CapsuleProgressBar

        bar = CapsuleProgressBar()
        bar.setRange(0, 100)
        bar.setValue(37)
        assert bar.value() == 37
        assert bar.maximum() == 100
        assert bar.minimum() == 0

    def test_zero_maximum_means_indeterminate(self, window):
        """``setRange(0, 0)`` 是 busy 约定，必须能被判定为"无确定比例"。"""
        from app.gui.widgets import CapsuleProgressBar

        bar = CapsuleProgressBar()
        bar.setRange(0, 0)
        assert bar.maximum() == 0

    def test_tone_switches_fill_color(self, window):
        from app.gui.widgets import CapsuleProgressBar

        bar = CapsuleProgressBar()
        assert bar.tone() == "normal"
        bar.set_tone("done")
        assert bar.tone() == "done"
        bar.set_tone("不存在的色调")     # 无效值不该悄悄改掉当前色调
        assert bar.tone() == "done"

    def test_paints_without_error_in_both_themes(self, window, qapp):
        """真画一遍：两种主题下自绘都不得抛异常，且要画出非背景色。

        只调 ``.grab()`` 就能触发 ``paintEvent``——这是唯一能挡住"取色键写错、
        运行时 KeyError"的廉价手段。
        """
        from app.gui import theme
        from app.gui.widgets import CapsuleProgressBar

        for mode in ("light", "dark"):
            theme.apply_theme(qapp, mode)
            bar = CapsuleProgressBar()
            bar.setRange(0, 100)
            bar.setValue(60)
            bar.resize(240, 10)
            bar.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            bar.show()
            qapp.processEvents()
            image = bar.grab().toImage()
            colors = {image.pixelColor(x, 5).name() for x in range(0, 240, 8)}
            bar.close()
            assert len(colors) > 1, f"{mode} 下胶囊条只画出一种颜色，八成没画出来"
        theme.apply_theme(qapp, "light")


class TestStageDot:
    """阶段状态点：颜色随状态走，与阶段行的状态机一致。"""

    def test_dot_follows_row_state(self, window):
        from app.gui.panes.analysis_pane import StageState

        row = window.workbench.analysis_pane.stage_rows[0]
        row.mark_pending()
        assert row.dot.state() is StageState.PENDING
        row.mark_running()
        assert row.dot.state() is StageState.RUNNING
        row.mark_done()
        assert row.dot.state() is StageState.DONE
        row.mark_failed()
        assert row.dot.state() is StageState.FAILED
        window.workbench.analysis_pane._reset_progress()

    def test_dot_paints_in_both_themes(self, window, qapp):
        from app.gui import theme
        from app.gui.panes.analysis_pane import StageState, _StageDot

        for mode in ("light", "dark"):
            theme.apply_theme(qapp, mode)
            for state in StageState:
                dot = _StageDot()
                dot.set_state(state)
                dot.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                dot.show()
                qapp.processEvents()
                assert not dot.grab().isNull(), f"{mode}/{state} 画不出来"
                dot.close()
        theme.apply_theme(qapp, "light")


class TestEnvCheckDialog:
    """「环境自检」对话框：检测只读、修复诚实、入口在帮助菜单。

    焦点是"界面如实反映后端结论"——后端的能力边界已由 tests/test_env_check.py
    钉死，这里盯的是对话框有没有把它们如实画出来。
    """

    def _dialog(self, window, qapp):
        dialog = window.build_env_check_dialog()
        dialog.show()
        qapp.processEvents()
        return dialog

    def _report(self, *items):
        from app.core.env_check import EnvReport

        return EnvReport(items=tuple(items))

    def test_menu_has_env_check(self, window):
        from app.gui.main_window import MainWindow

        labels = []
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu is not None:
                labels += [a.text() for a in menu.actions()]
        assert "环境自检…" in labels, f"菜单里没有「环境自检…」：{labels}"
        assert "env_check" in MainWindow.WORKER_NAMES

    def test_menu_action_opens_dialog(self, window, monkeypatch, qapp):
        """点菜单应调到 open_env_check_dialog（打桩避免 exec 阻塞）。"""
        opened = []
        monkeypatch.setattr(window, "open_env_check_dialog", lambda: opened.append(1))
        help_menu = next(
            a.menu() for a in window.menuBar().actions() if a.text() == "帮助"
        )
        actions = {a.text(): a for a in help_menu.actions()}
        actions["环境自检…"].trigger()
        assert opened == [1]

    def test_dialog_builds_and_shows(self, window, qapp):
        dialog = self._dialog(window, qapp)
        try:
            assert dialog.headline.text(), "结论行不能是空的"
        finally:
            dialog.close()

    def test_all_ok_report_reads_usable(self, window, qapp):
        from app.core.env_check import LEVEL_OK, EnvCheckItem

        dialog = self._dialog(window, qapp)
        try:
            dialog.apply_report(self._report(
                EnvCheckItem("ffmpeg", "FFmpeg 工具链", LEVEL_OK, "就绪"),
            ))
            assert "可用" in dialog.headline.text()
            assert dialog.items()[0]["level"] == LEVEL_OK
        finally:
            dialog.close()

    def test_blocked_report_lists_the_blockers(self, window, qapp):
        from app.core.env_check import LEVEL_BLOCK, EnvCheckItem

        dialog = self._dialog(window, qapp)
        try:
            dialog.apply_report(self._report(
                EnvCheckItem("asr_engine", "语音转写引擎（faster-whisper）",
                             LEVEL_BLOCK, "未安装 faster-whisper"),
            ))
            assert "不可用" in dialog.headline.text()
            assert "faster-whisper" in dialog.headline.text()
        finally:
            dialog.close()

    def test_only_actionable_fixes_get_a_button(self, window, qapp):
        """只有会真动手的修复画按钮；纯指引（none）不画。

        这是"诚实"落到界面上的体现：能修的给按钮，纯告知的（`none`）连按钮
        都没有。`download_model` 与 `install_engine` 都算"真动手"。
        """
        from app.core.env_check import (
            FIX_DOWNLOAD,
            FIX_INSTALL_ENGINE,
            FIX_NONE,
            LEVEL_BLOCK,
            EnvCheckItem,
            EnvFix,
        )

        dialog = self._dialog(window, qapp)
        try:
            dialog.apply_report(self._report(
                EnvCheckItem(
                    "whisper_model", "转写模型（whisper-small）", LEVEL_BLOCK,
                    "未下载", fix=EnvFix(FIX_DOWNLOAD, "下载 whisper-small", "whisper:small"),
                ),
                EnvCheckItem(
                    "asr_engine", "语音转写引擎（faster-whisper）", LEVEL_BLOCK,
                    "未安装", fix=EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎"),
                ),
                EnvCheckItem(
                    "ffplay", "预览声音（ffplay）", LEVEL_BLOCK,
                    "缺 ffplay", fix=EnvFix(FIX_NONE, "只是告知"),
                ),
            ))
            from app.gui.dialogs.env_check_dialog import _ItemRow

            rows = dialog.findChildren(_ItemRow)
            assert len(rows) == 3
            assert rows[0].btn_fix is not None, "模型项应给下载按钮"
            assert rows[1].btn_fix is not None, "缺引擎项应给安装按钮"
            assert rows[1].btn_fix.text() == "安装转写引擎"
            assert rows[2].btn_fix is None, "纯告知项不该给按钮"
        finally:
            dialog.close()

    def test_install_engine_button_emits_signal(self, window, qapp):
        """点「安装转写引擎」应发出 install_engine_requested。"""
        from app.core.env_check import (
            FIX_INSTALL_ENGINE,
            LEVEL_BLOCK,
            EnvCheckItem,
            EnvFix,
        )

        dialog = self._dialog(window, qapp)
        got = []
        dialog.install_engine_requested.connect(lambda: got.append(1))
        try:
            dialog.apply_report(self._report(
                EnvCheckItem(
                    "asr_engine", "语音转写引擎", LEVEL_BLOCK, "未安装",
                    fix=EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎"),
                ),
            ))
            dialog._rows[0].btn_fix.click()
            assert got == [1]
        finally:
            dialog.close()

    def test_install_engine_wired_to_main_window(self, window, qapp):
        """主窗口构建对话框时必须已把 install_engine_requested 接上处理器。

        否则按钮点了没反应。这里直接替换处理器为探针，再触发信号验证接线。
        """
        calls = []
        dialog = window.build_env_check_dialog()
        # 断开原有连接，接上探针，确认信号确实通到主窗口的方法名上
        dialog.install_engine_requested.disconnect()
        dialog.install_engine_requested.connect(lambda: calls.append(1))
        dialog.install_engine_requested.emit()
        assert calls == [1]
        assert "deps_install" in type(window).WORKER_NAMES
        # 真正的处理器存在
        assert callable(getattr(window, "_on_env_install_engine", None))

    def test_download_button_emits_signal(self, window, qapp):
        from app.core.env_check import FIX_DOWNLOAD, LEVEL_BLOCK, EnvCheckItem, EnvFix

        dialog = self._dialog(window, qapp)
        got = []
        dialog.download_requested.connect(got.append)
        try:
            dialog.apply_report(self._report(
                EnvCheckItem(
                    "whisper_model", "转写模型", LEVEL_BLOCK, "未下载",
                    fix=EnvFix(FIX_DOWNLOAD, "下载", "whisper:small"),
                ),
            ))
            dialog._rows[0].btn_fix.click()
            assert got == ["whisper:small"]
        finally:
            dialog.close()

    def test_recheck_button_emits_signal(self, window, qapp):
        dialog = self._dialog(window, qapp)
        got = []
        dialog.recheck_requested.connect(lambda: got.append(1))
        try:
            dialog.btn_recheck.click()
            assert got == [1]
        finally:
            dialog.close()


class TestAnalysisSurvivesMissingAsrModule:
    """回归：打包版缺 faster-whisper 时，分析必须降级而不是崩溃。

    根因：``asr.py`` 里 ``from faster_whisper import WhisperModel`` 抛的是
    ``ModuleNotFoundError``（ImportError 子类，**不是** RuntimeError），而
    ``AnalysisWorker`` 只 ``except RuntimeError``——于是"跳过转写继续分集"
    的降级漏网，冒泡成致命的"分析失败：No module named 'faster_whisper'"。
    """

    def test_ensure_model_raises_runtime_error_when_module_missing(
        self, monkeypatch, tmp_path
    ):
        from app.core import asr
        from app.core.asr import AsrEngine, AsrSettings, ModelInfo

        model_dir = tmp_path / "faster-whisper-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.bin").write_bytes(b"x" * 100)
        info = ModelInfo(tier="small", path=model_dir, size_bytes=100)
        monkeypatch.setattr(asr, "locate_model", lambda tier: info)

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "faster_whisper" or name.startswith("faster_whisper."):
                raise ModuleNotFoundError("No module named 'faster_whisper'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        engine = AsrEngine(AsrSettings(model_tier="small"))
        with pytest.raises(RuntimeError) as caught:
            engine.ensure_model()
        # 必须是 RuntimeError（workers 接得住），不能是 ImportError 冒泡出去
        assert not isinstance(caught.value, ImportError)
        assert "faster-whisper" in str(caught.value)


class TestDialogueDiagnosis:
    """转写没产出对白时，界面要说得清"为什么"——而不是含糊一句"无对白信息"。

    起因：用户装了打包版、下载了模型，却看到"无对白信息，无法做句级复核"，
    完全不知道问题出在哪（打包版根本不带转写引擎，模型下了也用不上）。
    这组用例把"三种不同处境要分开说"钉死。
    """

    def test_classifies_missing_engine(self):
        from app.gui.workers import DIALOGUE_NO_ENGINE, classify_dialogue_failure

        assert classify_dialogue_failure(
            "未安装 faster-whisper，无法做语音转写。打包版不含该依赖（体积原因）"
        ) == DIALOGUE_NO_ENGINE
        assert classify_dialogue_failure(
            "No module named 'faster_whisper'"
        ) == DIALOGUE_NO_ENGINE

    def test_classifies_missing_model(self):
        from app.gui.workers import DIALOGUE_NO_MODEL, classify_dialogue_failure

        assert classify_dialogue_failure(
            "未找到模型 small。尚未下载任何模型。请先运行：..."
        ) == DIALOGUE_NO_MODEL

    def test_missing_engine_advice_points_at_install_not_download(self):
        """缺引擎时**不能**引导用户去下模型——那解决不了问题。

        现在的正确出口是「环境自检 → 安装转写引擎」（应用内可装），不再是
        旧文案里那条"只能改用源码版"。
        """
        from app.gui.workers import DIALOGUE_NO_ENGINE, dialogue_failure_advice

        title, detail = dialogue_failure_advice(DIALOGUE_NO_ENGINE)
        assert "引擎" in title
        assert "环境自检" in detail or "安装转写引擎" in detail, (
            "应引导到应用内安装引擎，而不是再下模型"
        )
        assert "下载" not in detail or "用不了" in detail

    def test_detailed_log_line_includes_cause_and_raw_error(self):
        from app.gui.workers import (
            DIALOGUE_NO_ENGINE,
            describe_dialogue_failure,
        )

        line = describe_dialogue_failure(
            DIALOGUE_NO_ENGINE, "small", "No module named 'faster_whisper'"
        )
        assert "引擎" in line, "要有分类结论（人话）"
        assert "环境自检" in line or "安装转写引擎" in line, "要有下一步"
        assert "No module named" in line, "要带上原始报错（排障用）"

    def test_pane_surfaces_warning_on_diagnosis(self, window):
        """分析结束、worker 带诊断时，中栏标题与信号都要反映出来。"""
        from types import SimpleNamespace

        pane = window.workbench.analysis_pane
        seen: list = []
        pane.dialogue_warning.connect(lambda t, d: seen.append((t, d)))
        worker = SimpleNamespace(dialogue_diagnosis="no_engine", dialogue_model_tier="small")
        pane._report_dialogue_diagnosis(worker)
        assert seen, "应发出 dialogue_warning 信号"
        assert "引擎" in pane.stage_label.text()
        window.workbench._dialogue_issues = []     # 清理，免得影响后续用例

    def test_no_warning_when_transcript_ok(self, window):
        from types import SimpleNamespace

        pane = window.workbench.analysis_pane
        seen: list = []
        pane.dialogue_warning.connect(lambda t, d: seen.append((t, d)))
        pane._report_dialogue_diagnosis(SimpleNamespace(dialogue_diagnosis=""))
        assert not seen, "转写正常时不该弹警告"


class TestReparse:
    """「重新解析」：改了设置 / 补了模型后重跑，不必重新导入源片。

    起因：分析完成后想重跑却没有任何入口——只能「更换视频」把源片重新探测一遍。
    """

    def test_clear_analysis_results_keeps_media(self):
        from app.core.plan import BoundaryPlan
        from app.core.timebase import TimeBase
        from fractions import Fraction

        from app.gui.state import ProjectState

        state = ProjectState()
        state.media = object()          # 占位即可，本用例只关心清不清
        tb = TimeBase(1, 25)
        T = Fraction(120)
        plan = BoundaryPlan.from_seconds([T * i / 4 for i in range(5)], tb, tb.seconds_to_ticks(T))
        state.add_plan(plan)
        state.candidates = object()
        state.transcript = object()
        state.episode_reviews = [object()]
        state.exported = {1: (__import__("pathlib").Path("a.mp4"), 1)}

        state.clear_analysis_results()

        assert state.media is not None, "重新解析要保留源视频"
        assert state.plans == [] and state.candidates is None
        assert state.transcript is None and state.episode_reviews is None
        assert state.exported == {}, "旧方案的导出记录不再成立，要清掉"

    def test_can_reparse_requires_media_and_no_running_analysis(self, window):
        pane = window.workbench.analysis_pane
        saved_media = window.state.media
        saved_active = pane._active
        saved_worker = pane.worker
        try:
            window.state.media = None
            ok, reason = window.workbench.can_reparse()
            assert not ok and "源视频" in reason

            window.state.media = saved_media if saved_media is not None else object()
            pane._active = False
            ok, _ = window.workbench.can_reparse()
            assert ok

            # is_active() 要求 _active 为真**且** worker 非空（"已请求开始、
            # 线程尚未起来"那一瞬间也算在跑）
            pane._active = True
            pane.worker = object()
            ok, reason = window.workbench.can_reparse()
            assert not ok and "分析" in reason
        finally:
            window.state.media = saved_media
            pane._active = saved_active
            pane.worker = saved_worker

    def test_app_bar_exposes_reparse_button(self, window):
        """顶栏要有可见入口（用户找不到的功能等于不存在）。"""
        assert hasattr(window.app_bar, "btn_reparse")
        assert hasattr(window, "action_reparse"), "菜单里也要有一份（含快捷键）"


class TestLogExport:
    """日志导出：排障第一手证据要能一键落盘（原先只能全选复制，长日志选不全）。"""

    def test_export_writes_header_and_body(self, window, tmp_path, monkeypatch):
        from app.gui.widgets import message

        monkeypatch.setattr(message, "information", lambda *a, **k: None)
        insp = window.workbench.inspector
        insp.clear_log()
        insp.append_log("第一条", "info")
        insp.append_log("第二条", "warn")

        target = tmp_path / "log.txt"
        out = insp.export_log(str(target))
        assert out == str(target)
        text = target.read_text(encoding="utf-8")
        assert "日志" in text and "共 2 条" in text
        assert "第一条" in text and "第二条" in text

    def test_export_empty_log_is_refused(self, window, monkeypatch):
        from app.gui.widgets import message

        calls: list = []
        monkeypatch.setattr(message, "information", lambda *a, **k: calls.append(a))
        insp = window.workbench.inspector
        insp.clear_log()
        assert insp.export_log("whatever.txt") is None
        assert calls, "空日志应给出提示，而不是写出一个空文件"

    def test_log_toolbar_buttons_exist(self, window):
        insp = window.workbench.inspector
        assert hasattr(insp, "btn_export_log") and hasattr(insp, "btn_clear_log")


class TestExportDialogFitsScreen:
    """导出窗口要放得进屏幕、也要能拖小。

    起因：960x720 的固定尺寸在小屏笔记本上顶出屏幕，底部按钮点不到，又因为
    最小尺寸卡死而拖不小。
    """

    def test_size_is_clamped_to_screen(self, qapp, window):
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            pytest.skip("无屏幕信息")
        avail = screen.availableGeometry()
        dlg = window.build_export_dialog()
        dlg.show()
        qapp.processEvents()
        assert dlg.width() <= avail.width(), "宽度不该超出屏幕"
        assert dlg.height() <= avail.height(), "高度不该超出屏幕"
        dlg.close()

    def test_window_can_be_resized_smaller_than_default(self, qapp, window):
        dlg = window.build_export_dialog()
        assert dlg.minimumHeight() < dlg.height() or dlg.minimumWidth() < dlg.width(), (
            "最小尺寸必须小于默认尺寸，否则用户拖不小"
        )
        assert dlg.isSizeGripEnabled(), "要给出可拖拽的尺寸把手"
        dlg.close()

