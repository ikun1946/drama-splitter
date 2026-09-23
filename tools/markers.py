"""合成测试素材的标记规范。

方案 §20.2 要求准备"带帧号和同步声音标记的合成素材"来验证技术精度。
本模块定义帧号标记与声音标记的编码方式，供素材生成器与校验器共用。

为什么不用烧录数字 + OCR
------------------------
OCR 依赖字体与渲染，误差不可控且慢。这里改用**二进制条码**：每帧顶部画一组
等宽黑白块，MSB 在左，直接编码帧序号。解码时只采样每个块的**中心像素**并做
阈值判断。纯黑（0）与纯白（255）在 CRF 18 的 8-bit yuv420p 编码下，块心误差
通常只有几个灰度级，远小于 127 的阈值距离，因此帧身份可以被精确还原。

为什么不用帧内容哈希
--------------------
默认导出是**有损重编码**（§14.1），像素哈希必然不等。条码方案对压缩鲁棒，
因此可以同时用于"有损导出后帧身份是否正确"的校验。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FRAME_WIDTH",
    "FRAME_HEIGHT",
    "FPS",
    "FRAME_BITS",
    "BLOCK_SIZE",
    "BLOCK_GAP",
    "BLOCK_MARGIN_X",
    "BLOCK_MARGIN_Y",
    "MAX_FRAME_INDEX",
    "MARKER_LUMA_BLACK",
    "MARKER_LUMA_WHITE",
    "BACKGROUND_GRAY",
    "BEEP_INTERVAL_FRAMES",
    "BEEP_DURATION_SECONDS",
    "BEEP_FREQUENCY_HZ",
    "BEEP_AMPLITUDE",
    "SAMPLE_RATE",
    "marker_block_positions",
    "marker_bbox",
    "encode_marker_bits",
    "decode_marker_bits",
    "beep_start_seconds",
]

# ---- 画面参数 -------------------------------------------------------------

FRAME_WIDTH = 320
FRAME_HEIGHT = 180
FPS = 25

# 14 位可表示 0–16383 帧，25fps 下约 655 秒，足够测试用
FRAME_BITS = 14
BLOCK_SIZE = 16
BLOCK_GAP = 2
BLOCK_MARGIN_X = 12
BLOCK_MARGIN_Y = 12

MAX_FRAME_INDEX = (1 << FRAME_BITS) - 1

MARKER_LUMA_BLACK = 0
MARKER_LUMA_WHITE = 255
BACKGROUND_GRAY = 128
# 每 100 帧背景闪白一次，便于人工目视检查跳帧
FLASH_INTERVAL_FRAMES = 100

# ---- 声音参数 -------------------------------------------------------------

SAMPLE_RATE = 48000
BEEP_FREQUENCY_HZ = 1000.0
BEEP_DURATION_SECONDS = 0.100
BEEP_AMPLITUDE = 0.8
# 每 125 帧（25fps 下恰好 5.000 秒）在帧起点放一个短促提示音，
# 用于验证切点附近的音画同步误差是否在一个视频帧以内（§14.5 音频层）。
BEEP_INTERVAL_FRAMES = 125


def marker_block_positions() -> list[tuple[int, int]]:
    """返回每个条码块左上角的 (x, y)，按 MSB → LSB 顺序。"""
    positions: list[tuple[int, int]] = []
    for index in range(FRAME_BITS):
        x = BLOCK_MARGIN_X + index * (BLOCK_SIZE + BLOCK_GAP)
        positions.append((x, BLOCK_MARGIN_Y))
    return positions


def marker_bbox() -> tuple[int, int, int, int]:
    """条码包围盒 (x, y, w, h)。

    校验时先按此区域裁剪，把每帧从几十万字节压到几千字节，
    使"逐帧解码全片"成为可接受的开销。
    """
    width = (FRAME_BITS - 1) * (BLOCK_SIZE + BLOCK_GAP) + BLOCK_SIZE
    return BLOCK_MARGIN_X, BLOCK_MARGIN_Y, width, BLOCK_SIZE


def encode_marker_bits(frame_index: int) -> list[int]:
    """帧号 → 比特列表（MSB 在前）。"""
    if not 0 <= frame_index <= MAX_FRAME_INDEX:
        raise ValueError(f"帧号 {frame_index} 超出条码可表示范围 0–{MAX_FRAME_INDEX}")
    return [(frame_index >> (FRAME_BITS - 1 - i)) & 1 for i in range(FRAME_BITS)]


def decode_marker_bits(bits: list[int]) -> int:
    """比特列表（MSB 在前）→ 帧号。"""
    if len(bits) != FRAME_BITS:
        raise ValueError(f"需要 {FRAME_BITS} 个比特，实际收到 {len(bits)}")
    value = 0
    for bit in bits:
        value = (value << 1) | (1 if bit else 0)
    return value


def beep_start_seconds(beep_index: int) -> float:
    """第 beep_index 个提示音的起始时刻（秒）。"""
    return beep_index * (BEEP_INTERVAL_FRAMES / FPS)


@dataclass(frozen=True)
class MarkerLayout:
    """条码块的像素布局，供解码器采样。"""

    positions: tuple[tuple[int, int], ...]
    size: int

    @classmethod
    def default(cls) -> "MarkerLayout":
        return cls(tuple(marker_block_positions()), BLOCK_SIZE)

    def center(self, index: int) -> tuple[int, int]:
        x, y = self.positions[index]
        return x + self.size // 2, y + self.size // 2
