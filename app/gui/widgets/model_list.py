"""模型清单控件：筛选框 + 单选列表 + 计数/空态。

为什么不再"每行挂两个按钮"
------------------------
厂商的 ``/models`` 动辄上百条（硅基流动那种几百个）。原先表格每一行都并排放
「设为语义判断」「设为转写」两个宽按钮，结果是三个问题叠在一起：模型名被按钮
挤成省略号（真正要读的信息反而没了）、行高被撑开、一屏只剩四五行，长清单基本
没法用。

改成**筛选框 + 单选列表 + 选中后再指派**：模型名拿回整行宽度；几百条靠筛选框
定位；指派动作收在选中项上方的一行按钮里，不再逐行重复。

职责边界：只管显示与交互，**不碰配置**——指派 / 删除都发信号，由调用方写档案。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

__all__ = ["ModelList"]

#: 列表最小高度：太矮会挤成两三行，反而比"看不全"更糟
MIN_LIST_HEIGHT = 120


class ModelList(QWidget):
    """可筛选的模型名列表。"""

    #: 用户在列表里确认了一个模型（双击或回车）
    activated = Signal(str)
    #: 选中项变了（参数是模型名，空串表示没有选中）
    selection_changed = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        placeholder: str = "",
        filter_placeholder: str = "筛选模型名",
    ) -> None:
        super().__init__(parent)
        #: [(模型名, 行尾标记)]；标记可以是"手填""● 语义判断"，空串表示不标
        self._entries: list[tuple[str, str]] = []
        #: 上一次真正选中的模型名——重建清单时要保住它
        self._current = ""
        #: 调用方指定的优先选中项（通常是"正在用的那个模型"）
        self._preferred = ""

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText(filter_placeholder)
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setMinimumHeight(MIN_LIST_HEIGHT)
        self.list.setUniformItemSizes(True)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemActivated.connect(self._on_activated)

        self.note = QLabel("", self)
        self.note.setProperty("role", "muted")
        self.note.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self.filter_edit)
        outer.addWidget(self.list, 1)
        outer.addWidget(self.note)

        self._apply_placeholder(placeholder)

    # ---- 公开面 --------------------------------------------------------

    def set_items(self, entries, *, current: str = "", placeholder: str = "") -> None:
        """重建清单。``entries`` 是 ``[(模型名, 标记)]``。

        **重建时保住当前选中项**。清单会因为增删模型、指派、加参数标记而反复
        重建，每次都退回第一行的话，用户正在配的模型会被悄悄换掉——参数就会
        写到别人身上。选中的优先级：还在列表里的上一次选中项 > ``current``
        （通常是"正在用的那个模型"）> 第一行。
        """
        self._entries = [
            (str(name), str(tag or ""))
            for name, tag in entries
            if str(name).strip()
        ]
        self._preferred = str(current or "")
        self._apply_placeholder(placeholder)
        self._apply_filter(self.filter_edit.text())
        # 记住这次真正选中的：下一次重建还要保住它
        self._current = self.selected()

    def set_placeholder(self, text: str) -> None:
        """空态文案（没有模型时显示在列表里）。"""
        self._apply_placeholder(text)

    def selected(self) -> str:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def set_current(self, name: str) -> None:
        """选中某个模型（不在清单里就清空选中）。"""
        self._current = str(name or "")
        self._apply_filter(self.filter_edit.text())

    def names(self) -> list[str]:
        """**全部**模型名（不受筛选影响）——调用方要的是清单，不是当前这一屏。"""
        return [name for name, _ in self._entries]

    def shown_names(self) -> list[str]:
        """当前筛选后可见的模型名。"""
        return [
            str(self.list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.list.count())
        ]

    def set_filter(self, text: str) -> None:
        self.filter_edit.setText(str(text or ""))

    # ---- 内部 ----------------------------------------------------------

    def _apply_placeholder(self, text: str) -> None:
        self._placeholder = str(text or "")

    def _apply_filter(self, text: str) -> None:
        """按筛选框重建可见行，并尽量把当前模型留在选中位。"""
        keyword = (text or "").strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        for name, tag in self._entries:
            if keyword and keyword not in name.lower() and keyword not in tag.lower():
                continue
            label = f"{name}    {tag}".rstrip() if tag else name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            if tag:
                item.setToolTip(tag)
            self.list.addItem(item)
        # 选中优先级：还在列表里的「上一次选中」 > 调用方指定的（通常是在用的
        # 那个模型） > 第一行。保住上一项是必须的——否则用户在配 A 模型时因为
        # 刷新了一下列表就被挪到 B 上，参数会写到 B 身上。
        shown = self.shown_names()
        target = self._current if self._current in shown else ""
        if not target and self._preferred in shown:
            target = self._preferred
        if not target and self.list.count():
            target = str(self.list.item(0).data(Qt.ItemDataRole.UserRole))
        self.list.setCurrentRow(self._row_of(target))
        self.list.blockSignals(False)

        total = len(self._entries)
        shown = self.list.count()
        if not total:
            self.note.setText("")
        elif shown == total:
            self.note.setText(f"共 {total} 个模型。")
        else:
            self.note.setText(f"共 {total} 个模型，当前筛选出 {shown} 个。")

    def _on_current_changed(self, item: QListWidgetItem | None, _previous) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        name = str(name or "")
        # 用户选的即"当前模型"：重建清单时要保住它，否则参数会写到别的模型上
        if name:
            self._current = name
        self.selection_changed.emit(name)

    def _on_activated(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.activated.emit(str(name))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self._current and self.selected() != self._current:
            self.list.setCurrentRow(self._row_of(self._current))

    def _row_of(self, name: str) -> int:
        """按模型名找行号；找不到返回 -1（显式遍历，不依赖 findData 的匹配语义）。"""
        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.ItemDataRole.UserRole) == name:
                return index
        return -1
