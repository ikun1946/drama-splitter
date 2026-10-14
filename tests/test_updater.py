"""应用内更新：版本解析、资产挑选、API 失败分支、SHA-256 校验。

界面测试绝不联网，这里也一样：``releases/latest`` 与资产下载都打到本地
``http.server`` 上，真网络只在 ``tools/gui_smoke.py`` 与真机上验证。
"""

from __future__ import annotations

import hashlib
import json
import time
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
        setup_api_url="https://api.example.invalid/assets/setup",
        zip_url="https://example.invalid/a-win64.zip",
        zip_size=200,
        zip_digest="sha256:bb",
        zip_api_url="https://api.example.invalid/assets/zip",
    )
    base.update(kw)
    return updater.UpdateInfo(**base)


class TestAssetSelection:
    """资产挑选要给**候选列表**：API 资产地址优先、官方地址兜底。

    实测背景：官方下载地址在 github.com，用户机连 21 秒都不通（本机也只有
    ~48KB/s），而 Release API 的资产地址走 api.github.com → 302 → CDN 通常能用。
    """

    def test_installed_copy_prefers_api_then_official(self):
        urls, size, digest = make_info().asset_for(installed=True)
        assert urls == [
            "https://api.example.invalid/assets/setup",
            "https://example.invalid/a-setup.exe",
        ], "API 资产地址必须排在官方 github.com 地址前面"
        assert size == 100 and digest == "sha256:aa"

    def test_portable_copy_prefers_api_then_official(self):
        urls, size, digest = make_info().asset_for(installed=False)
        assert urls == [
            "https://api.example.invalid/assets/zip",
            "https://example.invalid/a-win64.zip",
        ]
        assert size == 200 and digest == "sha256:bb"

    def test_official_url_alone_still_works(self):
        urls, _size, _digest = make_info(setup_api_url="").asset_for(installed=True)
        assert urls == ["https://example.invalid/a-setup.exe"]

    def test_api_url_alone_still_works(self):
        urls, _size, _digest = make_info(setup_url="").asset_for(installed=True)
        assert urls == ["https://api.example.invalid/assets/setup"]

    def test_missing_asset_returns_none(self):
        assert make_info(setup_url="", setup_api_url="").asset_for(installed=True) is None
        assert make_info(zip_url="", zip_api_url="").asset_for(installed=False) is None


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
    #: >0 时按块慢发（每块之间 sleep chunk_delay），用来复现「下载途中」的状态
    chunk_size = 0
    chunk_delay = 0.0

    def log_message(self, *args):  # noqa: D102 - 测试里别刷屏
        pass

    def _send(self):
        self.send_response(self.status)
        for key, value in self.headers_extra.items():
            self.send_header(key, value)
        if self.path.startswith("/download"):
            self.send_header("Content-Length", str(len(self.body_blob)))
            self.end_headers()
            if self.chunk_size:
                sent = 0
                while sent < len(self.body_blob):
                    piece = self.body_blob[sent:sent + self.chunk_size]
                    self.wfile.write(piece)
                    self.wfile.flush()
                    sent += len(piece)
                    if self.chunk_delay:
                        time.sleep(self.chunk_delay)
                return
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
                "url": "https://api.example.invalid/assets/setup",
                "size": 100,
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": "drama-splitter-1.0.1-win64.zip",
                "browser_download_url": "https://example.invalid/a-win64.zip",
                "url": "https://api.example.invalid/assets/zip",
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
        # API 资产地址必须取出来：官方下载地址在 github.com，部分网络连不上
        assert result.info.setup_api_url == "https://api.example.invalid/assets/setup"
        assert result.info.zip_api_url == "https://api.example.invalid/assets/zip"

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


def _require_curl() -> None:
    import shutil

    if shutil.which("curl") is None:
        pytest.skip("本机没有 curl——更新下载依赖它，跳过真实下载用例")


class TestDownloadAsset:
    def test_download_and_verify_success(self, server, tmp_path):
        _require_curl()
        blob = b"x" * 65536
        server.body_blob = blob
        server.status = 200
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        target = tmp_path / "setup.exe"
        seen: list[int] = []
        outcome = updater.download_asset(
            f"{server.base_url}/download",
            target,
            digest=digest,
            size=len(blob),
            on_progress=lambda done, total, *_: seen.append(done),
        )
        assert outcome.ok and outcome.path == target
        assert target.read_bytes() == blob
        assert not (tmp_path / "setup.exe.part").exists(), "完成后不该留下 .part"
        assert seen and seen[-1] == len(blob), "结束时要回报最终进度"

    def test_checksum_mismatch_deletes_the_file(self, server, tmp_path):
        """安全边界：校验不过必须删掉，绝不把坏包交给安装器。"""
        _require_curl()
        blob = b"corrupted"
        server.body_blob = blob
        server.status = 200
        outcome = updater.download_asset(
            f"{server.base_url}/download",
            tmp_path / "setup.exe",
            digest="sha256:" + "0" * 64,
            size=len(blob),
        )
        assert not outcome.ok
        assert "校验" in outcome.message
        assert not (tmp_path / "setup.exe").exists(), "校验失败的目标文件必须删掉"
        assert not (tmp_path / "setup.exe.part").exists(), ".part 也要删掉"

    def test_empty_url_is_reported(self, tmp_path):
        outcome = updater.download_asset("", tmp_path / "x.exe")
        assert not outcome.ok and "为空" in outcome.message

    def test_curl_failure_is_reported_and_part_removed(self, server, tmp_path):
        _require_curl()
        server.status = 404
        server.body_blob = b"nope"
        outcome = updater.download_asset(f"{server.base_url}/download", tmp_path / "setup.exe")
        assert not outcome.ok
        assert not (tmp_path / "setup.exe").exists()
        assert not (tmp_path / "setup.exe.part").exists(), "空 .part 该清掉"


class TestDownloadProgress:
    """进度条不动 = 三层全断：worker 没传回调、updater 只在结束时回调、界面没接。"""

    @staticmethod
    def _slow(server, blob, *, chunk=16384, delay=0.05):
        server.body_blob = blob
        server.status = 200
        server.chunk_size = chunk
        server.chunk_delay = delay

    def test_progress_is_reported_while_downloading(self, server, tmp_path):
        _require_curl()
        blob = b"x" * (256 * 1024)
        self._slow(server, blob)
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        seen: list[int] = []
        outcome = updater.download_asset(
            f"{server.base_url}/download",
            tmp_path / "a.bin",
            digest=digest,
            size=len(blob),
            on_progress=lambda done, total, *_: seen.append(done),
        )
        assert outcome.ok
        assert len(seen) >= 3, f"下载途中应多次上报，实际 {seen}"
        assert seen == sorted(seen), "进度必须单调不减"
        assert seen[-1] == len(blob)

    def test_cancel_keeps_partial_file_for_resume(self, server, tmp_path):
        """取消不能白扔已下载的部分：慢链路上一次下不完是常态。"""
        _require_curl()
        blob = b"y" * (512 * 1024)
        self._slow(server, blob, chunk=8192, delay=0.05)
        state = {"cancel": False}

        def on_progress(done, _total, *_rest):
            if done > 64 * 1024:
                state["cancel"] = True

        target = tmp_path / "a.bin"
        outcome = updater.download_asset(
            f"{server.base_url}/download",
            target,
            size=len(blob),
            on_progress=on_progress,
            cancel_check=lambda: state["cancel"],
        )
        part = tmp_path / "a.bin.part"
        assert not outcome.ok and outcome.cancelled
        assert part.is_file() and part.stat().st_size > 0, "取消必须保留 .part"
        assert outcome.partial_bytes == part.stat().st_size
        assert not target.exists()
        assert "已保留" in outcome.message


class TestDownloadCurlArgs:
    def test_no_total_time_limit(self, tmp_path):
        """--max-time 是总时限：慢链路上每隔 30 秒被掐断，永远下不完。"""
        args = updater.download_curl_args("https://example.invalid/a", tmp_path / "a.part")
        assert "--max-time" not in args, "不许再设总时限"
        assert "--speed-limit" in args and "--speed-time" in args, "但要保留真卡死判定"

    def test_resumes_only_when_part_exists(self, tmp_path):
        part = tmp_path / "a.part"
        assert "-C" not in updater.download_curl_args("https://x/y", part)
        part.write_bytes(b"half")
        args = updater.download_curl_args("https://x/y", part)
        assert "-C" in args

    def test_accept_header_is_present(self, tmp_path):
        """走 API 资产地址时必须带 Accept: application/octet-stream，否则拿到 JSON。"""
        args = updater.download_curl_args("https://api.github.com/x", tmp_path / "a.part")
        assert "Accept: application/octet-stream" in args


class TestAssetCandidates:
    """候选顺序：用户镜像 → 直连 → 内置加速源（只做兜底）。"""

    def test_direct_first_then_builtin_mirrors(self):
        urls = updater.asset_candidates(["https://github.com/a/b"])
        assert urls[0] == "https://github.com/a/b", "直连必须排第一"
        assert any(u.startswith(updater.BUILTIN_MIRRORS[0]) for u in urls[1:]), (
            "直连失败后要能回退到内置加速源"
        )

    def test_user_mirror_goes_first_and_disables_builtin(self):
        urls = updater.asset_candidates(
            ["https://github.com/a/b"], mirror="https://my-mirror.test/"
        )
        assert urls[0] == "https://my-mirror.test/https://github.com/a/b"
        assert len(urls) == 2, f"用户配了镜像就只走「镜像 + 直连」，实际 {urls}"

    def test_user_mirror_only_wraps_the_download_url(self):
        """API 资产地址不套镜像——它本身可达，只负责 302 到 CDN。"""
        api = "https://api.github.com/repos/o/r/releases/assets/1"
        dl = "https://github.com/o/r/releases/download/v1/a.exe"
        urls = updater.asset_candidates([api, dl], mirror="https://my-mirror.test/")
        assert urls == [
            "https://my-mirror.test/https://github.com/o/r/releases/download/v1/a.exe",
            api,
            dl,
        ]

    def test_mirror_off_uses_direct_only(self):
        for value in ("off", "none", "-"):
            urls = updater.asset_candidates(["https://github.com/a/b"], mirror=value)
            assert urls == ["https://github.com/a/b"], value

    def test_api_url_is_not_mirrored(self):
        """内置加速源只套 github.com 地址；API 地址本身可达，不该绕路。"""
        api = "https://api.github.com/repos/o/r/releases/assets/1"
        urls = updater.asset_candidates([api])
        assert urls == [api]

    def test_empty_input(self):
        assert updater.asset_candidates([]) == []
        assert updater.asset_candidates([], mirror="https://m.test/") == []


class TestMirrorUrl:
    def test_empty_is_passthrough(self):
        assert updater.mirror_url("https://github.com/a", "") == "https://github.com/a"
        assert updater.mirror_url("https://github.com/a", None) == "https://github.com/a"

    def test_prefix_form(self):
        assert updater.mirror_url("https://github.com/a", "https://m.test/") == (
            "https://m.test/https://github.com/a"
        )
        assert updater.mirror_url("https://github.com/a", "https://m.test") == (
            "https://m.test/https://github.com/a"
        )

    def test_placeholder_form(self):
        assert updater.mirror_url("https://github.com/a", "https://m.test/?u={url}") == (
            "https://m.test/?u=https://github.com/a"
        )


class TestDownloadStatusText:
    def test_with_total_shows_progress_speed_and_eta(self):
        text, determinate = updater.download_status_text(
            10 * 1024 * 1024, 40 * 1024 * 1024, 20.0
        )
        assert determinate
        assert "已下载 10.0 MB / 40.0 MB" in text
        assert "512.0 KB/s" in text
        assert "剩余约 1 分 0 秒" in text

    def test_without_total_is_indeterminate(self):
        text, determinate = updater.download_status_text(1024 * 1024, 0, 5.0)
        assert not determinate, "总大小未知时不能假装有百分比"
        assert "已下载 1.0 MB" in text

    def test_no_speed_before_half_a_second(self):
        text, _ = updater.download_status_text(1024, 2048, 0.1)
        assert "/s" not in text, "刚开始不到半秒，速度没有意义"

    def test_format_helpers(self):
        assert updater.format_size(0) == "0 B"
        assert updater.format_size(1536) == "1.5 KB"
        assert updater.format_size(3 * 1024 * 1024) == "3.0 MB"
        assert updater.format_duration(45) == "45 秒"
        assert updater.format_duration(200) == "3 分 20 秒"
        assert updater.format_duration(3900) == "1 小时 5 分"


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
        # 装完要让新版自己回来：installer.iss 靠这个参数决定是否重启
        assert "/RESTARTAPP=1" in seen["args"]


# ---------------------------------------------------------------------------
# 更新闭环：入口文案 / 待确认标记 / 免安装版解压 / 装完自动重启
# ---------------------------------------------------------------------------


class TestUpdateEntryLabel:
    """关掉弹窗之后，菜单是唯一还在的入口——文案必须把版本号带上。"""

    def test_without_available_version(self):
        assert updater.update_action_label() == "检查更新…"

    def test_with_available_version(self):
        label = updater.update_action_label("1.0.9")
        assert "检查更新" in label and "1.0.9" in label


class TestUpdatePendingMarker:
    """升级完要能告诉用户「已更新到 X」；中途取消则不能误报。"""

    @staticmethod
    def _config_in(tmp_path, monkeypatch):
        from app.core import app_config

        monkeypatch.setattr(app_config, "app_dir", lambda: tmp_path)

    def test_round_trip_reports_new_version_once(self, tmp_path, monkeypatch):
        self._config_in(tmp_path, monkeypatch)
        assert updater.mark_update_pending("1.0.9") is True
        assert updater.take_update_pending("1.0.9") == "1.0.9"
        # 消费一次就清掉，别每次启动都报一遍
        assert updater.take_update_pending("1.0.9") is None

    def test_cancelled_install_does_not_report(self, tmp_path, monkeypatch):
        self._config_in(tmp_path, monkeypatch)
        assert updater.mark_update_pending("1.0.9") is True
        # 用户取消了安装：跑的还是旧版 → 不报，但标记要清掉
        assert updater.take_update_pending("1.0.8") is None
        assert updater.take_update_pending("1.0.9") is None

    def test_tolerates_v_prefix(self, tmp_path, monkeypatch):
        self._config_in(tmp_path, monkeypatch)
        assert updater.mark_update_pending("v1.0.9") is True
        assert updater.take_update_pending("1.0.9") == "v1.0.9"

    def test_empty_version_is_not_recorded(self, tmp_path, monkeypatch):
        self._config_in(tmp_path, monkeypatch)
        assert updater.mark_update_pending("") is False
        assert updater.take_update_pending("1.0.9") is None


class TestPortableExtraction:
    """免安装版：解压到临时目录给用户替换，绝不原地覆盖。"""

    @staticmethod
    def _zip(tmp_path, members):
        import zipfile

        path = tmp_path / "pkg.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in members:
                archive.writestr(name, "data")
        return path

    def test_returns_inner_folder(self, tmp_path):
        path = self._zip(tmp_path, [
            "drama-splitter/drama-splitter.exe",
            "drama-splitter/_internal/x.txt",
        ])
        ok, where, why = updater.extract_portable_zip(path, "1.0.9")
        assert ok and where is not None
        assert where.name == "drama-splitter", "包里套的那层目录要直接返回，省得用户再点一层"
        assert (where / "drama-splitter.exe").is_file()
        assert "已解压到" in why

    def test_rejects_path_traversal(self, tmp_path):
        path = self._zip(tmp_path, ["../evil.txt", "drama-splitter/ok.txt"])
        ok, where, _ = updater.extract_portable_zip(path, "1.0.9")
        assert ok and where is not None
        assert (where / "ok.txt").is_file()
        assert not (tmp_path / "evil.txt").exists()
        assert not (where.parent / "evil.txt").exists(), "压缩包里的 ../ 不许写到解压目录外"

    def test_missing_file_is_reported(self, tmp_path):
        ok, where, why = updater.extract_portable_zip(tmp_path / "nope.zip", "1.0.9")
        assert not ok and where is None and "不存在" in why


class TestRelaunchAfterSilentUpdate:
    """静默更新装完必须把新版拉起来——否则用户只看到「窗口关了，什么都没有」。"""

    def test_updater_passes_restart_flag(self):
        import inspect

        source = inspect.getsource(updater.launch_installer)
        assert "/RESTARTAPP=1" in source, "更新器没告诉安装器装完要重启"

    def test_installer_has_relaunch_entry(self):
        text = (ROOT / "tools" / "installer.iss").read_text(encoding="utf-8")
        assert "RelaunchRequested" in text, "installer.iss 没有重启新版的入口"
        assert "{param:RESTARTAPP|0}" in text, "重启条件没读 /RESTARTAPP 参数"
        # 交互安装那条仍要 skipifsilent：托管静默部署不该被突然拉出一个界面
        assert "skipifsilent" in text


class TestDownloadCandidates:
    """第一个地址失败就换下一个——用户那边 github.com 不通，得靠 API 地址救回来。"""

    def test_falls_back_to_the_next_url(self, server, tmp_path):
        _require_curl()
        blob = b"z" * 65536
        server.body_blob = blob
        server.status = 200
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        dead = "http://127.0.0.1:1/nothing"          # 连不上 → curl 非零退出
        good = f"{server.base_url}/download"
        outcome = updater.download_asset(
            [dead, good], tmp_path / "a.bin", digest=digest, size=len(blob)
        )
        assert outcome.ok, outcome.message
        assert (tmp_path / "a.bin").read_bytes() == blob

    def test_all_candidates_fail_is_reported(self, tmp_path):
        _require_curl()
        outcome = updater.download_asset(
            ["http://127.0.0.1:1/a", "http://127.0.0.1:1/b"], tmp_path / "a.bin"
        )
        assert not outcome.ok
        assert "下载失败" in outcome.message
        assert not (tmp_path / "a.bin.part").exists()

    def test_github_failure_hint_is_actionable(self):
        """连不上 github.com 时必须给出下一步怎么做，而不是只报 curl 码。"""
        hint = updater._download_failure_hint(["https://github.com/a/b"])
        assert "发布页" in hint and "update_mirror" in hint

    def test_hint_is_empty_for_other_hosts(self):
        assert updater._download_failure_hint(["https://mirror.test/a"]) == ""


class TestReleaseHeadline:
    """更新窗口第一眼要看到「这版改了什么」，而不是一整篇 Markdown。"""

    def test_takes_first_heading(self):
        notes = "## 这版修的是「下载失败」\n\n正文…"
        assert updater.release_headline(notes) == "这版修的是「下载失败」"

    def test_skips_hash_levels_and_blank_lines(self):
        assert updater.release_headline("\n\n### 小标题\n正文") == "小标题"

    def test_falls_back_to_first_text_line(self):
        assert updater.release_headline("没有标题的一段话\n\n## 后面才有") == "没有标题的一段话"

    def test_empty_input(self):
        assert updater.release_headline("") == ""
        assert updater.release_headline(None) == ""
