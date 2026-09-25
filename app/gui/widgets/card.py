"""圆角白底卡片容器。

用法::

    card = Card(title="媒体信息", parent=parent)
    card.body_layout.addRow("时长", QLabel("00:02:00.000"))

或注入自定义 body 布局（QHBoxLayout / QFormLayout 等）::

    card = Card("预设", body_layout=QFormLayout())
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    """圆角白底卡片：标题区 + 内容区。"""

    def __init__(
        self,
        title: str = "",
        parent: QWidget | None = None,
        body_layout: QVBoxLayout | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        if title:
            header = QFrame(self)
            header.setProperty("role", "card-title")
            header_layout = QVBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 0)
            self._title_label = QLabel(title)
            self._title_label.setProperty("role", "card-title-text")
            header_layout.addWidget(self._title_label)
            outer.addWidget(header)

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
        self.body_layout.setContentsMargins(12, 10, 12, 10)
        self.body_layout.setSpacing(8)
        outer.addWidget(self.body, 1)

    def set_title(self, text: str) -> None:
        if hasattr(self, "_title_label"):
            self._title_label.setText(text)

    def title(self) -> str:
        return self._title_label.text() if hasattr(self, "_title_label") else ""