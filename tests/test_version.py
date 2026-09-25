"""版本号一致性：run.py 与 pyproject.toml 不能各说各话。

历史上这两处漂移过（1.0.0 vs 0.1.0），而漂移不会被任何现有测试发现——
自检打印的是一个数，打包工具读的是另一个数。这里把它固化成断言。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # 只认 [project] 段里的 version，避免命中 [tool.*] 里的同名字段
    section = text.split("[project]", 1)[1]
    match = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
    assert match is not None, "pyproject.toml 的 [project] 段里没有 version"
    return match.group(1)


def test_pyproject_version_matches_run_py():
    import run

    assert run.VERSION == read_pyproject_version(), (
        f"run.py VERSION={run.VERSION!r} 与 "
        f"pyproject.toml version={read_pyproject_version()!r} 不一致，"
        "改版本时两处都要改"
    )


def test_version_is_semver_like():
    import run

    assert re.fullmatch(r"\d+\.\d+\.\d+", run.VERSION), run.VERSION
