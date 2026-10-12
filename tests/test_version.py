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
# 安装脚本的版本号：installer.iss 里的四处是手写副本，没有测试就一定会漂
#
# 与 pyproject 那份同理——改了 run.py 却忘了改安装脚本，装出来的程序「关于」
# 里显示的版本会和安装包文件名对不上，更新检查也会把用户困在旧版本。
# ---------------------------------------------------------------------------


def installer_iss_text() -> str:
    return (ROOT / "tools" / "installer.iss").read_text(encoding="utf-8")


def installer_iss_code() -> str:
    """剥掉注释后的安装脚本。

    注释里会**解释**为什么不许用 ``{src}`` / ``InitializeSetup``（那正是修复
    记录），所以断言必须只看代码。Inno 的段内注释是 ``;``，``[Code]`` 段里
    另有 ``//``，两种都剥。
    """
    lines = []
    for line in installer_iss_text().splitlines():
        stripped = line.lstrip()
        if stripped.startswith(";") or stripped.startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_installer_app_version_matches_run_py():
    import run

    match = re.search(r"^AppVersion=(\S+)", installer_iss_text(), re.MULTILINE)
    assert match is not None, "installer.iss 里没有 AppVersion"
    assert match.group(1) == run.VERSION, (
        f"installer.iss AppVersion={match.group(1)!r} 与 run.py VERSION={run.VERSION!r} 不一致"
    )


def test_installer_ver_name_embeds_version():
    import run

    match = re.search(r"^AppVerName=(.+)$", installer_iss_text(), re.MULTILINE)
    assert match is not None, "installer.iss 里没有 AppVerName"
    assert run.VERSION in match.group(1), (
        f"AppVerName={match.group(1)!r} 里没有当前版本号 {run.VERSION}"
    )


def test_installer_version_info_matches_run_py():
    import run

    match = re.search(r"^VersionInfoVersion=(\S+)", installer_iss_text(), re.MULTILINE)
    assert match is not None, "installer.iss 里没有 VersionInfoVersion"
    assert match.group(1) == f"{run.VERSION}.0", (
        f"VersionInfoVersion={match.group(1)!r} 应为 {run.VERSION}.0"
    )


def test_installer_output_filename_matches_run_py():
    import run

    match = re.search(r"^OutputBaseFilename=(\S+)", installer_iss_text(), re.MULTILINE)
    assert match is not None, "installer.iss 里没有 OutputBaseFilename"
    assert match.group(1) == f"drama-splitter-{run.VERSION}-setup", (
        f"OutputBaseFilename={match.group(1)!r} 与版本号 {run.VERSION} 不一致"
    )


# ---------------------------------------------------------------------------
# 安装脚本不得用 {src} 拼仓库内路径：这正是 v1.0.0 装不上的根因
#
# {src} 指「正在运行的安装程序自己所在的目录」，随用户把它放在哪而变。
# 安装包的文件来自 [Files] 段的编译期路径，运行时是自包含的；从 Downloads
# 双击时 {src}\..\dist\... 必然不存在。v1.0.0 因此在 InitializeSetup 里
# 误判「未找到打包产物」并中止，所有真实用户都装不上（而开发者从 dist\
# 里跑时恰好通过，测试没抓到）。
# ---------------------------------------------------------------------------


def test_installer_does_not_probe_repo_paths_via_src():
    text = installer_iss_code()
    assert "{src}" not in text, (
        "installer.iss 里出现了 {src}——它指向用户放安装包的目录，"
        "不能用来找仓库内的 dist\\ 产物。编译期检查交给 tools/build_installer.py。"
    )


def test_installer_has_no_initialize_setup():
    """InitializeSetup 曾在运行期误判产物缺失导致安装中止；不要把它加回来。"""
    text = installer_iss_code()
    assert "InitializeSetup" not in text, (
        "installer.iss 又出现了 InitializeSetup——若要用它做检查，"
        "必须确保判据在用户机器上成立（见本条测试的说明）。"
    )


def test_installer_verifies_main_exe_under_app_dir():
    """安装后应确认主程序落在 {app} 下——这是运行期唯一能验、也该验的事。"""
    assert "{app}\\drama-splitter.exe" in installer_iss_code(), (
        "installer.iss 没有在 {app} 下确认 drama-splitter.exe 是否落地"
    )


# ---------------------------------------------------------------------------
# 卸载时的可选数据清理
#
# 程序运行时在 exe 同级生成 config.json / cache/ / models/，这些不在安装器
# 的文件清单里，不显式处理就会在卸载后留下垃圾（models 动辄数 GB）。
# 交互卸载给一个带三个复选框的窗口；静默卸载只看命令行开关。
# ---------------------------------------------------------------------------


def test_installer_offers_uninstall_cleanup():
    text = installer_iss_code()
    assert "InitializeUninstall" in text, "installer.iss 没有卸载入口 InitializeUninstall"
    assert "CurUninstallStepChanged" in text, "installer.iss 没有 CurUninstallStepChanged"
    assert "CreateCustomForm" in text, "卸载时没有自定义选项窗口"
    for label in ("TNewCheckBox", "DelCache", "DelModels", "DelConfig"):
        assert label in text, f"installer.iss 缺少 {label}"


def test_uninstall_cleanup_targets_the_three_data_items():
    """三样数据各删各的，且都用 {app} 作根——不碰安装目录外的东西。"""
    text = installer_iss_code()
    assert "DataCachePath" in text and "DataModelsPath" in text and "DataConfigPath" in text
    # 三个删除动作都要在 CurUninstallStepChanged 里，且都以 DelXxx 变量为条件
    for guard in ("if DelCache", "if DelModels", "if DelConfig"):
        assert guard in text, f"installer.iss 缺少删除条件 {guard}"
    assert "DelTree" in text and "DeleteFile(DataConfigPath)" in text


def test_silent_uninstall_does_not_delete_data_without_switches():
    """静默卸载默认不删任何数据；清理必须由命令行开关显式声明。

    这是安全底线：无人值守（CI、批量部署）跑一次卸载不该销毁用户数据。
    三条开关名一并固定下来，避免日后被改名或加默认值。
    """
    text = installer_iss_code()
    assert "UninstallSilent()" in text, "卸载入口没有区分静默模式"
    for switch in ("/DELCACHE", "/DELMODELS", "/DELCONFIG"):
        assert switch in text, f"installer.iss 缺少静默清理开关 {switch}"


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
