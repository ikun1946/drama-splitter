"""空态（EmptyState）。

为什么不用一行小字
----------------
原先所有空态都是 ``QLabel("暂无问题。")`` 居中一放。右栏在"素材已就绪、还没有问题"
这个最常见的阶段里，95% 的面积就那么一行 11pt 的字——用户看不出这是"没检查出问题"
还是"面板没加载出来"。

空态要回答三件事：**这是什么**、**为什么空**、**现在能做什么**。所以拆成
图标占位 + 标题 + 说明 + 可选动作，而不是一句话。

用法::

    EmptyState("还没有问题", title="环境检查", detail="导入源片后这里会列出兼容性问题。")
    EmptyState("暂无建议", detail="集内的切点风险会出现在这里。")
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = ["EmptyState"]

_ICON_SIZE = 36
_DOT_SIZE = 8


class _Icon(QFrame):
    """圆形浅色底 + 居中圆点。

    刻意不引外部图标资源：QSS 里没有任何图片机制，引资源就得自己管深浅两套
    路径与 DPI，而一个圆点已经足够把"这里本来该有内容"这件事说清楚。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "empty-icon")
        self.setFixedSize(_ICON_SIZE, _ICON_SIZE)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        from ..theme import current_mode, qcolor

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(qcolor(current_mode(), "PRIMARY")))
        painter.drawEllipse(
            self.rect().center().x() - _DOT_SIZE // 2,
            self.rect().center().y() - _DOT_SIZE // 2,
            _DOT_SIZE,
            _DOT_SIZE,
        )


class EmptyState(QWidget):
    """居中空态：图标 + 标题 + 说明 + 可选动作。"""

    def __init__(
        self,
        text: str = "",
        *,
        title: str = "",
        detail: str = "",
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "empty-state")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._icon = _Icon(self)
        # title 为空时用 text 当标题（兼容"只给一句话"的旧调用方式）
        self._title = QLabel(title or text, self)
        self._title.setProperty("role", "empty-title")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)

        self._detail = QLabel(detail, self)
        self._detail.setProperty("role", "empty-detail")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail.setWordWrap(True)
        self._detail.setVisible(bool(detail))

        self._action = QPushButton(action_text, self)
        self._action.setProperty("role", "ghost")
        self._action.setVisible(bool(action_text))

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(4)
        column.addWidget(self._title)
        column.addWidget(self._detail)
        column.addWidget(self._action, 0, Qt.AlignmentFlag.AlignHCenter)
        # 标题的 margin 靠外层 spacer 拉开，避免 QLabel 自带 margin 叠加
        column.setContentsMargins(16, 16, 16, 16)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addStretch(1)
        holder.addLayout(column)
        holder.addStretch(1)
        outer.addLayout(holder)
        outer.addStretch(2)

    def set_text(self, text: str) -> None:
        self._title.setText(text)

    def set_detail(self, text: str) -> None:
        self._detail.setText(text)
        self._detail.setVisible(bool(text))

    def title_text(self) -> str:
        return self._title.text()

    def detail_text(self) -> str:
        return self._detail.text()
