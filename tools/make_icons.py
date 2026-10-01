# -*- coding: utf-8 -*-
"""渲染应用图标：3 个候选概念 × 7 档尺寸，产出多分辨率 .ico 与对照图。

为什么程序化画，而不是用 AI 生成位图
----------------------------------
在 16px 上 AI 位图基本糊成一团，配色也不会精确对上 ``theme.py`` 的
``PRIMARY``，而且每次调用结果不可复现、不便进版本库。图标是**每一档尺寸都要
精确**的东西，用 QPainter 画反而更可控。

两条硬约束（都在设计阶段定的，别轻易破）
------------------------------------
1. **16px 必须依然可辨**。任务栏就是 16px，实际只装得下 3~4 个形状。
   所以细节随尺寸递减：齿孔、时间轴刻度这些在 32px 以下直接不画。
2. **必须同时在浅色与深色任务栏上成立**。``.ico`` 没有深浅变体（Windows
   桌面不读），所以底板是**不透明**的主色圆角方块——不能做"白图标 + 透明底"，
   那种在浅色任务栏上会直接消失（Chrome / Edge 都是靠实心底板解决的）。

为什么手写 ICO 容器，而不是交给 Qt
---------------------------------
Qt 的 ``QImageWriter`` 虽支持 ico，但历来只写**单张**图；Windows 需要的是
一个文件里塞 7 档（16/24/32/48/64/128/256）。Pillow 没装，而本项目不引新依赖。
ICO 容器本身很简单（6 字节头 + 每档 16 字节目录项 + 图片数据），用 ``struct``
拼即可，各帧直接放 PNG——Vista 之后完全支持这种写法。

用法：
    python tools/make_icons.py            # 三个概念全出
    python tools/make_icons.py --only A   # 只出 A
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "assets"
PREVIEW_DIR = OUT_DIR / "previews"
ICO_PATH = OUT_DIR / "icon.ico"

#: Windows 会按用途挑最接近的一档：任务栏 16、标题栏 24、资源管理器列表 32、
#: 桌面 48、中等图标 64/128、快捷方式与缩略图 256。七档都要有。
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: 与 app/gui/theme.py 的浅色主题主色同源。
PRIMARY = "#3b6cb8"
PRIMARY_DEEP = "#2f5aa0"
GLYPH = "#ffffff"


# ---------------------------------------------------------------------------
# 底板：三个概念共用
# ---------------------------------------------------------------------------


def _draw_plate(painter, size: int) -> None:
    """不透明的主色圆角方块。

    这是"在浅色和深色任务栏上都看得见"的唯一保障——形状边缘由底板决定，
    不依赖下方背景。任何把底板改成透明的想法都会在浅色任务栏上失效。
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

    margin = size * 0.045
    rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    radius = rect.width() * 0.225
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # 一层很淡的上浅下深，模拟受光面；不用渐变是因为 16px 上渐变会变脏
    painter.fillPath(path, QColor(PRIMARY))
    painter.setPen(QPen(QColor(PRIMARY_DEEP), max(1.0, size * 0.02)))
    painter.drawPath(path)


# ---------------------------------------------------------------------------
# 概念 A「断帧」：胶片格 + 一道斜切缝
# ---------------------------------------------------------------------------


def _concept_a(painter, size: int, detail: bool) -> None:
    """视频被切成几段。

    16px 上只保留"横条 + 斜缝"两个形状；齿孔是 32px 才有意义的信息。
    """
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen

    w = size * 0.56
    h = size * 0.40
    x = (size - w) / 2
    y = (size - h) / 2 - size * 0.02
    rect = QRectF(x, y, w, h)

    painter.setPen(QPen(QColor(GLYPH), max(1.2, size * 0.055)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, size * 0.03, size * 0.03)

    if not detail:
        # 小尺寸下只用一道斜缝表达"切断"
        pen = QPen(QColor(GLYPH), max(1.6, size * 0.075))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(x - w * 0.16, y + h + h * 0.10),
            QPointF(x + w * 1.16, y - h * 0.10),
        )
        return

    # 大尺寸：上下各一排齿孔，再叠斜缝
    hole_w = w * 0.11
    hole_h = h * 0.20
    for row_y in (y + h * 0.14, y + h * 0.86 - hole_h):
        offset = x + hole_w * 0.7
        while offset + hole_w < x + w - hole_w * 0.2:
            painter.fillRect(QRectF(offset, row_y, hole_w, hole_h), QColor(GLYPH))
            offset += hole_w * 2.0

    pen = QPen(QColor(GLYPH), max(1.4, size * 0.06))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(
        QPointF(x - w * 0.14, y + h + h * 0.12),
        QPointF(x + w * 1.14, y - h * 0.12),
    )


# ---------------------------------------------------------------------------
# 概念 B「时标」：播放三角 + 一条带刻度的时间轴
# ---------------------------------------------------------------------------


def _concept_b(painter, size: int, detail: bool) -> None:
    """播放 + 定位切点。

    三角与横条的组合在 16px 仍然读得出来；刻度是 48px 以上才加的。
    """
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPen, QPolygonF

    tri_h = size * 0.34
    tri_w = tri_h * 0.92
    cx = size / 2
    top = size * 0.30
    triangle = QPolygonF(
        [
            QPointF(cx - tri_w / 2, top),
            QPointF(cx - tri_w / 2, top + tri_h),
            QPointF(cx + tri_w / 2, top + tri_h / 2),
        ]
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(GLYPH))
    painter.drawPolygon(triangle)

    bar_y = size * 0.76
    bar_w = size * 0.56
    pen = QPen(QColor(GLYPH), max(1.4, size * 0.06))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(
        QPointF(cx - bar_w / 2, bar_y), QPointF(cx + bar_w / 2, bar_y)
    )

    if not detail:
        return
    tick = QPen(QColor(GLYPH), max(1.0, size * 0.035))
    for ratio in (0.0, 0.5, 1.0):
        tx = cx - bar_w / 2 + bar_w * ratio
        painter.setPen(tick)
        painter.drawLine(
            QPointF(tx, bar_y - size * 0.05), QPointF(tx, bar_y + size * 0.05)
        )


# ---------------------------------------------------------------------------
# 概念 C「分集」：三条竖块，中间那条高亮
# ---------------------------------------------------------------------------


def _concept_c(painter, size: int, detail: bool) -> None:
    """分成 N 集。

    踩过的坑：最初在三条竖块之间画了 ``PRIMARY_DEEP`` 的分隔线想强调"切开的
    边界"，结果在 16~32px 上那两��线糊成一片，和底板同色系，远看像渲染故障
    而不是分隔。小尺寸下**多一笔就多一份噪声**——所以这里只靠"中间那条更高"
    来表达边界，线条一律不加。

    竖块要够宽：16px 上三条 0.135×size 的窄条只有 2px，读不出条数。
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor

    gap = size * 0.07
    block_w = size * 0.20
    outer_h = size * 0.30
    mid_h = size * 0.46
    total_h = mid_h + gap
    x = (size - (block_w * 3 + gap * 2)) / 2
    top = (size - total_h) / 2

    painter.setPen(Qt.PenStyle.NoPen)
    # 外侧两条略淡、中间一条纯白：即便小到看不出高度差，靠明暗也还能区分
    painter.setBrush(QColor(GLYPH))
    painter.drawRoundedRect(
        QRectF(x, top + (mid_h - outer_h) / 2, block_w, outer_h),
        block_w * 0.34, block_w * 0.34,
    )
    painter.drawRoundedRect(
        QRectF(x + block_w + gap, top, block_w, mid_h),
        block_w * 0.34, block_w * 0.34,
    )
    painter.drawRoundedRect(
        QRectF(x + (block_w + gap) * 2, top + (mid_h - outer_h) / 2, block_w, outer_h),
        block_w * 0.34, block_w * 0.34,
    )


CONCEPTS = {
    "A-断帧": _concept_a,
    "B-时标": _concept_b,
    "C-分集": _concept_c,
}


# ---------------------------------------------------------------------------
# 渲染与打包
# ---------------------------------------------------------------------------


def render(concept: str, size: int):
    """画一档尺寸，返回 QImage。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        _draw_plate(painter, size)
        CONCEPTS[concept](painter, size, detail=size >= 32)
    finally:
        painter.end()
    return image


def png_bytes(image) -> bytes:
    """QImage → PNG 字节（走内存缓冲，不落临时文件）。"""
    from PySide6.QtCore import QBuffer, QByteArray

    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("PNG 编码失败")
    return bytes(QByteArray(buffer.data()))


def build_ico(frames: dict[int, bytes]) -> bytes:
    """把若干档 PNG 拼成一个多分辨率 ICO。

    结构：6 字节 ICONDIR 头 + 每档 16 字节目录项 + 依次排列的图片数据。
    目录项里的宽高是 8 位无符号，**256 必须写成 0**（满 8 位就是 0）。
    """
    sizes = sorted(frames)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    blobs = b""
    for size in sizes:
        data = frames[size]
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        blobs += data
        offset += len(data)
    return header + entries + blobs


def write_contact_sheet(only: str | None = None) -> Path:
    """3 概念 × 7 尺寸的对照图，**上下分成浅色与深色两块**。

    这是选型的唯一依据：图标在真实任务栏上的观感没法用断言保证，必须人眼比对。
    上下两块的意义就是当场验证"实心底板在两种背景上都还看得见"。
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    # 沙箱 venv 里 Qt 找不到系统字体目录（QFontDatabase 会告警），字体拿不到就
    # 退化成默认字号。对照图的重点是"图标本身在小尺寸下什么样"，标注次要。
    label_font = QFont()
    label_font.setPointSize(9)

    names = [c for c in CONCEPTS if only is None or c == only]
    cell = 96
    pad = 24
    label_h = 30
    row_h = cell + 10
    # 高度必须按**概念条数**算：漏乘 rows 会让三行叠在一行高度里，
    # 后面几行直接画到图外（踩过一次）。
    half_h = pad + label_h + len(names) * row_h + pad

    width = pad * 2 + label_h + cell * len(SIZES)
    height = half_h * 2
    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    painter = QPainter(sheet)
    try:
        for half, bg in enumerate(("#f4f5f9", "#20232a")):
            painter.fillRect(0, half * half_h, width, half_h, QColor(bg))
            fg = QColor("#1a1d23" if half == 0 else "#e8eaf0")
            painter.setPen(QColor(fg))
            painter.setFont(label_font)

            base_y = half * half_h + pad
            painter.drawText(QRectF(pad, base_y, label_h - 6, 20), Qt.AlignmentFlag.AlignLeft, "size")
            for column, size in enumerate(SIZES):
                x = pad + label_h + column * cell
                painter.drawText(
                    QRectF(x, base_y, cell, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{size}",
                )

            for row, name in enumerate(names):
                y = base_y + label_h + row * row_h
                # 只标 A/B/C：沙箱 venv 里 Qt 找不到 CJK 字体，中文会画成方框，
                # 而这张图的重点是"图标在小尺寸下什么样"，标号足够辨认。
                painter.setPen(QColor(fg))
                painter.drawText(
                    QRectF(pad, y, label_h - 6, cell),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    name.split("-")[0],
                )
                for column, size in enumerate(SIZES):
                    x = pad + label_h + column * cell
                    tile = render(name, size)
                    # 小尺寸按**原样 1:1** 贴，不放大——放大后就看不出真实观感了
                    if size <= cell:
                        painter.drawImage(x + (cell - size) // 2, y + (cell - size) // 2, tile)
                    else:
                        target = QRectF(
                            x + (cell - size / 2) / 2,
                            y + (cell - size / 2) / 2,
                            size / 2,
                            size / 2,
                        )
                        painter.drawImage(target, tile)
    finally:
        painter.end()

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = PREVIEW_DIR / "contact-sheet.png"
    sheet.save(str(path), "PNG")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染应用图标候选")
    parser.add_argument(
        "--only", choices=sorted(CONCEPTS), help="只渲染指定概念（默认三个都出）"
    )
    args = parser.parse_args()

    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    names = [c for c in CONCEPTS if args.only is None or c == args.only]
    written: dict[str, bytes] = {}
    for name in names:
        frames = {}
        for size in SIZES:
            image = render(name, size)
            data = png_bytes(image)
            frames[size] = data
            (PREVIEW_DIR / f"{name}-{size}.png").write_bytes(data)
        written[name] = build_ico(frames)
        print(f"  {name}: {len(frames)} 档，最大 {max(frames)}")

    sheet = write_contact_sheet(args.only)
    print(f"对照图：{sheet}")

    if args.only is None:
        # 三个概念都出时，把首个概念的 .ico 落到 assets/icon.ico 供接入使用
        ICO_PATH.write_bytes(written[names[0]])
        print(f"图标文件：{ICO_PATH}（默认取首个概念；选定后用 --only 重出）")
    else:
        # 选定后同样落到 assets/icon.ico —— 接入打包与安装程序只认这一个路径，
        # 单独存一份 icon-B.ico 反而多一个要同步的文件。
        ICO_PATH.write_bytes(written[args.only])
        print(f"图标文件：{ICO_PATH}（概念 {args.only}）")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
