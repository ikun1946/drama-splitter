"""应用内更新：版本解析、资产挑选、API 失败分支、SHA-256 校验。

界面测试绝不联网，这里也一样：``releases/latest`` 与资产下载都打到本地
``http.server`` 上，真网络只在 ``tools/gui_smoke.py`` 与真机上验证。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import updater  # noqa: E402


# ---------------------------------------------------------------------------
# 版本解析
# ---------------------------------------------------------------------------


class TestParseVersion:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1.0.0", (1, 0, 0)),
            ("v1.0.0", (1, 0, 0)),
            ("V2.3.4", (2, 3, 4)),
            ("v1.1", (1, 1, 0)),
            ("v3", (3, 0, 0)),
            ("  v1.0.0  ", (1, 0, 0)),
            ("v1.0.0-rc1", (1, 0, 0)),
        ],
    )
    def test_valid(self, text, expected):
        assert updater.parse_version(text) == expected

    @pytest.mark.parametrize("text", [None, "", "abc", "v", "1.2.3.4", "v1.x.0"])
    def test_invalid_returns_none(self, text):
        """解析不了必须返回 None 而不是抛——远端 tag 的形态不由我们控制。"""
        assert updater.parse_version(text) is None


class TestIsNewer:
    def test_remote_ahead(self):
        assert updater.is_newer("1.0.1", "1.0.0") is True
        assert updater.is_newer("v2.0.0", "1.9.9") is True

    def test_same_or_behind(self):
        assert updater.is_newer("1.0.0", "1.0.0") is False
        assert updater.is_newer("1.0.0", "1.0.1") is False

    def test_unparsable_side_gives_false(self):
        """宁可漏报也不误报：解析不了时不能让用户反复看到"有新版"却装不上。"""
        assert updater.is_newer("garbage", "1.0.0") is False
        assert updater.is_newer("1.0.1", None) is False


# ---------------------------------------------------------------------------
# 资产挑选
# ---------------------------------------------------------------------------


def make_info(**kw) -> updater.UpdateInfo:
    base = dict(
        tag="v1.0.1",
        version="1.0.1",
        url="https://example.invalid/r",
        notes="修了点东西",
        setup_url="https://example.invalid/a-setup.exe",
        setup_size=100,
        setup_digest="sha256:aa",
        zip_url="https://example.invalid/a-win64.zip",
        zip_size=200,
        zip_digest="sha256:bb",
    )
    base.update(kw)
    return updater.UpdateInfo(**base)


class TestAssetSelection:
    def test_installed_copy_picks_setup(self):
        url, size, digest = make_info().asset_for(installed=True)
        assert url.endswith("-setup.exe") and size == 100 and digest == "sha256:aa"

    def test_portable_copy_picks_zip(self):
        url, size, digest = make_info().asset_for(installed=False)
        assert url.endswith("-win64.zip") and size == 200 and digest == "sha256:bb"

    def test_missing_asset_returns_none(self):
        assert make_info(setup_url="").asset_for(installed=True) is None
        assert make_info(zip_url="").asset_for(installed=False) is None


class TestInstalledCopy:
    def test_detects_uninstaller(self, tmp_path, monkeypatch):
        from app.core import app_config

        monkeypatch.setattr(app_config, "app_dir", lambda: tmp_path)
        assert updater.installed_copy() is False
        (tmp_path / "uninstall").mkdir()
        (tmp_path / "uninstall" / "unins000.exe").write_bytes(b"")
        assert updater.installed_copy() is True


# ---------------------------------------------------------------------------
# 本地 HTTP 服务：API 与下载
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    payload = b""
    status = 200
    body_blob = b""
    headers_extra: dict = {}

    def log_message(self, *args):  # noqa: D102 - 测试里别刷屏
        pass

    def _send(self):
        self.send_response(self.status)
        for key, value in self.headers_extra.items():
            self.send_header(key, value)
        if self.path.startswith("/download"):
            self.send_header("Content-Length", str(len(self.body_blob)))
            self.end_headers()
            self.wfile.write(self.body_blob)
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    do_GET = _send


@pytest.fixture
def server(request):
    handler = type("H", (_Handler,), {})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    handler.base_url = f"http://127.0.0.1:{srv.server_port}"
    yield handler
    srv.shutdown()


def release_payload(**over):
    body = {
        "tag_name": "v1.0.1",
        "html_url": "https://example.invalid/r",
        "body": "更新说明",
        "assets": [
            {
                "name": "drama-splitter-1.0.1-setup.exe",
                "browser_download_url": "https://example.invalid/a-setup.exe",
                "size": 100,
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": "drama-splitter-1.0.1-win64.zip",
                "browser_download_url": "https://example.invalid/a-win64.zip",
                "size": 200,
                "digest": "sha256:" + "b" * 64,
            },
        ],
    }
    body.update(over)
    return body


class TestCheckForUpdate:
    def _run(self, server, monkeypatch, **over):
        server.payload = json.dumps(release_payload(**over)).encode("utf-8")
        server.status = over.pop("_status", 200)
        monkeypatch.setattr(updater, "RELEASE_API", server.base_url + "/latest")
        return updater.check_for_update("1.0.0")

    def test_newer_version_reports_update(self, server, monkeypatch):
        result = self._run(server, monkeypatch)
        assert result.ok and result.has_update
        assert result.info is not None
        assert result.info.version == "1.0.1"
        assert result.info.notes == "更新说明"
        assert result.info.setup_digest.startswith("sha256:")

    def test_same_version_is_not_an_update(self, server, monkeypatch):
        result = self._run(server, monkeypatch, tag_name="v1.0.0")
        assert result.ok and not result.has_update
        assert "最新" in result.message

    def test_404_says_publish_address_is_gone(self, server, monkeypatch):
        server.payload = b"{}"
        server.status = 404
        monkeypatch.setattr(updater, "RELEASE_API", server.base_url + "/latest")
        result = updater.check_for_update("1.0.0")
        assert not result.ok
        assert "失效" in result.message

    def test_403_says_rate_limited_not_offline(self, server, monkeypatch):
        """限流与断网是两回事，文案不能混。"""
        server.payload = b"{}"
        server.status = 403
        monkeypatch.setattr(updater, "RELEASE_API", server.base_url + "/latest")
        result = updater.check_for_update("1.0.0")
        assert not result.ok
        assert "频繁" in result.message

    def test_non_json_is_reported_not_raised(self, server, monkeypatch):
        server.payload = b"<html>not json</html>"
        server.status = 200
        monkeypatch.setattr(updater, "RELEASE_API", server.base_url + "/latest")
        result = updater.check_for_update("1.0.0")
        assert not result.ok and "JSON" in result.message

    def test_release_without_assets(self, server, monkeypatch):
        result = self._run(server, monkeypatch, assets=[])
        assert not result.ok
        assert "安装包" in result.message

    def test_never_raises_on_dead_host(self, monkeypatch):
        monkeypatch.setattr(
            updater, "RELEASE_API", "http://127.0.0.1:1/releases/latest"
        )
        result = updater.check_for_update("1.0.0", timeout=2)
        assert not result.ok and result.message


# ---------------------------------------------------------------------------
# 下载与 SHA-256 校验
# ---------------------------------------------------------------------------


class TestDownloadAsset:
    def test_download_and_verify_success(self, server, tmp_path, monkeypatch):
        blob = b"x" * 4096
        server.body_blob = blob
        server.status = 200
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        monkeypatch.setattr(
            updater,
            "_curl",
            lambda args, timeout=60: (0, "") if "-o" in args else (0, ""),
        )

        def fake_curl(args, timeout=60):
            target = Path(args[args.index("-o") + 1])
            target.write_bytes(blob)
            return 0, ""

        monkeypatch.setattr(updater, "_curl", fake_curl)
        target = tmp_path / "setup.exe"
        seen = []
        outcome = updater.download_asset(
            "https://example.invalid/a", target, digest=digest,
            on_progress=lambda *a: seen.append(a),
        )
        assert outcome.ok and outcome.path == target
        assert target.read_bytes() == blob
        assert not (tmp_path / "setup.exe.part").exists()
        assert seen, "应回报一次进度"

    def test_checksum_mismatch_deletes_the_file(self, tmp_path, monkeypatch):
        """安全边界：校验不过必须删掉，绝不把坏包交给安装器。"""
        blob = b"corrupted"
        target = tmp_path / "setup.exe"

        def fake_curl(args, timeout=60):
            Path(args[args.index("-o") + 1]).write_bytes(blob)
            return 0, ""

        monkeypatch.setattr(updater, "_curl", fake_curl)
        outcome = updater.download_asset(
            "https://example.invalid/a", target, digest="sha256:" + "0" * 64
        )
        assert not outcome.ok
        assert "校验" in outcome.message
        assert not target.exists(), "校验失败的目标文件必须删掉"
        assert not (tmp_path / "setup.exe.part").exists(), ".part 也要删掉"

    def test_empty_url_is_reported(self, tmp_path):
        outcome = updater.download_asset("", tmp_path / "x.exe")
        assert not outcome.ok and "为空" in outcome.message

    def test_curl_failure_is_reported_and_part_removed(self, tmp_path, monkeypatch):
        def fake_curl(args, timeout=60):
            Path(args[args.index("-o") + 1]).write_bytes(b"partial")
            return 22, "HTTP 404"

        monkeypatch.setattr(updater, "_curl", fake_curl)
        target = tmp_path / "setup.exe"
        outcome = updater.download_asset("https://example.invalid/a", target)
        assert not outcome.ok
        assert not (tmp_path / "setup.exe.part").exists()


class TestLaunchInstaller:
    def test_missing_file(self, tmp_path):
        ok, why = updater.launch_installer(tmp_path / "nope.exe")
        assert not ok and "不存在" in why

    def test_passes_silent_arguments(self, tmp_path, monkeypatch):
        setup = tmp_path / "setup.exe"
        setup.write_bytes(b"MZ")
        seen = {}

        def fake_popen(args, creationflags=0):
            seen["args"] = args
            return object()

        monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
        ok, _ = updater.launch_installer(setup)
        assert ok
        assert "/VERYSILENT" in seen["args"]
        assert "/NORESTART" in seen["args"]
