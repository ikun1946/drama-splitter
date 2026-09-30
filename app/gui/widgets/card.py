"""圆角白底卡片容器。

用法::

    card = Card(title="媒体信息", parent=parent)
    card.body_layout.addRow("时长", QLabel("00:02:00.000"))

标题左侧可以挂一条**语义色条**，用来在不加噪文字的前提下标出这张卡的状态
（成功 / 警告 / 阻断）::

    card = Card("集长范围（两种来源互斥）", accent="warn")

或注入自定义 body 布局（QHBoxLayout / QFormLayout 等）::

    card = Card("预设", body_layout=QFormLayout())
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

#: 可用的语义色条档位。None = 不显示色条（默认）。
ACCENTS = ("primary", "success", "warn", "danger")


class Card(QFrame):
    """圆角白底卡片：标题区 + 内容区。"""

    def __init__(
        self,
        title: str = "",
        parent: QWidget | None = None,
        body_layout: QVBoxLayout | None = None,
        *,
        accent: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self._accent: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        if title:
            header = QFrame(self)
            header.setProperty("role", "card-title")
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)
            self._accent_bar = QFrame(header)
            self._accent_bar.setProperty("role", "accent-bar")
            self._accent_bar.setFixedWidth(3)
            self._accent_bar.setFixedHeight(15)
            header_layout.addWidget(
                self._accent_bar, 0, Qt.AlignmentFlag.AlignVCenter
            )
            self._title_label = QLabel(title)
            self._title_label.setProperty("role", "card-title-text")
            header_layout.addWidget(self._title_label)
            header_layout.addStretch(1)
            self._header_layout = header_layout
            outer.addWidget(header)
            self.set_accent(accent)

        self.body = QFrame(self)
        self.body.setProperty("role", "card-body")
        if body_layout is None:
            self.body_layout = QVBoxLayout(self.body)
        else:
            # 调用方传入的布局：把它挂到 self.body 上
            old_layout = self.body.layout()
            if old_layout is not None:
                # 把旧 layout 从 widget 上摘下来，避免双 layout
                old_widget = QWidget()
                old_widget.setLayout(old_layout)
                old_widget.deleteLater()
            self.body_layout = body_layout
            self.body.setLayout(self.body_layout)
        self.body_layout.setContentsMargins(16, 16, 16, 16)
        self.body_layout.setSpacing(12)
        outer.addWidget(self.body, 1)

    def set_accent(self, accent: str | None) -> None:
        """设置标题左侧的语义色条；``None`` 或未知档位则隐藏。"""
        if not hasattr(self, "_accent_bar"):
            return
        self._accent = accent if accent in ACCENTS else None
        self._accent_bar.setVisible(self._accent is not None)
        if self._accent is not None:
            self._accent_bar.setProperty("tone", self._accent)
        # dynamic property 要重新 polish 才生效
        self._accent_bar.style().unpolish(self._accent_bar)
        self._accent_bar.style().polish(self._accent_bar)

    def accent(self) -> str | None:
        return self._accent

    def set_title(self, text: str) -> None:
        if hasattr(self, "_title_label"):
            self._title_label.setText(text)

    def add_header_widget(self, widget: QWidget) -> None:
        """在标题右侧挂一个控件（如"使用中：whisper-small"或展开按钮）。

        没有标题的卡片会被忽略——头部不存在，无从挂起。
        """
        if not hasattr(self, "_header_layout"):
            return
        self._header_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)

    def title(self) -> str:
        return self._title_label.text() if hasattr(self, "_title_label") else ""