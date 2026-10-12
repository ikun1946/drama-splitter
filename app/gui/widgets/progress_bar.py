"""CapsuleProgressBar —— 细长胶囊进度条（自绘）。

为什么不用 QProgressBar
----------------------
参考界面（即梦 AI 的「生成中」）的进度条有两点是 QProgressBar 给不出来的：

1. **细长全圆角胶囊**：轨道与填充都是两端半圆的胶囊形。QProgressBar 的
   ``::chunk`` 圆角只能近似——矮条 + 小宽度时四角会露出轨道底色；
2. **流动高光带**：没有细粒度进度的阶段显示一条来回扫过的高光带（"在做、
   剩余时间未知"的诚实表达），而不是 Qt busy 模式那种贴在左端的小方块。

所以沿用 :class:`TimelineRuler` 的既有做法自绘：``paintEvent`` + ``QPainter``。
取色一律走 ``qcolor(current_mode(), key)``，深浅色自动跟随，不需要换肤钩子；
也不写任何 QSS——自绘控件用不上，写了反而多一处要维护的样式。

确定的进度用**缓动**逼近目标值（``_shown`` 是显示值，``value()`` 仍是逻辑值），
这样百分比跳变时看到的是"推进"而不是"闪现"。测试读的是 ``value()``，不受影响。

设计令牌的用法
--------------
参考图是深色界面，所以那边是"深轨道 + 亮填充"。本项目浅色模式下轨道取
``TL_TRACK``（凹下去的浅灰），填充取色调令牌——对比关系一致，配色留在本
项目自己的体系里，不去抄即梦的色值。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import current_mode, qcolor

__all__ = [
    "CapsuleProgressBar",
    "TONE_NORMAL",
    "TONE_DONE",
    "TONE_FAILED",
    "TONE_MUTED",
]

#: 填充色调。轨道恒为 TL_TRACK，只换填充色。
TONE_NORMAL = "normal"   # 进行中
TONE_DONE = "done"       # 已完成
TONE_FAILED = "failed"   # 失败
TONE_MUTED = "muted"     # 取消 / 停摆

_TONE_KEY = {
    TONE_NORMAL: "PRIMARY",
    TONE_DONE: "SUCCESS",
    TONE_FAILED: "DANGER",
    TONE_MUTED: "NEUTRAL",
}

_TICK_MS = 16        # ≈60fps
_SWEEP_STEP = 0.018  # 高光带每帧推进的相位（约 0.9 秒扫一趟）
_GLIDE = 0.22        # 确定模式下每帧向目标值靠拢的比例
_SNAP = 0.4          # 差这么多就直接吸附，免得无限逼近让计时器空转
_BAND_RATIO = 0.34   # 高光带占轨道宽度的比例


class CapsuleProgressBar(QWidget):
    """细长胶囊进度条。

    ``setRange(0, 0)`` 切到流动模式（高光带来回扫），与 QProgressBar 的 busy
    约定一致——``maximum() == 0`` 就是"在跑，但没有可报的比例"。给一个大于 0
    的上限则回到确定模式，显示实际比例。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        thickness: int = 8,
    ) -> None:
        super().__init__(parent)
        self._minimum = 0
        self._maximum = 100
        self._value = 0
        self._shown = 0.0        # 缓动后的显示值（0..maximum）
        self._phase = 0.0        # 流动模式的扫光相位（0..1）
        self._tone = TONE_NORMAL

        self.setFixedHeight(thickness)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ---- QProgressBar 同形接口（阶段行与总进度都按这套调用） -------------

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802 - Qt 命名
        was_busy = self._maximum <= 0
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        if self._maximum <= 0:
            self._phase = 0.0
        else:
            if was_busy:
                # 从流动切回确定：此前没画过填充，从头推进
                self._shown = 0.0
            self._shown = max(0.0, min(self._shown, float(self._maximum)))
        self._sync_timer()
        self.update()

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt 命名
        self._value = int(value)
        self._sync_timer()
        self.update()

    def value(self) -> int:
        return self._value

    def maximum(self) -> int:
        return self._maximum

    def minimum(self) -> int:
        return self._minimum

    def set_tone(self, tone: str) -> None:
        """换填充色调（进行中 / 已完成 / 失败 / 已取消）。"""
        if tone not in _TONE_KEY or tone == self._tone:
            return
        self._tone = tone
        self.update()

    def tone(self) -> str:
        return self._tone

    # ---- 动画 -----------------------------------------------------------

    def _sync_timer(self) -> None:
        if self._maximum <= 0:
            self._timer.start()
            return
        if abs(float(self._value) - self._shown) > _SNAP:
            self._timer.start()
        else:
            self._shown = float(self._value)
            self._timer.stop()

    def _tick(self) -> None:
        if self._maximum <= 0:
            self._phase = (self._phase + _SWEEP_STEP) % 1.0
            self.update()
            return
        target = float(self._value)
        delta = target - self._shown
        if abs(delta) <= _SNAP:
            self._shown = target
            self._timer.stop()
        else:
            self._shown += delta * _GLIDE
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self._timer.stop()

    # ---- 绘制 -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        mode = current_mode()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        capsule = QPainterPath()
        capsule.addRoundedRect(rect, radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(qcolor(mode, "TL_TRACK")))
        painter.drawPath(capsule)

        fill = QColor(qcolor(mode, _TONE_KEY.get(self._tone, "PRIMARY")))
        if self._maximum <= 0:
            self._paint_sweep(painter, rect, capsule, fill)
        else:
            self._paint_fill(painter, rect, radius, fill)
        painter.end()

    def _paint_fill(
        self, painter: QPainter, rect: QRectF, radius: float, fill: QColor
    ) -> None:
        if self._maximum <= 0:
            return
        frac = max(0.0, min(1.0, self._shown / self._maximum))
        width = rect.width() * frac
        if width <= 0.5:
            return
        # 填充本身也是胶囊：宽度不足一个直径时圆角随之收窄，不会露出尖角
        bar = QRectF(rect.left(), rect.top(), width, rect.height())
        corner = min(radius, width / 2)
        # 上浅下深的竖向渐变——让它读起来是一根有厚度的胶囊，不是一块色板
        gradient = QLinearGradient(bar.left(), bar.top(), bar.left(), bar.bottom())
        gradient.setColorAt(0.0, fill.lighter(118))
        gradient.setColorAt(1.0, fill)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(bar, corner, corner)

    def _paint_sweep(
        self, painter: QPainter, rect: QRectF, capsule: QPainterPath, fill: QColor
    ) -> None:
        """流动模式：一条两端淡出的高光带扫过轨道。"""
        band = max(rect.width() * _BAND_RATIO, rect.height() * 2.2)
        span = rect.width() + band
        x = rect.left() - band + span * self._phase

        head = QColor(fill)
        head.setAlpha(215)
        tail = QColor(fill)
        tail.setAlpha(0)

        gradient = QLinearGradient(x, rect.top(), x + band, rect.top())
        gradient.setColorAt(0.0, tail)
        gradient.setColorAt(0.5, head)
        gradient.setColorAt(1.0, tail)

        painter.save()
        painter.setClipPath(capsule)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRect(QRectF(x, rect.top(), band, rect.height()))
        painter.restore()
