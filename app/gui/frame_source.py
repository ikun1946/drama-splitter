"""帧级预览服务：用 FFmpeg 解码真实帧，不做"播放器进度条近似"。

设计依据（§13.3）：
> 预览播放器的拖动定位不一定逐帧准确；逐帧审核应使用可验证的解码帧，
> 不能只凭播放器进度条判断。

因此这里不走 Qt 媒体播放器，而是直接把 FFmpeg 解出的像素包成 QImage：
- 单帧取图：按精确时间定位解一帧，用于逐帧步进与切点检视；
- 连续播放：后台流式解码 + 定时器按帧率出图，保证"所见帧 = 该时刻应有的帧"。

另外，不使用 QtMultimedia 还有一个现实原因：它位于 160MB+ 的 PySide6-Addons 中，
而本项目只需要 Essentials。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage

__all__ = ["FrameDecoder", "StreamPlayer", "DecodeError"]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DecodeError(RuntimeError):
    """解码失败。"""


def _ffmpeg_path() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app.core.ffmpeg import locate_ffmpeg

    return str(locate_ffmpeg().ffmpeg)


class FrameDecoder:
    """按精确时间取单帧。用于逐帧步进与切点检视。"""

    def __init__(self, path: str | Path | None = None, max_width: int | None = 1280) -> None:
        self._path = Path(path) if path else None
        self._max_width = max_width
        self._ffmpeg = _ffmpeg_path()
        self._size: tuple[int, int] = (0, 0)

    def set_source(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None
        self._size = (0, 0)

    @property
    def source(self) -> Path | None:
        return self._path

    def output_size(self) -> tuple[int, int]:
        """实际输出尺寸：等比缩放到 max_width，高度取偶数。"""
        if self._size != (0, 0):
            return self._size
        if self._path is None:
            return (0, 0)
        root = Path(__file__).resolve().parent.parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.core.ffmpeg import locate_ffmpeg
        from app.core.probe import probe_media

        info = probe_media(locate_ffmpeg(), self._path, detect_vfr=False)
        if info.video is None:
            return (0, 0)
        width = info.video.width if not self._max_width else min(self._max_width, info.video.width)
        height = max(2, int(round(info.video.height * width / info.video.width)))
        if height % 2:
            height += 1
        self._size = (width, height)
        return self._size

    def frame_at(self, seconds: Fraction | float) -> QImage | None:
        """解出该时刻**正在显示**的那一帧。

        使用 `-ss` 输入定位 + 重新编码，并显式 `-frames:v 1`，
        保证"取到哪一帧"完全确定，而不是由播放器进度条决定。
        """
        if self._path is None:
            raise DecodeError("尚未选择源文件")
        width, height = self.output_size()
        if (width, height) == (0, 0):
            raise DecodeError("无法确定画面尺寸")

        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(seconds)):.6f}",
            "-i",
            str(self._path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        proc = subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            raise DecodeError((proc.stderr or b"").decode("utf-8", "replace").strip()[-400:])
        raw = proc.stdout or b""
        needed = width * height * 3
        if len(raw) < needed:
            return None
        return QImage(raw[:needed], width, height, width * 3, QImage.Format.Format_RGB888).copy()

    def close(self) -> None:
        self._path = None
        self._size = (0, 0)


class StreamPlayer(QObject):
    """连续播放：后台线程流式解码，定时器按帧率出图。

    用法：
        player.set_range(start, end)
        player.play()
        player.pause()
        player.seek(seconds)
    """

    frame_ready = Signal(QImage)
    position_changed = Signal(float)
    playback_finished = Signal()
    error = Signal(str)

    MAX_QUEUE = 48  # 预解码上限，避免长片段一次性吃掉内存

    def __init__(self, source: str | Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ffmpeg = _ffmpeg_path()
        self._source: Path | None = Path(source) if source else None
        self._max_width = 960
        self._fps = Fraction(25)

        self._start = 0.0
        self._end: float | None = None
        self._position = 0.0

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._queue: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._frame_size = (0, 0)
        self._last_frame: bytes | None = None

        self._timer = QTimer(self)
        # 帧率定时必须用精确定时器，否则播放节奏会随系统负载漂移
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)
        self._playing = False

    # ---- 配置 ----------------------------------------------------------

    def set_source(self, path: str | Path | None) -> None:
        self.stop()
        self._source = Path(path) if path else None

    def set_max_width(self, width: int) -> None:
        self._max_width = max(160, width)

    def set_fps(self, fps: Fraction) -> None:
        self._fps = fps if fps and fps > 0 else Fraction(25)
        if self._playing:
            self._timer.setInterval(self._interval_ms())

    def set_range(self, start_seconds: float, end_seconds: float | None = None) -> None:
        self._start = max(0.0, start_seconds)
        self._end = end_seconds
        self._position = self._start

    @property
    def position(self) -> float:
        return self._position

    @property
    def is_playing(self) -> bool:
        return self._playing

    # ---- 播放控制 ------------------------------------------------------

    def play(self) -> None:
        if self._source is None:
            self.error.emit("尚未选择源文件")
            return
        if self._playing:
            return
        self._playing = True
        self._start_reader(self._position)
        self._timer.start(self._interval_ms())

    def pause(self) -> None:
        self._playing = False
        self._timer.stop()
        self._stop_reader()

    def stop(self) -> None:
        self.pause()
        self._position = self._start
        self._last_frame = None

    def seek(self, seconds: float) -> None:
        was_playing = self._playing
        self.pause()
        self._position = max(self._start, seconds)
        if self._end is not None:
            self._position = min(self._position, self._end)
        # 立即显示目标帧，避免拖动后画面滞后
        frame = self._grab_single(self._position)
        if frame is not None:
            self.frame_ready.emit(frame)
        self.position_changed.emit(self._position)
        if was_playing:
            self.play()

    # ---- 内部 ----------------------------------------------------------

    def _interval_ms(self) -> int:
        return max(8, int(round(1000.0 / float(self._fps))))

    def _frame_size(self) -> tuple[int, int]:
        if self._frame_size != (0, 0):
            return self._frame_size
        if self._source is None:
            return (0, 0)
        root = Path(__file__).resolve().parent.parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.core.ffmpeg import locate_ffmpeg
        from app.core.probe import probe_media

        info = probe_media(locate_ffmpeg(), self._source, detect_vfr=False)
        if info.video is None:
            return (0, 0)
        width = min(self._max_width, info.video.width)
        height = max(2, int(round(info.video.height * width / info.video.width)))
        if height % 2:
            height += 1
        self._frame_size = (width, height)
        return self._frame_size

    def _build_stream_command(self, start_seconds: float) -> list[str]:
        width, height = self._frame_size()
        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start_seconds:.6f}",
            "-i",
            str(self._source),
            "-an",
            "-fps_mode",
            "passthrough",
            "-vf",
            f"scale={width}:{height}",
        ]
        if self._end is not None:
            span = max(0.0, self._end - start_seconds)
            args += ["-t", f"{span:.6f}"]
        args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return args

    def _start_reader(self, start_seconds: float) -> None:
        self._stop_reader()
        if self._source is None:
            return
        size = self._frame_size()
        if size == (0, 0):
            self.error.emit("无法确定画面尺寸")
            return

        self._stop_flag.clear()
        with self._lock:
            self._queue.clear()
        self._proc = subprocess.Popen(
            self._build_stream_command(start_seconds),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        self._reader = threading.Thread(target=self._read_loop, args=(size,), daemon=True)
        self._reader.start()

    def _read_loop(self, size: tuple[int, int]) -> None:
        frame_bytes = size[0] * size[1] * 3
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while not self._stop_flag.is_set():
            with self._lock:
                if len(self._queue) >= self.MAX_QUEUE:
                    time.sleep(0.005)
                    continue
            chunk = proc.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                break
            with self._lock:
                self._queue.append(chunk)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _stop_reader(self) -> None:
        self._stop_flag.set()
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        reader, self._reader = self._reader, None
        if reader and reader.is_alive():
            reader.join(timeout=1.0)

    def _on_tick(self) -> None:
        size = self._frame_size()
        with self._lock:
            chunk = self._queue.popleft() if self._queue else None

        if chunk is None:
            # 预解码队列空：若解码线程已结束，说明本段播完
            if self._reader is not None and not self._reader.is_alive():
                self._playing = False
                self._timer.stop()
                self.playback_finished.emit()
                return
            # 否则短暂欠载，重复上一帧以维持时间感（不跳帧）
            chunk = self._last_frame
            if chunk is None:
                return
        else:
            self._last_frame = chunk

        image = QImage(
            chunk,
            size[0],
            size[1],
            size[0] * 3,
            QImage.Format.Format_RGB888,
        ).copy()

        frame_duration = 1.0 / float(self._fps)
        self._position += frame_duration
        if self._end is not None and self._position > self._end + frame_duration:
            self._playing = False
            self._timer.stop()
            self._stop_reader()
            self.playback_finished.emit()
            return

        self.frame_ready.emit(image)
        self.position_changed.emit(self._position)

    def _grab_single(self, seconds: float) -> QImage | None:
        """同步取单帧，用于 seek 后立刻出图。"""
        size = self._frame_size()
        if size == (0, 0) or self._source is None:
            return None
        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, seconds):.6f}",
            "-i",
            str(self._source),
            "-frames:v",
            "1",
            "-vf",
            f"scale={size[0]}:{size[1]}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        proc = subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW)
        raw = proc.stdout or b""
        needed = size[0] * size[1] * 3
        if len(raw) < needed:
            return None
        return QImage(raw[:needed], size[0], size[1], size[0] * 3, QImage.Format.Format_RGB888).copy()
