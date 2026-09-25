"""StepIndicator —— 横向步骤条。

用法::

    indicator = StepIndicator(["导入", "分析", "审核", "导出"])
    indicator.set_current(2)
    indicator.set_done(*range(2))   # 标记前两步已完成
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QBrush, QMouseEvent
from PySide6.QtWidgets import QWidget


class StepIndicator(QWidget):
    """横向步骤指示器（可点击）。"""

    clicked = Signal(int)

    def __init__(self, steps: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps = list(steps)
        self._done: set[int] = set()
        self._current = 0
        self.setMinimumHeight(48)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_steps(self, steps: list[str]) -> None:
        self._steps = list(steps)
        self._done.clear()
        self._current = 0
        self.update()

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self._steps):
            self._current = index
            self.update()

    def set_done(self, *indices: int) -> None:
        self._done = set(indices)
        self.update()

    def mark_done(self, index: int) -> None:
        self._done.add(index)
        self.update()

    def step_count(self) -> int:
        return len(self._steps)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        n = len(self._steps)
        if n == 0:
            return
        margin = 12
        step_w = (self.width() - 2 * margin) / n
        x = event.position().x()
        for i in range(n):
            cx = margin + step_w * (i + 0.5)
            if abs(x - cx) < step_w / 2:
                self.clicked.emit(i)
                break

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if not self._steps:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        margin = 12
        n = len(self._steps)
        step_w = (w - 2 * margin) / n
        circle_r = 9
        cy = h / 2 - 4

        for i in range(n - 1):
            x1 = margin + step_w * (i + 0.5)
            x2 = margin + step_w * (i + 1.5)
            done = i in self._done
            color = QColor("#3b6cb8") if done else QColor("#d9dce4")
            painter.setPen(QPen(color, 2))
            painter.drawLine(int(x1) + circle_r, int(cy), int(x2) - circle_r, int(cy))

        for i, label in enumerate(self._steps):
            cx = margin + step_w * (i + 0.5)
            done = i in self._done
            current = i == self._current

            if done:
                fill = QColor("#2e7d32")
                mark_color = QColor("white")
                mark = "✓"
            elif current:
                fill = QColor("#3b6cb8")
                mark_color = QColor("white")
                mark = str(i + 1)
            else:
                fill = QColor("#ffffff")
                mark_color = QColor("#a0a3ac")
                mark = str(i + 1)

            painter.setPen(QPen(QColor("#3b6cb8") if current else QColor("#d9dce4"), 2))
            painter.setBrush(QBrush(fill))
            painter.drawEllipse(int(cx) - circle_r, int(cy) - circle_r, circle_r * 2, circle_r * 2)

            painter.setPen(QPen(mark_color))
            f = QFont()
            f.setBold(True)
            f.setPointSize(8)
            painter.setFont(f)
            painter.drawText(
                int(cx) - circle_r, int(cy) - circle_r, circle_r * 2, circle_r * 2,
                Qt.AlignmentFlag.AlignCenter, mark,
            )

            painter.setPen(QPen(QColor("#20232a") if (current or done) else QColor("#a0a3ac")))
            f.setBold(current)
            f.setPointSize(8)
            painter.setFont(f)
            painter.drawText(
                int(cx) - step_w / 2 + 4, int(cy) + circle_r + 4,
                int(step_w) - 8, h - int(cy) - circle_r - 4,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )