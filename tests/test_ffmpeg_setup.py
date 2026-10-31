"""FFmpeg 自动配置：解压、校验、下载切换与查找顺序。

不联网：下载函数与远端体积都被替换；zip 用标准库现场构造。真实网络只在
``test_ffmpeg_setup_live`` 这类手工端到端里跑，不进单元测试。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core import ffmpeg_setup as fs
from app.core import ffmpeg as ff
from app.core.ffmpeg import FfmpegBinaries, FfmpegNotFound

def _make_zip(path: Path, *, include_ffprobe: bool = True, include_ffplay: bool = True) -> None:
    prefix = "ffmpeg-8.0-essentials_build/bin/"
    names = [prefix + "ffmpeg.exe"]
    if include_ffprobe:
        names.append(prefix + "ffprobe.exe")
    if include_ffplay:
        names.append(prefix + "ffplay.exe")
    names += [
        "ffmpeg-8.0-essentials_build/README.txt",
        "../escape.exe",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"dummy")

@pytest.fixture
def fake_config(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    monkeypatch.setattr("app.core.app_config.config_path", lambda: target)
    return target

@pytest.fixture
def fake_stubs(monkeypatch):
    def locate(explicit_dir=None):
        base = Path(explicit_dir) if explicit_dir else Path("C:/fake/bin")
        return FfmpegBinaries(
            ffmpeg=base / "ffmpeg.exe",
            ffprobe=base / "ffprobe.exe",
            version="test-build",
            encoders=frozenset({"libx264", "aac"}),
        )

    monkeypatch.setattr(fs, "locate_ffmpeg", locate)
    monkeypatch.setattr(fs, "check_encoders", lambda binaries: binaries.encoders)
    monkeypatch.setattr(fs, "free_space_bytes", lambda root: 10 * 1024**3)
    monkeypatch.setattr(fs, "remote_size", lambda url: 100)
    return locate

class TestExtract:
    def test_extracts_only_the_three_binaries(self, tmp_path):
        archive = tmp_path / "ffmpeg.zip"
        _make_zip(archive)
        out = tmp_path / "out"
        extracted = fs.extract_ffmpeg_zip(archive, out)
        assert sorted(extracted) == ["ffmpeg.exe", "ffplay.exe", "ffprobe.exe"]
        for name in extracted:
            assert (out / name).is_file()
        assert not (out / "README.txt").exists()
        assert not (tmp_path / "escape.exe").exists()

    def test_missing_ffprobe_raises(self, tmp_path):
        archive = tmp_path / "ffmpeg.zip"
        _make_zip(archive, include_ffprobe=False)
        with pytest.raises(ValueError, match="ffprobe"):
            fs.extract_ffmpeg_zip(archive, tmp_path / "out")

    def test_ffplay_is_optional(self, tmp_path):
        archive = tmp_path / "ffmpeg.zip"
        _make_zip(archive, include_ffplay=False)
        out = tmp_path / "out"
        extracted = fs.extract_ffmpeg_zip(archive, out)
        assert "ffplay.exe" not in extracted
        assert (out / "ffmpeg.exe").is_file()

class TestValidate:
    def test_missing_files(self, tmp_path):
        ok, message = fs.validate_ffmpeg_dir(tmp_path)
        assert not ok
        assert "ffmpeg.exe" in message and "ffprobe.exe" in message

    def test_accepts_complete_build(self, tmp_path, fake_stubs):
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (tmp_path / name).write_bytes(b"x")
        ok, message = fs.validate_ffmpeg_dir(tmp_path)
        assert ok
        assert "test-build" in message

class TestInstall:
    def test_success_writes_managed_bin_and_cleans_zip(
        self, tmp_path, monkeypatch, fake_stubs, fake_config
    ):
        monkeypatch.setattr(fs, "app_dir", lambda: tmp_path)

        def download(url, part, total, *, on_progress=None, cancel_check=None):
            _make_zip(part)
            if on_progress:
                on_progress(100, 100)
            return True, False, "", 100

        monkeypatch.setattr(fs, "_download_one", download)
        result = fs.install_ffmpeg()
        assert result.ok
        assert result.directory == tmp_path / "ffmpeg" / "bin"
        assert (result.directory / "ffmpeg.exe").is_file()
        assert not (tmp_path / "ffmpeg" / "download").exists()

    def test_falls_back_to_second_url(
        self, tmp_path, monkeypatch, fake_stubs, fake_config
    ):
        monkeypatch.setattr(fs, "app_dir", lambda: tmp_path)
        calls = []

        def download(url, part, total, *, on_progress=None, cancel_check=None):
            calls.append(url)
            if len(calls) == 1:
                return False, False, "网络错误", 0
            _make_zip(part)
            return True, False, "", 100

        monkeypatch.setattr(fs, "_download_one", download)
        result = fs.install_ffmpeg()
        assert result.ok
        assert len(calls) == 2

    def test_cancel_reports_and_keeps_no_bin(
        self, tmp_path, monkeypatch, fake_stubs, fake_config
    ):
        monkeypatch.setattr(fs, "app_dir", lambda: tmp_path)
        monkeypatch.setattr(
            fs,
            "_download_one",
            lambda *a, **k: (False, True, "已取消下载", 10),
        )
        result = fs.install_ffmpeg()
        assert result.cancelled
        assert not fs.managed_bin().exists()

    def test_failed_validation_keeps_existing_bin(
        self, tmp_path, monkeypatch, fake_stubs, fake_config
    ):
        monkeypatch.setattr(fs, "app_dir", lambda: tmp_path)
        existing = tmp_path / "ffmpeg" / "bin"
        existing.mkdir(parents=True)
        (existing / "ffmpeg.exe").write_bytes(b"old")

        def download(url, part, total, *, on_progress=None, cancel_check=None):
            _make_zip(part)
            return True, False, "", 100

        def broken_locate(explicit_dir=None):
            raise FfmpegNotFound("校验失败")

        monkeypatch.setattr(fs, "_download_one", download)
        monkeypatch.setattr(fs, "locate_ffmpeg", broken_locate)
        result = fs.install_ffmpeg()
        assert not result.ok
        assert (existing / "ffmpeg.exe").read_bytes() == b"old"

class TestLookup:
    def test_managed_dir_wins_over_path(self, tmp_path, monkeypatch):
        managed = tmp_path / "managed"
        managed.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (managed / name).write_bytes(b"x")
        path_dir = tmp_path / "path"
        path_dir.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (path_dir / name).write_bytes(b"x")

        monkeypatch.delenv("DRAMA_FFMPEG_DIR", raising=False)
        monkeypatch.setattr(ff, "_configured_dir", lambda: None)
        monkeypatch.setattr(ff, "_managed_bin", lambda: managed)
        monkeypatch.setattr(ff, "_which", lambda name: path_dir / f"{name}.exe")
        monkeypatch.setattr(ff, "_finalize", lambda a, b: FfmpegBinaries(a, b))
        found = ff.locate_ffmpeg()
        assert found.ffmpeg == managed / "ffmpeg.exe"

    def test_search_fallback_accepts_flat_layout(self, tmp_path, monkeypatch):
        (tmp_path / "ffmpeg.exe").write_bytes(b"x")
        monkeypatch.setattr(ff, "_FALLBACK_DIRS", (tmp_path,))
        assert ff._search_fallback("ffmpeg") == tmp_path / "ffmpeg.exe"

    def test_config_ffmpeg_dir_roundtrip(self, tmp_path, monkeypatch):
        from app.core.app_config import AppConfig

        monkeypatch.setattr("app.core.app_config.config_path", lambda: tmp_path / "config.json")
        cfg = AppConfig()
        cfg.ffmpeg_dir = str(tmp_path)
        cfg.save()
        assert AppConfig.load().ffmpeg_dir == str(tmp_path)
