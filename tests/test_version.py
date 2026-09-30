"""版本号一致性：run.py 与 pyproject.toml 不能各说各话。

历史上这两处漂移过（1.0.0 vs 0.1.0），而漂移不会被任何现有测试发现——
自检打印的是一个数，打包工具读的是另一个数。这里把它固化成断言。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# 打包态路径：用户可写的东西必须落在 exe 旁边，不能写进安装目录
#
# PyInstaller 的 onedir 产物把 Python 运行时与全部模块放在 exe 同级的
# `_internal/` 里，也就是"装进 Program Files 时的只读目录"。任何仍按
# `Path(__file__).parent.parent.parent` 推导的路径，在源码运行时完全正常，
# 打包后却指向 `_internal/` ——配置写不进去、缓存写进去就 PermissionError。
# 源码运行测不出来（没有 _internal），所以这里直接模拟打包态。
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """把进程伪装成打包态：sys.frozen 为真，sys.executable 指向假装的 exe。"""
    install = tmp_path / "Program Files" / "drama-splitter"
    install.mkdir(parents=True)
    exe = install / "drama-splitter.exe"
    exe.write_bytes(b"")
    (install / "_internal").mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return install


def test_app_dir_follows_exe_when_frozen(frozen):
    from app.core.app_config import app_dir

    assert app_dir() == frozen


def test_writable_paths_stay_beside_exe_not_in_bundle(frozen):
    """配置、缓存、模型目录三者都不得落到 `_internal/`。

    判据是"落在 exe 旁边的用户可写目录"，而不是"某个字符串出现在代码里"——
    只要有一个漏网，用户装到 Program Files 就会在第一次使用时才报错。
    """
    from app.core.app_config import app_dir, config_path, default_models_root

    candidates = {
        "配置": config_path(),
        "缓存": app_dir() / "cache",
        "模型": default_models_root(),
    }
    for label, path in candidates.items():
        assert path != frozen / "_internal", f"{label}目录写进了打包产物内部：{path}"
        assert "_internal" not in path.parts, f"{label}目录写进了打包产物内部：{path}"


def test_analysis_cache_defaults_beside_exe_not_in_bundle(frozen):
    """分析缓存的默认落点同样不得进 `_internal/`。

    这一条对应 `workers.AnalysisWorker` 里真正被用到的那行——真实调用从不传
    `cache_dir`（界面没有这个入口），所以默认值就是唯一实际路径。
    """
    from app.gui.workers import default_cache_dir

    assert default_cache_dir() == frozen / "cache"
    assert "_internal" not in default_cache_dir().parts


def test_model_search_covers_exe_sibling_when_frozen(frozen):
    """模型搜索必须包含 exe 同级——模型是用户另外下载的大文件，不进包。"""
    from app.core.asr import model_search_roots

    assert frozen / "models" in model_search_roots()
