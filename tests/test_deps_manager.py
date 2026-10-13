"""运行期依赖安装（deps_manager）的单元测试。

三条要钉死的约定：
1. **不联网也能测的纯逻辑**：版本归一化、wheel 兼容性判断、清单自洽；
2. **sys.path 追加而非前置**：打包产物自带 numpy，前置会让外挂版本覆盖它
   （实测直接崩），这条是本次实现踩出来的核心教训；
3. **"文件在"不等于"能用"**：``deps_present``（只看文件）与 ``deps_installed``
   （真导入）必须分开，前者给只读自检用，后者才是可用性判据。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.core import deps_manager as dm  # noqa: E402


class TestManifest:
    def test_manifest_is_nonempty_and_unique(self):
        names = [name for name, _ in dm.DEPS_MANIFEST]
        assert names, "依赖清单不能为空"
        assert len(names) == len(set(names)), "清单里有重复包名"

    def test_manifest_has_engine_and_key_deps(self):
        """转写引擎与它的关键依赖必须在清单里——少一个就导入失败。"""
        names = {dm._normalize(n) for n, _ in dm.DEPS_MANIFEST}
        for required in ("faster-whisper", "ctranslate2", "av", "tokenizers", "onnxruntime"):
            assert required in names, f"清单缺关键依赖：{required}"

    def test_required_imports_are_in_manifest(self):
        """``REQUIRED_IMPORTS`` 里的每个包都得在清单里（否则装完必然验不过）。"""
        names = {dm._normalize(n) for n, _ in dm.DEPS_MANIFEST}
        for name in dm.REQUIRED_IMPORTS:
            assert dm._normalize(name) in names, (
                f"{name} 在验证清单里却不在安装清单里——装完永远验不过"
            )

    def test_av_is_pinned_below_17(self):
        """``av`` 必须钉在 16.x。

        ``av`` 19 删了 ``metadata_errors`` 参数，而 faster-whisper 仍在传它，
        真跑转写会 ``TypeError``；``av`` 14 及更早在 Python 3.12 上导入即崩
        （``KeyError: '__reduce_cython__'``）。这是实跑才发现、pip 解析不出的
        约束，用测试钉住，免得有人"顺手升级"。
        """
        av_version = dict(dm.DEPS_MANIFEST).get("av")
        assert av_version is not None, "清单里没有 av"
        major = int(av_version.split(".")[0])
        assert major == 16, (
            f"av 被钉在 {av_version}，但只有 15/16 实测可用"
            "（19 缺 metadata_errors，14 及更早在 cp312 上导入崩）"
        )


class TestVersionHelpers:
    def test_normalize_equates_hyphen_and_underscore(self):
        assert dm._normalize("faster-whisper") == dm._normalize("faster_whisper")
        assert dm._normalize("huggingface-hub") == dm._normalize("huggingface_hub")

    def test_version_prefix_matches_accepts_rc_variants(self):
        """上游可能把 rc 版推到 simple 索引（实测 PyYAML/colorama），要认。"""
        assert dm._version_prefix_matches("6.0.2", "6.0.2")
        assert dm._version_prefix_matches("6.0.2rc1", "6.0.2")
        assert not dm._version_prefix_matches("6.0.3", "6.0.2")


class TestWheelCompatibility:
    def test_accepts_win_amd64_and_any(self):
        py = f"cp{sys.version_info[0]}{sys.version_info[1]}"
        assert dm._wheel_compatible(f"pkg-1.0-{py}-{py}-win_amd64.whl")
        assert dm._wheel_compatible(f"pkg-1.0-{py}-none-any.whl")

    def test_rejects_foreign_platform(self):
        py = f"cp{sys.version_info[0]}{sys.version_info[1]}"
        assert not dm._wheel_compatible(f"pkg-1.0-{py}-{py}-manylinux_2_17_x86_64.whl")
        assert not dm._wheel_compatible(f"pkg-1.0-{py}-{py}-macosx_11_0_arm64.whl")

    def test_accepts_abi3_wheels(self):
        """``cp310-abi3`` 这类前向兼容轮子要在 cp312 上被接受（protobuf 就是）。"""
        assert dm._wheel_compatible("protobuf-5.27.3-cp310-abi3-win_amd64.whl")

    def test_rejects_non_wheel(self):
        assert not dm._wheel_compatible("pkg-1.0.tar.gz")


class TestSysPathAppendNotPrepend:
    """外挂目录必须**追加**到 sys.path 末尾，不得前置。

    打包产物自带 numpy；依赖集里也有 numpy。前置会让外挂版本盖住产物里的，
    实测抛 ``'numpy.ufunc' object has no attribute '__module__'``。
    """

    def test_register_appends_to_end(self, tmp_path):
        root = tmp_path / "deps"
        root.mkdir()
        before = list(sys.path)
        try:
            dm.register_deps(root)
            assert str(root) in sys.path
            assert sys.path[-1] == str(root), "外挂目录应追加到末尾，不是插到最前"
            assert sys.path[0] != str(root)
        finally:
            sys.path[:] = before

    def test_register_is_idempotent(self, tmp_path):
        root = tmp_path / "deps"
        root.mkdir()
        before = list(sys.path)
        try:
            dm.register_deps(root)
            dm.register_deps(root)
            assert sys.path.count(str(root)) == 1
        finally:
            sys.path[:] = before

    def test_register_missing_dir_returns_false(self, tmp_path):
        assert dm.register_deps(tmp_path / "nope") is False


class TestPresentVsInstalled:
    """``deps_present``（只看文件）与 ``deps_installed``（真导入）必须分开。"""

    def test_present_false_on_empty_dir(self, tmp_path):
        root = tmp_path / "deps"
        root.mkdir()
        assert dm.deps_present(root) is False

    def test_present_true_when_dirs_exist_even_if_not_importable(self, tmp_path):
        """文件目录齐了就报 present——**不**导入（自检不能有副作用）。"""
        root = tmp_path / "deps"
        for name in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime", "av"):
            (root / name).mkdir(parents=True)
        assert dm.deps_present(root) is True

    def test_installed_false_when_placeholder_dirs(self, tmp_path):
        """空目录能骗过 present，但骗不过 installed（真导入会失败）。"""
        root = tmp_path / "deps"
        for name in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime", "av"):
            (root / name).mkdir(parents=True)
        assert dm.deps_installed(root) is False


class TestDllDirDiscovery:
    """DLL 目录扫描要递归、要不漏包内部的 DLL 目录。

    ctranslate2 把 DLL 放在**包内部**（``ctranslate2/*.dll``），
    onnxruntime 在 ``onnxruntime/capi/``，av 在顶层 ``av.libs/``。早先只登记
    顶层与 ``*.libs``，漏了包内部那两处，于是 ``import ctranslate2`` 抛
    ``DLL load failed``。这条把"必须递归"钉住。
    """

    def test_finds_dll_in_package_subdir(self, tmp_path):
        root = tmp_path / "deps"
        (root / "ctranslate2").mkdir(parents=True)
        (root / "ctranslate2" / "ctranslate2.dll").write_bytes(b"MZ")
        (root / "av.libs").mkdir(parents=True)
        (root / "av.libs" / "avcodec.dll").write_bytes(b"MZ")
        (root / "onnxruntime" / "capi").mkdir(parents=True)
        (root / "onnxruntime" / "capi" / "onnxruntime.dll").write_bytes(b"MZ")

        dirs = {str(d) for d in dm._dll_dirs(root)}
        assert str(root / "ctranslate2") in dirs, "漏了包内部的 DLL 目录"
        assert str(root / "av.libs") in dirs
        assert str(root / "onnxruntime" / "capi") in dirs

    def test_no_dll_dirs_when_none_present(self, tmp_path):
        root = tmp_path / "deps"
        (root / "somepkg").mkdir(parents=True)
        assert dm._dll_dirs(root) == []


class TestInstallDepsContract:
    """``install_deps`` 的失败契约：不抛异常，以结果返回可读原因。"""

    def test_unwritable_root_returns_result_not_exception(self, tmp_path, monkeypatch):
        """目录建不出来时返回 ok=False，不炸调用方（后台线程尤其需要）。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("iam a file", encoding="utf-8")

        def boom(*a, **k):
            raise OSError("拒绝访问")

        monkeypatch.setattr(Path, "mkdir", boom)
        result = dm.install_deps(blocker / "deps")
        assert result.ok is False
        assert result.message

    def test_cancel_before_start_returns_cancelled(self, tmp_path):
        """取消检查在第一个包之前就为真时应报 cancelled，且不下载。"""
        result = dm.install_deps(tmp_path / "deps", cancel_check=lambda: True)
        assert result.cancelled is True
        assert result.ok is False

    def test_plan_failure_message_names_the_packages(self, tmp_path, monkeypatch):
        """所有包都解析不出（模拟断网）时，报错要点出是哪些包。"""
        monkeypatch.setattr(dm, "resolve_wheel_url", lambda *a, **k: None)
        result = dm.install_deps(tmp_path / "deps")
        assert result.ok is False
        assert "无法从镜像取到" in result.message


class TestUninstall:
    def test_uninstall_removes_dir_and_path_entry(self, tmp_path):
        root = tmp_path / "deps"
        (root / "faster_whisper").mkdir(parents=True)
        before = list(sys.path)
        try:
            dm.register_deps(root)
            assert str(root) in sys.path
            ok, _ = dm.uninstall_deps(root)
            assert ok is True
            assert not root.exists()
            assert str(root) not in sys.path
        finally:
            sys.path[:] = before

    def test_uninstall_missing_dir_is_ok(self, tmp_path):
        ok, detail = dm.uninstall_deps(tmp_path / "nope")
        assert ok is True
        assert detail


class TestBuildShipsStableAbiDll:
    """打包脚本必须把 ``python3.dll`` 带进产物。

    为什么单独钉这条：外挂引擎里的 ``tokenizers`` / ``av`` / ``faster_whisper``
    是编译时链接 ``python3.dll``（稳定 ABI 转发库）的 .pyd；PyInstaller 只带
    版本专用的 ``python312.dll``，**不会**带 ``python3.dll``。缺了它，冻结版
    导入这些包一律 ``DLL load failed``——而应用内装的引擎会因此静默不可用。
    实测补上这个 70 KB 的 DLL 后三个包全部导入成功。
    """

    def test_build_package_adds_stable_abi_dll(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "tools" / "build_package.py").read_text(encoding="utf-8")
        assert "stable_abi_dll" in text, "打包脚本里没有 stable_abi_dll 逻辑"
        assert "python3.dll" in text, "打包脚本没有处理 python3.dll"
        # 必须真的加进 --add-data（否则函数在也没用）
        assert "--add-data" in text

    def test_stable_abi_dll_helper_finds_it(self):
        """helper 应能在本机解释器旁边找到 python3.dll（或明确返回 None）。"""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "_bp_for_test", root / "tools" / "build_package.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        found = module.stable_abi_dll()
        if sys.platform == "win32":
            # Windows 上通常能找到；找不到也不能崩（返回 None 是合法降级）
            assert found is None or found.name == "python3.dll"
        else:
            assert found is None
