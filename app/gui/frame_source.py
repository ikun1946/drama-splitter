"""帧级预览服务：用 FFmpeg 解码真实帧，不做"播放器进度条近似"。

设计依据（§13.3）：
> 预览播放器的拖动定位不一定逐帧准确；逐帧审核应使用可验证的解码帧，
> 不能只凭播放器进度条判断。

因此这里不走 Qt 媒体播放器，而是直接把 FFmpeg 解出的像素包成 QImage：
- 单帧取图：按精确时间定位解一帧，用于逐帧步进与切点检视；
- 连续播放：后台流式解码 + 定时器按帧率出图，保证"所见帧 = 该时刻应有的帧"。

另外，不使用 QtMultimedia 还有一个现实原因：它位于 160MB+ 的 PySide6-Addons 中，
而本项目只需要 Essentials。因此**预览的声音**也不走 Qt：播放时并行起一个只放
声音的 ``ffplay``（同一文件、同一起点，与画面一起启停），见 ``StreamPlayer``
的音频通道一节。
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

__all__ = ["FrameDecoder", "FrameScrubber", "StreamPlayer", "DecodeError"]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DecodeError(RuntimeError):
    """解码失败。"""


def _binaries():
    """定位 FFmpeg 工具链（ffmpeg / ffprobe / ffplay 一起返回，只探测一次）。"""
    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app.core.ffmpeg import locate_ffmpeg

    return locate_ffmpeg()


class FrameDecoder:
    """按精确时间取单帧。用于逐帧步进与切点检视。"""

    def __init__(self, path: str | Path | None = None, max_width: int | None = 1280) -> None:
        self._path = Path(path) if path else None
        self._max_width = max_width
        self._ffmpeg = str(_binaries().ffmpeg)
        self._size: tuple[int, int] = (0, 0)

    def set_source(self, path: str | Path | None,
                   size: tuple[int, int] | None = None) -> None:
        """设定源文件；``size`` 是**已知的源分辨率**（宽, 高），可选。

        传了 size 就直接算出输出尺寸。否则第一次取帧时会在调用线程里同步跑
        一次 ffprobe（``output_size`` 里），大文件/慢盘上表现为"点一下卡一下"——
        而调用方通常已经从媒体探测里拿到了分辨率。
        """
        self._path = Path(path) if path else None
        self._size = self._scaled(size) if size else (0, 0)

    def _scaled(self, size: tuple[int, int]) -> tuple[int, int]:
        width, height = size
        if width <= 0 or height <= 0:
            return (0, 0)
        out_width = min(self._max_width, width) if self._max_width else width
        out_height = max(2, int(round(height * out_width / width)))
        if out_height % 2:
            out_height += 1
        return (out_width, out_height)

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


class FrameScrubber(QObject):
    """异步单帧取帧：拖动/步进时不阻塞 GUI 线程。

    为什么需要它
    ------------
    ``FrameDecoder.frame_at()`` 每次都要新起一个 ffmpeg 进程并定位解码一个
    GOP。实测 1080p 上约 168 ms，其中进程启动只占 ~28 ms，其余全花在定位与
    解码。拖动时间轴时 ``TimelineRuler`` 以 30 Hz 发 seek 信号，若每次都同步
    取帧，GUI 线程每秒只能处理 6 次，事件队列持续积压——表现就是"一拖就卡死"。

    这里把取帧挪到后台线程，并**只保留最新请求**：拖动过程中间那些位置用户
    根本来不及看，解出来也是白解。帧带秒数一起发出，接收方据此丢弃过期帧
    （解码期间来了新请求时，旧帧不发）。

    精度取向与 ``StreamPlayer`` 一致：仍然用精确 ``-ss`` 定位，保证"看到的就是
    该时刻应有的帧"。松手后调用方走同步的 ``FrameDecoder.frame_at()`` 落定，
    二者用的是同一条命令，不会出现"拖动到 A、松手跳到 B"的偏差。
    """

    frame_ready = Signal(QImage, float)   # (帧, 该帧对应的秒数)

    IDLE_POLL = 0.01   # 线程空转轮询间隔（没活干时）

    def __init__(self, decoder: "FrameDecoder", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._decoder = decoder
        self._lock = threading.Lock()
        self._pending: float | None = None
        self._pending_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def request(self, seconds: float) -> None:
        """请求解某一秒的帧。高频调用安全——中间请求会被合并掉。"""
        with self._lock:
            self._pending = float(seconds)
            self._pending_id += 1
        self._ensure_thread()

    def cancel(self) -> None:
        """丢弃尚未开始的待解请求（松手时调用，避免旧帧盖住精确定位的结果）。"""
        with self._lock:
            self._pending = None
            self._pending_id += 1

    def stop(self) -> None:
        """停掉后台线程（换源、关闭时调用）。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        with self._lock:
            self._pending = None

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                target = self._pending
                req_id = self._pending_id
                self._pending = None
            if target is None:
                time.sleep(self.IDLE_POLL)
                continue
            try:
                image = self._decoder.frame_at(target)
            except Exception:  # noqa: BLE001 - 后台线程里任何异常都不该杀死循环
                image = None
            if self._stop.is_set():
                return
            # 解码期间又来了新请求 → 这一帧已经过期，丢掉不画，免得画一下又跳走
            with self._lock:
                superseded = self._pending_id != req_id
            if image is not None and not superseded:
                self.frame_ready.emit(image, target)


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
    playing_changed = Signal(bool)   # 播放↔暂停（含自然播完）
    error = Signal(str)

    MAX_QUEUE = 48  # 预解码上限，避免长片段一次性吃掉内存

    def __init__(self, source: str | Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        binaries = _binaries()
        self._ffmpeg = str(binaries.ffmpeg)
        self._ffplay = binaries.ffplay      # None = 这套 FFmpeg 没带 ffplay，只能无声
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
        # 输出尺寸缓存。注意名字不要叫 _frame_size：那会遮蔽同名的 _frame_size()
        # 方法，调用点直接 TypeError（本文件最早版本就这样，播放按钮因此一直
        # 静默失效——异常在 Qt 槽里被打印后吞掉，界面上只表现为"点了没反应"）。
        self._cached_size = (0, 0)
        self._last_frame: bytes | None = None

        # 音频通道（预览出声）：与画面并行的一个只放声音的 ffplay 进程
        self._audio: subprocess.Popen | None = None
        self._audio_track: int | None = None
        self._audio_enabled = True

        # 画面时钟：跟墙钟走，不跟"送达的帧数"走。
        #
        # 早先的写法是每个 tick ``self._position += 1/fps``。那等于把画面时间
        # 绑在"解码并渲染成功的帧数"上：一旦某帧渲染超过一个帧时长，Qt 定时器
        # 会把错过的 tick 合并掉，而位置只前进一帧——**每丢一帧就永久落后 40ms，
        # 再也追不回来**。音频走的是 ffplay 的实时钟（准的），两者之差就是累计
        # 丢帧量，实测每分钟漂 ~3 秒，正是"越放越不同步"。
        #
        # 改成 ``clock_origin + (now - clock_start)``：位置恒等于真实经过的时间，
        # 解码跟不上时就**丢帧追赶**（弃掉过期的中间帧），宁可掉帧也不漂移——
        # 掉帧只影响流畅度，漂移则会毁掉音画同步。
        self._clock_origin = 0.0
        self._clock_start: float | None = None   # None = 本段尚未起表（等第一帧到手）
        # 已经**显示过**的帧序号（相对本段起点）。定时器是细粒度轮询，一帧时长内
        # 会触发好几次；这个值是"这一 tick 是否该换新帧"的判据。
        self._last_index = -1
        # 已经**从解码队列取走**的帧序号。取走第 i 帧后它等于 i，于是队列里下一
        # 帧的序号是 _last_taken_index + 1——丢帧追赶时据此算该丢几帧。
        self._last_taken_index = -1

        self._timer = QTimer(self)
        # 帧率定时必须用精确定时器，否则播放节奏会随系统负载漂移
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)
        self._playing = False

    # ---- 配置 ----------------------------------------------------------

    def set_source(self, path: str | Path | None,
                   size: tuple[int, int] | None = None) -> None:
        """设定源文件；``size`` 是**已知的源分辨率**（宽, 高），可选。

        每次换源都要清掉尺寸缓存，否则换一个不同分辨率的片子后仍按旧尺寸
        解码（画面糊/花或直接读不出帧）。传了 size 就直接算出输出尺寸，
        省掉首次播放时在调用线程里同步跑的那次 ffprobe。
        """
        self.stop()
        self._source = Path(path) if path else None
        self._cached_size = self._scaled(size) if size else (0, 0)

    def _scaled(self, size: tuple[int, int]) -> tuple[int, int]:
        width, height = size
        if width <= 0 or height <= 0:
            return (0, 0)
        out_width = min(self._max_width, width)
        out_height = max(2, int(round(height * out_width / width)))
        if out_height % 2:
            out_height += 1
        return (out_width, out_height)

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
        self._set_playing(True)
        self._start_reader(self._position)
        self._timer.start(self._interval_ms())
        self._start_audio()


    def pause(self) -> None:
        self._set_playing(False)
        self._timer.stop()
        self._stop_reader()

    def _set_playing(self, value: bool) -> None:
        """``_playing`` 的唯一写入口：状态没变不发信号。

        「播放」键兼作「暂停」键后，按钮文案完全依赖这个信号的准确性——
        自然播完（``_on_tick``）也必须走这里，否则键会一直停在「暂停」。
        收声也挂在这里：停下就一定没声音（暂停、自然播完、换源都走同一条路），
        免得漏掉某条退出路径留下一个还在响的 ffplay。
        """
        if self._playing != value:
            self._playing = value
            if not value:
                self._stop_audio()
            self.playing_changed.emit(value)

    # ---- 音频通道（预览出声）--------------------------------------------
    #
    # 画面是"FFmpeg 解帧 → QImage"，音频则交给一个只放声音的 ffplay 进程，两者
    # 用同一份文件、同一个起点并行起播，一起启停。为什么不走 QtMultimedia：
    # 它在 160MB+ 的 PySide6-Addons 里，本项目只装 Essentials（见模块开头）。
    # 代价是两条独立时钟，起播时声音会比画面晚半秒左右（ffplay 启动开销），
    # 逐帧审核够用；要严格同步只能换 QAudioSink 那条路。

    def set_audio_track(self, index: int | None) -> None:
        """指定预览放哪条音轨（ffprobe 的全局流序号，与导入页选中项一致）。"""
        self._audio_track = index

    def set_audio_enabled(self, enabled: bool) -> None:
        """关掉后完全不启音频进程（headless 冒烟、批处理截图这类场景用）。"""
        self._audio_enabled = enabled
        if not enabled:
            self._stop_audio()

    def _audio_command(self, start_seconds: float) -> list[str] | None:
        """音频通道命令；没 ffplay / 没源 / 已静音时返回 None（画面照常播）。"""
        if not self._audio_enabled or self._ffplay is None or self._source is None:
            return None
        args = [
            str(self._ffplay),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nodisp",          # 不出窗口
            "-vn",
            "-sn",              # 只解码音频，不浪费 CPU 在画面上
            "-autoexit",        # 播完自己退出，不留孤儿进程
        ]
        if self._audio_track is not None:
            args += ["-ast", str(self._audio_track)]
        args += ["-ss", f"{max(0.0, start_seconds):.6f}"]
        if self._end is not None:
            args += ["-t", f"{max(0.0, self._end - start_seconds):.6f}"]
        args += ["-i", str(self._source)]
        return args

    def _start_audio(self) -> None:
        self._stop_audio()
        command = self._audio_command(self._position)
        if command is None:
            return
        try:
            self._audio = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except OSError:
            # 放不出声音不该影响画面：静默降级（ffplay 被删、被策略拦住等）
            self._audio = None

    def _stop_audio(self) -> None:
        proc, self._audio = self._audio, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

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
        """定时器轮询间隔，**不是**帧时长。

        为什么不用帧时长当间隔：Windows 的定时器默认分辨率是 ~15.6ms，40ms 的
        间隔会让触发起伏被系统网格量化，实测**每个 40ms 间隔都会漏掉一次触发**，
        帧率从 25 掉到 18——而早先"每 tick 出一帧"的写法会把漏掉的触发直接变成
        永久性掉帧。改成 10ms 细粒度轮询后，一帧时长（40ms）内会触发约 4 次，
        该出哪一帧完全由 ``_on_tick`` 里的墙钟决定，多出来的触发直接早退。
        实测：40ms → 18 fps，20ms → 25 fps，10ms → 36 fps。
        """
        return 10


    def _frame_size(self) -> tuple[int, int]:
        """每帧原始字节对应的输出尺寸（``set_source`` 已知源分辨率时直接用）。"""
        if self._cached_size != (0, 0):
            return self._cached_size
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
        self._cached_size = self._scaled(
            (info.video.width, info.video.height)
        )
        return self._cached_size

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
        # 本段起点就是 start_seconds；但**先不起表**——ffmpeg 进程从启动到吐出
        # 第一帧有实打实的开销（1080p 上可达数百毫秒），若在启动那一刻就计时，
        # 第一帧到手时已"欠"了几百毫秒，会立刻触发丢帧追赶、开局就跳。
        # 改成第一帧真正到手时才起表（见 _on_tick），位置从起点干净地开始。
        self._clock_origin = max(0.0, start_seconds)
        self._clock_start = None
        self._last_index = -1
        self._last_taken_index = -1
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
            # 队列满了就先**退出 with**（放锁），再睡，醒了重查。
            #
            # 为什么不能在持锁时 sleep：GUI 线程的 _on_tick 要拿同一把锁来取帧，
            # 而 ``time.sleep(0.005)`` 在 Windows 上按 ~15.6ms 的系统时钟粒度落地
            # ——于是每一帧的取锁都要排在读者这次睡眠后面。实测（1080p25 素材，
            # 解码链路本身有 641fps 余量）：
            #   持锁 sleep  → 25fps 的视频只送出 16.8fps，tick 95 分位 16.4ms、
            #                 最坏 990ms（画面时间在走、画面却在跳 = 卡顿）
            #   放开锁再睡  → 24.7fps，tick 95 分位 0.6ms、最坏 1ms
            # 锁只用于保护队列本身，睡眠不属于临界区。
            with self._lock:
                full = len(self._queue) >= self.MAX_QUEUE
            if full:
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
        frame_duration = 1.0 / float(self._fps)

        # 尚未起表（本段第一帧还没到手）：这一 tick 只探队首，别推进位置。
        # 起表时刻就是"第一帧该显示的瞬间"，位置自此从起点匀速前进。
        if self._clock_start is None:
            with self._lock:
                ready = len(self._queue) > 0
            if not ready:
                if self._reader is not None and not self._reader.is_alive():
                    self._set_playing(False)
                    self._timer.stop()
                    self.playback_finished.emit()
                return
            self._clock_start = time.perf_counter()

        # 位置由墙钟决定，而不是"取了几帧"。clock_origin 是本段起点，加上真实
        # 经过的时间就是此刻应处的位置——解码慢了也是这个值，不会被拖住。
        elapsed = time.perf_counter() - self._clock_start
        self._position = self._clock_origin + elapsed

        # 这一段播完了就给个小余量收尾（余量是为了别把正好落在 end 上的最后一帧切掉）
        if self._end is not None and self._position >= self._end + frame_duration * 0.5:
            with self._lock:
                self._queue.clear()
            self._set_playing(False)
            self._timer.stop()
            self._stop_reader()
            self.playback_finished.emit()
            return

        # 本帧应显示的序号（从段起点算起）。定时器是细粒度轮询，一帧时长内可能
        # 触发多次；只在确实该换到新的一帧时才出图，否则直接早退（省掉重复的
        # QImage 构造与信号投递）。
        target_index = int(elapsed / frame_duration)
        if target_index <= self._last_index:
            return
        self._last_index = target_index

        with self._lock:
            queued = len(self._queue)
            if queued == 0:
                chunk = None
            else:
                # 队列里下一帧的序号是 _last_taken_index + 1。要取到 target_index，
                # 需要先丢掉排在它前面的 (target_index - 1 - _last_taken_index) 帧
                # —— 那些都已过期（解码跟不上时），丢了才追得回墙钟。
                drop = target_index - self._last_taken_index - 1
                if drop > 0:
                    drop = min(drop, queued - 1)     # 至少留一帧（就是目标帧）
                    for _ in range(drop):
                        self._queue.popleft()
                    self._last_taken_index += drop
                chunk = self._queue.popleft() if self._queue else None
            if chunk is not None:
                self._last_taken_index += 1

        if chunk is None:
            # 预解码队列空：若解码线程已结束，说明本段播完
            if self._reader is not None and not self._reader.is_alive():
                self._set_playing(False)
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
