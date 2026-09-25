"""StepNav —— 侧栏导航（步骤列表 + 完成态指示）。

基于 ``QListWidget``，但自绘每行：图标（QStyle.StandardPixmap）+ 标题 + 完成态对勾。
通过 ``set_state_dict()`` 设置每一步的状态（done/current/future）。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QWidget,
)


class StepState(str, Enum):
    FUTURE = "future"
    CURRENT = "current"
    DONE = "done"


_STEP_ICON_NAMES = [
    "SP_DialogOpenButton",       # 1 导入
    "SP_FileDialogDetailedView", # 2 设置
    "SP_BrowserReload",          # 3 分析
    "SP_FileDialogContentsView", # 4 审核
    "SP_DialogSaveButton",       # 5 导出
    "SP_DriveHDIcon",            # 6 模型
]


class StepNav(QListWidget):
    """侧栏步骤导航。"""

    def __init__(self, names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._names = list(names)
        self._states: list[StepState] = [StepState.FUTURE] * len(names)
        self._states[0] = StepState.CURRENT
        self.setFixedWidth(180)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(2)

        for i, name in enumerate(self._names):
            item = QListWidgetItem(self)
            self.addItem(item)
            item.setSizeHint(QSize(0, 44))
            item.setData(Qt.ItemDataRole.UserRole, i)

        self.setCurrentRow(0)

    def set_state(self, index: int, state: StepState) -> None:
        if 0 <= index < len(self._states):
            self._states[index] = state
            self.viewport().update()

    def set_states(self, states: list[StepState]) -> None:
        if len(states) == len(self._states):
            self._states = list(states)
            self.viewport().update()

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for i in range(self.count()):
            item = self.item(i)
            rect = self.visualItemRect(item)
            if rect.height() == 0:
                continue
            state = self._states[i]
            selected = i == self.currentRow()

            # 高亮条
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#3b6cb8"))
                painter.drawRect(rect.left(), rect.top(), 3, rect.height())
            # 背景
            if selected:
                painter.fillRect(rect.adjusted(3, 2, -2, -2), QColor("#e8f0fc"))
            elif rect.contains(self.mapFromGlobal(self.cursor().pos())):
                painter.fillRect(rect.adjusted(3, 2, -2, -2), QColor("#eef1f7"))

            # 圆点
            cx = rect.left() + 22
            cy = rect.center().y()
            if state == StepState.DONE:
                fill, border, mark = QColor("#2e7d32"), QColor("#2e7d32"), "✓"
            elif state == StepState.CURRENT:
                fill, border, mark = QColor("#3b6cb8"), QColor("#3b6cb8"), "●"
            else:
                fill, border, mark = QColor("#ffffff"), QColor("#d9dce4"), ""
            painter.setPen(border)
            painter.setBrush(fill)
            painter.drawEllipse(cx - 10, cy - 10, 20, 20)
            painter.setPen(QColor("white") if state != StepState.FUTURE else QColor("#a0a3ac"))
            f = QFont()
            f.setBold(True)
            f.setPointSize(9)
            painter.setFont(f)
            painter.drawText(cx - 10, cy - 10, 20, 20, Qt.AlignmentFlag.AlignCenter, mark)

            # 标题
            painter.setPen(QColor("#20232a") if state != StepState.FUTURE else QColor("#a0a3ac"))
            f.setBold(state == StepState.CURRENT or selected)
            f.setPointSize(11)
            painter.setFont(f)
            painter.drawText(
                rect.left() + 42, rect.top(), rect.width() - 50, rect.height(),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._names[i],
            )