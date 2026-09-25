"""LabeledField：标签 + 控件 + 可选 help 文本的复合容器。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class LabeledField(QWidget):
    """标题 + 输入控件 + 可选 help 文本的复合容器。"""

    def __init__(
        self,
        label: str,
        *,
        help_text: str | None = None,
        suffix: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = QLabel(label)
        self._help = QLabel(help_text or "")
        self._help.setProperty("role", "muted")
        self._help.setVisible(bool(help_text))
        self._suffix_lbl = QLabel(suffix or "")
        self._suffix_lbl.setProperty("role", "muted")
        self._suffix_lbl.setVisible(bool(suffix))

        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(4)
        outer.addWidget(self._title)
        outer.addLayout(self._row)
        # 始终加入布局（无文案时不可见、不占位）：原先只在构造时可见才加，
        # 于是事后 set_help() 的文字虽然写进去了却永远不会显示。
        outer.addWidget(self._help)

    def attach(self, widget: QWidget) -> QWidget:
        """把输入控件接到字段行；如有 suffix 则控件后贴一个后缀。"""
        self._row.addWidget(widget, 1)
        if self._suffix_lbl.isVisible():
            self._row.addWidget(self._suffix_lbl)
        return widget

    def set_label(self, text: str) -> None:
        """改标题文案（设置表单里同一个字段在两种模式下含义不同）。"""
        self._title.setText(text)

    def set_help(self, text: str) -> None:
        self._help.setText(text)
        self._help.setVisible(bool(text))