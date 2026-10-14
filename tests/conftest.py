"""pytest 公共夹具与路径设置。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

for path in (ROOT, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TESTDATA = ROOT / "testdata"

REQUIRED_ASSETS = {
    "cfr": "cfr_120s_25fps.mp4",
    "no_audio": "noaudio_120s.mp4",
    "multi_audio": "multiaudio_120s.mp4",
    "nonzero_pts": "nonzero_pts_120s.mp4",
    "vfr": "vfr_120s.mp4",
}


@pytest.fixture(scope="session")
def assets() -> dict[str, Path]:
    """返回合成测试素材路径。

    素材不随仓库分发。缺失时给出明确的生成指令，而不是让测试静默通过。
    """
    missing = [name for name in REQUIRED_ASSETS.values() if not (TESTDATA / name).exists()]
    if missing:
        pytest.skip(
            "缺少合成测试素材，请先运行：\n"
            "  python tools/make_test_assets.py testdata --seconds 120\n"
            f"缺失文件：{', '.join(missing)}"
        )
    return {key: TESTDATA / name for key, name in REQUIRED_ASSETS.items()}


@pytest.fixture(scope="session")
def binaries():
    from app.core.ffmpeg import check_encoders, locate_ffmpeg

    found = locate_ffmpeg()
    check_encoders(found)
    return found


SPEECH_WAV = "speech_16k.wav"
SPEECH_TRUTH = "speech_ground_truth.json"


@pytest.fixture(scope="session")
def speech_asset() -> Path:
    """中文语音测试素材（带转写标准答案）。

    由 `python tools/make_speech_asset.py testdata` 生成，依赖系统中文 SAPI 引擎。
    缺失时跳过而不是失败——它替不了 ASR 正确性验证，只能替代"没有真实素材"。
    """
    path = TESTDATA / SPEECH_WAV
    if not path.exists():
        pytest.skip(
            "缺少语音测试素材，请先运行：\n"
            "  python tools/make_speech_asset.py testdata"
        )
    return path


@pytest.fixture(scope="session")
def speech_truth() -> dict:
    import json

    path = TESTDATA / SPEECH_TRUTH
    if not path.exists():
        pytest.skip("缺少语音标准答案，请先运行 tools/make_speech_asset.py")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def speech_video(binaries) -> Path:
    """把合成语音铺到视频上，得到**有真实对白**的测试素材。

    阶段3/2 的端到端验证需要候选点非空，而纯色视频没有语音也没有镜头切换，
    候选恒为空、DP 主路径永远走不到。这里用合成语音 + 纯色画面构造素材：
    画面虽然没有信息量，但对白是真实的（SAPI 合成），足以驱动
    转写 → 句末候选 → 候选图 → 动态规划 全链路。
    """
    import subprocess

    wav = TESTDATA / SPEECH_WAV
    if not wav.exists():
        pytest.skip("缺少语音素材，请先运行 tools/make_speech_asset.py")
    target = TESTDATA / "speech_video.mp4"
    if target.exists() and target.stat().st_size > 1024:
        return target

    cmd = [
        str(binaries.ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=25",
        "-i", str(wav),
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", "-y", str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"无法合成语音视频：{(proc.stderr or '')[-200:]}")
    return target


@pytest.fixture(scope="session", autouse=True)
def _qt_teardown_at_session_end():
    """会话结束前显式拆掉 Qt，不留 QApplication 给解释器收尾。

    实测：跑完几百个界面用例之后，解释器收尾阶段销毁 QApplication 会**偶发**
    fail-fast（退出码 ``0xC0000409``、故障模块 ``Qt6Core.dll``）。收尾诊断显示
    那一刻已经没有 Python 线程、没有 QThread，只剩一个 QApplication —— 也就是说
    崩点不在我们的后台任务，而在 Qt 自己的拆除顺序上。

    这种"全部用例都通过之后才崩"的失败最难查，所以在解释器还健康时自己拆：
    关掉所有顶层窗口 → 标记删除 → 处理事件 → 退出 → 显式析构 QApplication。
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:      # 没装 Qt 的环境（纯核心测试）
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
        import shiboken6

        shiboken6.delete(app)
    except Exception:  # noqa: BLE001 - 拆不掉也别再抛
        pass


@pytest.fixture(scope="session", autouse=True)
def _no_network_update_check():
    """测试期不联网查更新。

    ``MainWindow`` 构造时会做一次"今天还没查过就静默查一次"的更新检查——那是一次
    **真实网络请求**（curl，超时十几秒）。测试里会反复新建主窗口，慢网络上这些
    线程会活过收尾：``test_ui_smoke`` 的"收尾后不该还有在跑的线程"就是这么被拖红的
    （仓库里的 config.json 没有 update_checked_at，于是每个新窗口都会真的去查）。
    界面测试只关心界面行为，联网那条链路由 updater 的单测覆盖。
    """
    try:
        from app.gui.main_window import MainWindow
    except ImportError:      # 没装 PySide6 的纯核心环境
        yield
        return
    original = MainWindow._startup_update_check
    MainWindow._startup_update_check = lambda self: None
    yield
    MainWindow._startup_update_check = original


def whisper_model_or_skip(tier: str = "small"):
    """定位 whisper 模型；缺失时跳过并给出下载命令。"""
    from app.core.asr import locate_model

    info = locate_model(tier)
    if info is None:
        pytest.skip(
            f"未下载 {tier} 模型（models/ 下）。下载命令：\n"
            f"  python tools/fetch_whisper_model.py {tier}\n"
            "注意本机 huggingface.co 不可达，脚本会自动走 hf-mirror 镜像，"
            "并绕开 huggingface_hub 的符号链接机制（本机账户无该权限，会产出 0 字节文件）。"
        )
    return info
