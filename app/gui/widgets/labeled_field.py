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
        # 三个标签一律把 self 当父级：Qt 里"没有父级 + setVisible(True)"等于
        # 建一个**真正的顶层窗口**。当初 suffix 标签就是这样——分集设置里
        # 4 个带单位(秒/％)的字段会在对话框弹出的瞬间各弹出一个十几像素的
        # 小窗，正是用户看到的那几个「奇怪的小窗口」。
        self._title = QLabel(label, self)
        self._help = QLabel(help_text or "", self)
        self._suffix_lbl = QLabel(suffix or "", self)
        self._help.setProperty("role", "muted")
        # help 必须折行：不折会把它所在的整行撑宽（厂商管理里就出现过"输入框右侧的
        # 控件被挤出可视区"，因为一行 help 比视口还宽，触发了横向滚动）
        self._help.setWordWrap(True)
        self._help.setVisible(bool(help_text))
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
        # 始终挂上（无 suffix 时不可见、不占位），与 _help 同一套做法。
        # 不再用 isVisible() 当「有没有后缀」的判据：构造期字段还没入壳，
        # isVisible() 恒为 False，会把后缀漏挂掉。
        self._row.addWidget(self._suffix_lbl)
        return widget

    def set_label(self, text: str) -> None:
        """改标题文案（设置表单里同一个字段在两种模式下含义不同）。"""
        self._title.setText(text)

    def set_help(self, text: str) -> None:
        self._help.setText(text)
        self._help.setVisible(bool(text))