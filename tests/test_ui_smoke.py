"""界面收尾：装了模型时，后台探测线程不许活过进程。

背景（v1.0.9 实测）：模型页一构造就会起 ``JudgeProbeWorker`` 探测推理栈，
装了模型时它要跑几秒（Ollama 探测 / 读模型文件）。谁没等它就退出，Qt 析构
一个还在跑的 QThread 时会直接 fail-fast——退出码 ``0xC0000409``、故障模块
``Qt6Core.dll``，**只在装了模型的机器上复现**。受害的有一条半：

- ``--ui-smoke``：检查做完直接返回，装机验收拿到假失败（README 正是让人看
  它的返回码）；
- pytest：用例构造过模型页、既不关闭也不销毁，整个进程崩在收尾。

所以这里钉三条：``_ui_smoke`` 的出口统一走收尾、模型页在解释器退出前自己
把探测线程收干净，以及 **所有** ``BaseWorker`` 的全局登记 + 退出统一收尾
（模型页那条只是特例；任何 QThread 活到收尾都会崩）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6，跳过界面收尾测试")

import run  # noqa: E402


class _FakeApp:
    def __init__(self) -> None:
        self.events = 0

    def processEvents(self) -> None:  # noqa: N802 - 模仿 Qt 命名
        self.events += 1


class _FakeWindow:
    def __init__(self) -> None:
        self.cancelled: list[int] = []

    def cancel_all_workers(self, wait_ms: int = 0) -> None:
        self.cancelled.append(wait_ms)


def test_finish_stops_workers_and_keeps_exit_code() -> None:
    window, app = _FakeWindow(), _FakeApp()
    assert run._finish_ui_smoke(window, app, 0) == 0
    assert window.cancelled, "收尾没有停后台线程——装了模型的机器上会 fail-fast"
    assert app.events >= 1, "收尾后要让事件循环跑一轮，把取消信号送出去"


def test_finish_keeps_failure_code() -> None:
    window, app = _FakeWindow(), _FakeApp()
    assert run._finish_ui_smoke(window, app, 1) == 1


def test_finish_survives_broken_window() -> None:
    """收尾本身出错不该改变自检结论（最坏也只是退回原来的行为）。"""

    class _Broken:
        def cancel_all_workers(self, wait_ms: int = 0) -> None:
            raise RuntimeError("boom")

    assert run._finish_ui_smoke(_Broken(), _FakeApp(), 0) == 0


def test_ui_smoke_exits_all_go_through_finish() -> None:
    """源码级断言：``_ui_smoke`` 里不许再有裸 ``return 0/1``（防回退）。"""
    source = (ROOT / "run.py").read_text(encoding="utf-8")
    body = source.split("def _ui_smoke() -> int:", 1)[1].split("def main() -> int:", 1)[0]
    assert "return _finish_ui_smoke(" in body, "_ui_smoke 的出口没走收尾"
    assert "return 0\n" not in body, "_ui_smoke 里还有裸的 return 0"
    assert "return 1\n" not in body, "_ui_smoke 里还有裸的 return 1"


def test_model_form_probes_are_drained_at_exit() -> None:
    """退出兜底必须真的去停探测线程（装了模型的机器全靠它）。"""
    from app.gui.panes import model_form as form_module

    class _FakeForm:
        def __init__(self) -> None:
            self.stopped = 0

        def _stop_probe(self) -> None:
            self.stopped += 1

    form = _FakeForm()
    form_module._LIVE_FORMS.add(form)
    try:
        form_module._stop_probes_at_exit()
        assert form.stopped == 1, "退出时没有收探测线程"
    finally:
        form_module._LIVE_FORMS.discard(form)


def test_model_form_stops_probe_before_destruction() -> None:
    """源码级断言：控件析构前必须等线程收尾（防回退）。"""
    source = (ROOT / "app" / "gui" / "panes" / "model_form.py").read_text(encoding="utf-8")
    assert "self.destroyed.connect" in source, "模型页没有析构前收线程"
    assert "probe.wait(" in source, "收线程时没有等它结束"


def test_running_worker_is_registered_and_drained() -> None:
    """跑着的后台任务必须被登记，并能被退出收尾一次收干净。

    这是"探测线程活过进程"那条的通用版：任何 ``BaseWorker`` 子类在解释器收尾时
    还活着，Qt 都会 fail-fast（``0xC0000409`` @ ``Qt6Core.dll``）——而且崩在
    "测试已经全部跑完"之后，最难查。
    """
    import time

    from app.gui import workers as workers_mod
    from app.gui.workers import BaseWorker

    class _SlowWorker(BaseWorker):
        def work(self) -> None:  # noqa: D102 - 故意睡够，让收尾有机会抓到它
            time.sleep(0.4)

    worker = _SlowWorker()
    worker.start()
    try:
        time.sleep(0.1)
        assert any(w is worker for w in list(workers_mod._LIVE_WORKERS)), (
            "运行中的任务没被登记——退出时就没法收它"
        )
        assert workers_mod.stop_all_workers(wait_ms=3000) == 0, "收尾后不该还有在跑的线程"
        assert not worker.isRunning(), "收尾必须等线程真正结束"
    finally:
        worker.wait(3000)


def test_exit_drain_is_wired() -> None:
    """源码级断言：全局登记与 atexit 兜底都在（防回退）。"""
    source = (ROOT / "app" / "gui" / "workers.py").read_text(encoding="utf-8")
    assert "_LIVE_WORKERS.add(self)" in source, "运行中的任务没有登记"
    assert "atexit.register(_drain_workers_at_exit)" in source, "退出兜底没挂上"
    assert "def stop_all_workers(" in source
