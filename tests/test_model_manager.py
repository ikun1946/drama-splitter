"""模型下载与选择测试。

下载链路的验收方式是**起一个本地 HTTP 服务器真实跑一遍**：走的是与线上
完全相同的 curl 代码路径（同样的断点续传、截断校验、取消与清理逻辑），
但不依赖外网，因此可以在任何环境下重复运行。

线上源（hf-mirror）只在 `TestRealDownload` 里做一次真实校验，默认跳过，
由环境变量 `DRAMA_RUN_NETWORK_TESTS=1` 打开。
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
from pathlib import Path

import pytest

from app.core.app_config import AppConfig, default_models_root
from app.core.model_manager import (
    MODEL_CATALOG,
    ModelFile,
    ModelSpec,
    delete_model,
    download_model,
    free_space_bytes,
    installed_state,
    llm_spec,
    spec_by_key,
    validate_models_root,
    whisper_spec,
)


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_keys_are_unique(self):
        keys = [spec.key for spec in MODEL_CATALOG]
        assert len(keys) == len(set(keys)), f"键重复：{keys}"

    def test_whisper_specs_have_four_required_files(self, ):
        for tier in ("tiny", "small", "large-v3"):
            spec = whisper_spec(tier)
            names = {item.name for item in spec.files}
            assert names == {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt"}
            assert spec.subdir == f"faster-whisper-{tier}"

    def test_model_bin_has_meaningful_minimum(self):
        """权重文件的体积下限必须足够大，否则截断的下载会被当成成功。"""
        spec = whisper_spec("small")
        weight = next(item for item in spec.files if item.name == "model.bin")
        assert weight.min_bytes >= 1024 * 1024

    def test_llm_spec_matches_repo_naming(self):
        spec = llm_spec("2B", "Q4_K_M")
        assert spec.repo == "unsloth/Qwen3.5-2B-GGUF"
        assert spec.files[0].name == "Qwen3.5-2B-Q4_K_M.gguf"
        assert spec.subdir == "llm/Qwen3.5-2B-GGUF"

    def test_urls_point_at_resolve_main(self):
        urls = whisper_spec("tiny").urls("https://example.com")
        assert urls["model.bin"] == (
            "https://example.com/Systran/faster-whisper-tiny/resolve/main/model.bin"
        )

    def test_spec_by_key_roundtrip(self):
        for key in ("whisper:tiny", "whisper:large-v3", "llm:2B:Q4_K_M", "llm:9B:Q5_K_M"):
            spec = spec_by_key(key)
            assert spec is not None, key
            assert spec.key == key

    def test_unknown_key_returns_none(self):
        assert spec_by_key("whisper:huge") is None
        assert spec_by_key("乱写") is None

    def test_catalog_has_whisper_and_llm(self):
        kinds = {spec.kind for spec in MODEL_CATALOG}
        assert kinds == {"whisper", "llm"}


# ---------------------------------------------------------------------------
# 状态与目录
# ---------------------------------------------------------------------------


class TestInstallState:
    def test_missing_everything(self, tmp_path):
        state = installed_state(whisper_spec("tiny"), tmp_path)
        assert not state.ready
        assert not state.partial
        assert len(state.missing) == 4
        assert state.describe() == "未安装"

    def test_complete_install_is_ready(self, tmp_path):
        spec = whisper_spec("tiny")
        directory = spec.target_dir(tmp_path)
        directory.mkdir(parents=True)
        directory.joinpath("model.bin").write_bytes(b"x" * (1024 * 1024))
        for name in ("config.json", "tokenizer.json", "vocabulary.txt"):
            directory.joinpath(name).write_text("{}", encoding="utf-8")
        state = installed_state(spec, tmp_path)
        assert state.ready
        assert "已安装" in state.describe()

    def test_truncated_weight_is_not_ready(self, tmp_path):
        """权重文件过小 = 下载被截断，绝不能算装好。"""
        spec = whisper_spec("tiny")
        directory = spec.target_dir(tmp_path)
        directory.mkdir(parents=True)
        directory.joinpath("model.bin").write_bytes(b"x" * 1024)  # 远小于 1MB
        for name in ("config.json", "tokenizer.json", "vocabulary.txt"):
            directory.joinpath(name).write_text("{}", encoding="utf-8")
        state = installed_state(spec, tmp_path)
        assert not state.ready
        assert state.truncated == ["model.bin"]
        assert state.partial

    def test_zero_byte_file_is_not_ready(self, tmp_path):
        """配置文件可以为小，但**不能为空**——0 字节说明下载中断。"""
        spec = whisper_spec("tiny")
        directory = spec.target_dir(tmp_path)
        directory.mkdir(parents=True)
        directory.joinpath("model.bin").write_bytes(b"x" * (1024 * 1024))
        directory.joinpath("config.json").write_bytes(b"")
        directory.joinpath("tokenizer.json").write_text("{}", encoding="utf-8")
        directory.joinpath("vocabulary.txt").write_text("a\n", encoding="utf-8")
        state = installed_state(spec, tmp_path)
        assert not state.ready
        assert state.truncated == ["config.json"]

    def test_small_config_files_count_as_complete(self, tmp_path):
        """合法的小配置文件（如 2 字节的 `{}`）不能被判成不完整。"""
        spec = whisper_spec("tiny")
        directory = spec.target_dir(tmp_path)
        directory.mkdir(parents=True)
        directory.joinpath("model.bin").write_bytes(b"x" * (1024 * 1024))
        directory.joinpath("config.json").write_text("{}", encoding="utf-8")
        directory.joinpath("tokenizer.json").write_text("{}", encoding="utf-8")
        directory.joinpath("vocabulary.txt").write_text("a\n", encoding="utf-8")
        assert installed_state(spec, tmp_path).ready

    def test_validate_creates_directory(self, tmp_path):
        target = tmp_path / "a" / "b"
        ok, detail = validate_models_root(target)
        assert ok and target.exists() and str(target) == detail

    def test_free_space_is_reported(self, tmp_path):
        free = free_space_bytes(tmp_path)
        assert free is None or free > 0


# ---------------------------------------------------------------------------
# 本地服务器上的真实下载
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - 测试里不需要访问日志
        pass


class LocalRepoServer:
    """把临时目录当作镜像服务器，复现 `/<repo>/resolve/main/<file>` 结构。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)  # noqa: E731
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local_repo(tmp_path):
    """构造一个假的模型仓库（含 4 个文件，其中权重刚好达标）。"""
    server_root = tmp_path / "server"
    repo_dir = server_root / "test" / "demo-model" / "resolve" / "main"
    repo_dir.mkdir(parents=True)
    repo_dir.joinpath("config.json").write_text('{"ok": true}', encoding="utf-8")
    repo_dir.joinpath("model.bin").write_bytes(b"M" * 4096)
    repo_dir.joinpath("tokenizer.json").write_text("{}", encoding="utf-8")
    repo_dir.joinpath("vocabulary.txt").write_text("a\nb\n", encoding="utf-8")

    server = LocalRepoServer(server_root)
    yield server, repo_dir
    server.close()


def demo_spec(**overrides) -> ModelSpec:
    """体积下限设小，方便用几 KB 的文件测完整流程。"""
    data = dict(
        key="test:demo",
        kind="whisper",
        label="测试模型",
        repo="test/demo-model",
        subdir="demo-model",
        files=(
            ModelFile("config.json", 8),
            ModelFile("model.bin", 1024),
            ModelFile("tokenizer.json", 2),
            ModelFile("vocabulary.txt", 2),
        ),
    )
    data.update(overrides)
    return ModelSpec(**data)


class TestDownload:
    def test_downloads_all_files(self, local_repo, tmp_path):
        server, repo_dir = local_repo
        expected = sum(f.stat().st_size for f in repo_dir.iterdir() if f.is_file())
        events: list[tuple[int, int, str]] = []
        result = download_model(
            demo_spec(),
            tmp_path / "models",
            endpoint=server.endpoint,
            on_progress=lambda done, total, name, index, count: events.append(
                (done, total, name)
            ),
        )
        assert result.ok, result.message
        assert result.installed_dir.exists()
        # 期望值按服务器上的真实文件大小算，不写死——避免测试自己算错
        assert result.downloaded_bytes == expected, f"应为 {expected}，实得 {result.downloaded_bytes}"
        assert events, "必须回报进度"
        assert events[-1][1] > 0, "本地服务器支持 HEAD，应能拿到总大小"

    def test_no_leftover_part_files(self, local_repo, tmp_path):
        server, _ = local_repo
        result = download_model(demo_spec(), tmp_path / "models", endpoint=server.endpoint)
        assert result.ok
        parts = list(result.installed_dir.glob("*.part"))
        assert parts == [], f"残留临时文件：{parts}"

    def test_truncated_remote_file_is_rejected(self, local_repo, tmp_path):
        """远端给的文件小于下限时必须失败，不能把截断的权重当成功。"""
        server, repo_dir = local_repo
        repo_dir.joinpath("model.bin").write_bytes(b"short")
        result = download_model(demo_spec(), tmp_path / "models", endpoint=server.endpoint)
        assert not result.ok
        assert "下限" in result.message or "截断" in result.message, result.message
        # 失败后该文件不应留在目标目录
        assert not (tmp_path / "models" / "demo-model" / "model.bin").exists()

    def test_missing_remote_file_reports_clearly(self, local_repo, tmp_path):
        server, repo_dir = local_repo
        repo_dir.joinpath("tokenizer.json").unlink()
        result = download_model(demo_spec(), tmp_path / "models", endpoint=server.endpoint)
        assert not result.ok
        assert "tokenizer.json" in result.message

    def test_cancel_cleans_up(self, local_repo, tmp_path):
        """取消必须终止下载并清掉 .part，不留半截文件。"""
        server, _ = local_repo
        state = {"calls": 0}

        def cancel_check() -> bool:
            state["calls"] += 1
            return state["calls"] > 1  # 第一次进度回调之后就取消

        result = download_model(
            demo_spec(),
            tmp_path / "models",
            endpoint=server.endpoint,
            on_progress=lambda *args: None,
            cancel_check=cancel_check,
        )
        assert result.cancelled, result.message
        parts = list((tmp_path / "models").rglob("*.part"))
        assert parts == [], f"取消后仍残留临时文件：{parts}"

    def test_installed_files_are_skipped_on_rerun(self, local_repo, tmp_path):
        """已装好的文件不重复下载——再点一次不该重下 3GB。"""
        server, _ = local_repo
        first = download_model(demo_spec(), tmp_path / "models", endpoint=server.endpoint)
        assert first.ok
        second = download_model(demo_spec(), tmp_path / "models", endpoint=server.endpoint)
        assert second.ok
        assert second.downloaded_bytes == first.downloaded_bytes

    def test_unwritable_root_is_refused(self, local_repo, tmp_path):
        """模型目录不可写时要给出可读原因，而不是抛异常。

        用一个**文件**冒充目录来制造不可写场景（Windows 上 chmod 不生效）。
        """
        server, _ = local_repo
        blocker = tmp_path / "blocked"
        blocker.write_text("我不是目录", encoding="utf-8")
        result = download_model(demo_spec(), blocker, endpoint=server.endpoint)
        assert not result.ok
        assert result.message

    def test_progress_total_unknown_when_head_fails(self, tmp_path, local_repo, monkeypatch):
        """总大小探测不到时 total 必须为 0，界面据此显示"未知"而不是编数字。"""
        import app.core.model_manager as manager

        server, _ = local_repo
        monkeypatch.setattr(manager, "remote_size", lambda url: None)
        events: list[int] = []
        result = download_model(
            demo_spec(),
            tmp_path / "models",
            endpoint=server.endpoint,
            on_progress=lambda done, total, name, index, count: events.append(total),
        )
        assert result.ok
        assert all(total == 0 for total in events)


class TestDelete:
    def test_delete_removes_only_target(self, local_repo, tmp_path):
        server, _ = local_repo
        root = tmp_path / "models"
        assert download_model(demo_spec(), root, endpoint=server.endpoint).ok
        other = whisper_spec("tiny")
        other_dir = other.target_dir(root)
        other_dir.mkdir(parents=True)
        other_dir.joinpath("config.json").write_text("{}", encoding="utf-8")

        ok, message = delete_model(demo_spec(), root)
        assert ok, message
        assert not (root / "demo-model").exists()
        assert other_dir.exists(), "不得删除其他模型的目录"

    def test_delete_missing_model_is_reported(self, tmp_path):
        ok, message = delete_model(demo_spec(), tmp_path)
        assert not ok and message


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------


class TestAppConfig:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        config = AppConfig(models_dir="D:/models", last_download_key="whisper:small")
        config.save(path)
        loaded = AppConfig.load(path)
        assert loaded.models_dir == "D:/models"
        assert loaded.last_download_key == "whisper:small"
        assert loaded.endpoint == "https://hf-mirror.com"

    def test_missing_file_yields_defaults(self, tmp_path):
        loaded = AppConfig.load(tmp_path / "nope.json")
        assert loaded.models_dir is None
        assert loaded.resolved_models_dir() == default_models_root()

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        loaded = AppConfig.load(path)
        assert loaded.models_dir is None

    def test_resolved_dir_prefers_configured(self, tmp_path):
        config = AppConfig(models_dir=str(tmp_path / "custom"))
        assert config.resolved_models_dir() == tmp_path / "custom"


# ---------------------------------------------------------------------------
# 与搜索链的联动
# ---------------------------------------------------------------------------


class TestSearchRoots:
    def test_configured_dir_is_first_candidate(self, tmp_path, monkeypatch):
        """用户在界面里指定的目录必须优先于 exe 同级目录被使用。"""
        import app.core.app_config as config_module
        from app.core.asr import model_search_roots

        monkeypatch.setattr(
            config_module.AppConfig,
            "load",
            classmethod(lambda cls, path=None: AppConfig(models_dir=str(tmp_path / "chosen"))),
        )
        monkeypatch.delenv("DRAMA_MODELS_DIR", raising=False)
        roots = model_search_roots()
        assert roots[0] == tmp_path / "chosen"

    def test_env_var_beats_config(self, tmp_path, monkeypatch):
        import app.core.app_config as config_module
        from app.core.asr import model_search_roots

        monkeypatch.setattr(
            config_module.AppConfig,
            "load",
            classmethod(lambda cls, path=None: AppConfig(models_dir=str(tmp_path / "chosen"))),
        )
        monkeypatch.setenv("DRAMA_MODELS_DIR", str(tmp_path / "from_env"))
        assert model_search_roots()[0] == tmp_path / "from_env"

    def test_roots_are_deduplicated(self, monkeypatch):
        from app.core.asr import model_search_roots

        roots = model_search_roots()
        keys = [str(r).lower() for r in roots]
        assert len(keys) == len(set(keys)), f"出现重复候选：{keys}"


# ---------------------------------------------------------------------------
# 真实镜像（默认跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("DRAMA_RUN_NETWORK_TESTS") != "1",
    reason="需要外网下载，设置 DRAMA_RUN_NETWORK_TESTS=1 后运行",
)
class TestRealDownload:
    def test_remote_size_is_measurable(self):
        from app.core.model_manager import remote_size

        size = remote_size(
            "https://hf-mirror.com/Systran/faster-whisper-tiny/resolve/main/model.bin"
        )
        assert size and size > 60 * 1024 * 1024, f"探测到的体积异常：{size}"

    def test_download_tiny_into_temp_dir(self, tmp_path):
        result = download_model(whisper_spec("tiny"), tmp_path / "models")
        assert result.ok, result.message
        assert installed_state(whisper_spec("tiny"), tmp_path / "models").ready
