"""应用图标：结构校验 + 三条「接入点不能静默失效」的守卫测试。

为什么这些要测
--------------
图标有三个接入点，每一个失败时都**不报错、只是不好看**，所以编译通过、
测试全绿都发现不了：

1. ``tools/build_package.py`` 的 ``--icon`` —— 不传就不报错，exe 用默认图标；
2. ``tools/installer.iss`` 的 ``SetupIconFile`` —— 留空不报错，setup.exe 用默认图标；
3. ``run.py`` 的 ``setWindowIcon`` —— **exe 的 PE 资源图标 ≠ 运行时窗口图标**。
   PyInstaller 的窗口版引导程序不会把 exe 的资源图标交给 Qt 主窗口，漏掉这步
   的症状是「exe 属性里有图标，但双击后任务栏是默认图标」，极易查错方向。

所以判据统一为「**产物里真的有图标**」，而不是「代码里出现过 icon 这个词」。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ICON = ROOT / "assets" / "icon.ico"

#: Windows 按用途挑档：任务栏 16、标题栏 24、列表 32、桌面 48、
#: 中等图标 64/128、快捷方式与缩略图 256。少一档就会在某个场景退回默认图标。
EXPECTED_SIZES = (16, 24, 32, 48, 64, 128, 256)


def read_ico(data: bytes) -> list[tuple[int, int, int]]:
    """解析 ICO，返回 [(宽, 高, 字节数)]。"""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0, "ICONDIR 的 reserved 必须是 0"
    assert kind == 1, "ICONDIR 的 type 必须是 1（图标；2 是鼠标指针）"
    entries = []
    for i in range(count):
        fields = struct.unpack("<BBBBHHII", data[6 + 16 * i : 22 + 16 * i])
        entries.append((fields[0] or 256, fields[1] or 256, fields[6]))
    return entries


@pytest.mark.skipif(not ICON.is_file(), reason="尚未生成 assets/icon.ico")
class TestIconFile:
    def test_has_all_required_resolutions(self):
        entries = read_ico(ICON.read_bytes())
        got = sorted(width for width, _h, _n in entries)
        assert got == list(EXPECTED_SIZES), (
            f"缺档：期望 {list(EXPECTED_SIZES)}，实际 {got}"
        )

    def test_256_is_encoded_as_zero(self):
        """ICO 的宽高是 8 位无符号，**256 必须写成 0**（满 8 位就是 0）。

        写成 256 会被截断成 0 之外的错误值，资源管理器挑不出 256 那档。
        """
        data = ICON.read_bytes()
        _r, _k, count = struct.unpack("<HHH", data[:6])
        raw = [struct.unpack("<BBBBHHII", data[6 + 16 * i : 22 + 16 * i])[0]
               for i in range(count)]
        assert 0 in raw, "256 那档的宽必须是 0（8 位无符号的满值）"

    def test_every_frame_is_a_valid_png(self):
        data = ICON.read_bytes()
        for index, (width, height, size) in enumerate(read_ico(data)):
            # 目录项的 offset 在同一结构的第 8 个字段
            offset = struct.unpack(
                "<BBBBHHII", data[6 + 16 * index : 22 + 16 * index]
            )[7]
            assert data[offset : offset + 4] == b"\x89PNG", (
                f"{width}×{height} 那档不是合法 PNG"
            )
            assert size > 0, f"{width}×{height} 那档长度为 0"

    def test_file_is_not_empty_trivial(self):
        assert ICON.stat().st_size > 2048, "图标小得可疑，多半是空画布"


class TestIconWiring:
    """三条守卫：接入点缺了都不报错，必须靠测试盯住。"""

    def test_build_package_passes_icon(self):
        source = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        assert '"--icon"' in source, "打包命令里没有 --icon，exe 会用默认图标"
        assert "icon.ico" in source, "打包命令引用的不是 assets/icon.ico"
        # 运行时窗口图标要用的那份数据文件也必须一起带走
        assert '"--add-data"' in source, (
            "没有 --add-data，产物里就没有 icon.ico，运行时设不了窗口图标"
        )

    def test_installer_sets_setup_icon(self):
        text = (ROOT / "tools" / "installer.iss").read_text(encoding="utf-8")
        line = next(
            (ln for ln in text.splitlines() if ln.startswith("SetupIconFile=")), ""
        )
        value = line.split("=", 1)[1].strip() if line else ""
        assert value, "SetupIconFile 是空的，setup.exe 会用默认图标"
        assert value.endswith("icon.ico"), f"SetupIconFile 指向的不是 ico：{value}"

    def test_runtime_sets_window_icon(self):
        """PE 资源图标不等于窗口图标——必须显式 setWindowIcon。"""
        assert (ROOT / "app" / "gui" / "branding.py").is_file(), (
            "缺少 app/gui/branding.py，运行时窗口图标无处设置"
        )
        from app.gui import branding

        source = (ROOT / "app" / "gui" / "branding.py").read_text(encoding="utf-8")
        assert "setWindowIcon" in source, "branding.py 没有设窗口图标"
        entry = (ROOT / "run.py").read_text(encoding="utf-8")
        assert "apply_app_icon" in entry, (
            "run.py 没有调用 apply_app_icon，图标只在磁盘上、界面上看不到"
        )

    def test_apply_app_icon_reports_missing_file_instead_of_raising(self, monkeypatch):
        """找不到图标不是错误——返回 False，不能挡住启动。"""
        from app.gui import branding

        monkeypatch.setattr(branding, "icon_file", lambda: None)
        assert branding.apply_app_icon(object()) is False
