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
