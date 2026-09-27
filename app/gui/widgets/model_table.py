"""模型清单表格（ModelTable）。

一行一个模型名，行内可直接指派给「语义判断 / 语音转写」，也能手动添加名字。
两个来源合在一张表里，用来源小字区分：

- **自动**：该厂商 ``GET /models`` 拉回来的（表单点「刷新模型列表」重取）；
- **手填**：用户自己敲进去的，落盘在档案的 ``custom_models``，可单独删除。

存在的理由：有些厂商的可用模型**不在清单里**——方舟用推理接入点 ID
（``ep-xxx``）、自建网关常只暴露别名，手填的名字必须能留下来。

职责边界：表格只管显示与交互，**不碰配置**——指派 / 添加 / 删除都发信号，
由表单写档案（与 ``panes`` 层"只通过公开信号向上汇报"的约定一致）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.providers import CAP_ASR, CAP_JUDGE
from .scroll_area import scrollable

__all__ = ["ModelTable"]

#: 行区高度：贴着内容长（一行也不要留一大片空白），多了就固定到上限并出滚动条
MIN_LIST_HEIGHT = 64
MAX_LIST_HEIGHT = 236


class _ModelRow(QFrame):
    """一行：模型名 + 来源 + 「设为语义判断 / 设为转写」（+ 手填行的删除）。"""

    def __init__(self, name: str, *, custom: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.custom = custom
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(8)

        self.label = QLabel(name, self)
        row.addWidget(self.label, 1)
        if custom:
            source = QLabel("手填", self)
            source.setProperty("role", "muted")
            row.addWidget(source)

        self.btn_judge = QPushButton("设为语义判断", self)
        self.btn_asr = QPushButton("设为转写", self)
        self.btn_remove: QPushButton | None = None
        for button in (self.btn_judge, self.btn_asr):
            button.setProperty("role", "ghost")
            row.addWidget(button)
        if custom:
            self.btn_remove = QPushButton("删除", self)
            self.btn_remove.setProperty("role", "ghost")
            row.addWidget(self.btn_remove)

    def set_use(self, *, judge: bool, asr: bool) -> None:
        """标出这行指给了哪条能力。已指派的按钮再点没有意义，顺手禁用。"""
        self.btn_judge.setText("● 语义判断" if judge else "设为语义判断")
        self.btn_judge.setEnabled(not judge)
        self.btn_asr.setText("● 转写" if asr else "设为转写")
        self.btn_asr.setEnabled(not asr)


class ModelTable(QWidget):
    """模型清单表格；对外只发信号，不写配置。"""

    assigned = Signal(str, str)   # 模型名, 能力（CAP_JUDGE / CAP_ASR）
    added = Signal(str)           # 手动添加的模型名
    removed = Signal(str)         # 删除一个手填的模型名

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[_ModelRow] = []
        self._names: tuple[str, ...] = ()
        self._fetched: set[str] = set()
        self._judge = ""
        self._asr = ""

        self._inner = QWidget(self)
        self._list = QVBoxLayout(self._inner)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(2)
        self._empty = QLabel(
            "还没有模型信息：点「刷新模型列表」从该厂商拉一次，"
            "或在下面直接添加一个模型名。",
            self._inner,
        )
        self._empty.setProperty("role", "muted")
        self._empty.setWordWrap(True)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)

        self._scroll = scrollable(self._inner)
        self._scroll.setMinimumHeight(MIN_LIST_HEIGHT)

        self.edit_add = QLineEdit(self)
        self.edit_add.setPlaceholderText("输入模型名，回车即添加（清单里没有的名字也能用）")
        self.edit_add.returnPressed.connect(self._emit_add)
        self.btn_add = QPushButton("添加", self)
        self.btn_add.clicked.connect(self._emit_add)
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(8)
        add_row.addWidget(self.edit_add, 1)
        add_row.addWidget(self.btn_add)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self._scroll)
        outer.addLayout(add_row)

    # ---- 公开面（表单、工具与测试用）------------------------------------

    def set_models(self, names, *, fetched=()) -> None:
        """重建全部行。``fetched`` 里的算"自动"来源（不标手填、不能删）。"""
        self._names = tuple(str(name) for name in names)
        self._fetched = {str(name).strip() for name in fetched if str(name).strip()}
        self._rebuild()

    def set_assignment(self, judge_model: str, asr_model: str) -> None:
        """标记哪一行给了哪条能力（档案里的 judge_model / asr_model_name）。"""
        self._judge = (judge_model or "").strip()
        self._asr = (asr_model or "").strip()
        for row in self._rows:
            row.set_use(judge=row.name == self._judge, asr=row.name == self._asr)

    def rows(self) -> list[dict]:
        """各行的当前状态（工具与测试按这个读，不去摸内部控件）。"""
        return [
            {
                "name": row.name,
                "custom": row.custom,
                "in_judge": row.name == self._judge,
                "in_asr": row.name == self._asr,
            }
            for row in self._rows
        ]

    def names(self) -> list[str]:
        return [row.name for row in self._rows]

    # ---- 内部 ----------------------------------------------------------

    def _emit_add(self) -> None:
        name = self.edit_add.text().strip()
        if name:
            self.added.emit(name)

    def _rebuild(self) -> None:
        for row in self._rows:
            self._list.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []

        for name in self._names:
            row = _ModelRow(name, custom=name not in self._fetched, parent=self._inner)
            row.btn_judge.clicked.connect(
                lambda _=False, value=name: self.assigned.emit(value, CAP_JUDGE)
            )
            row.btn_asr.clicked.connect(
                lambda _=False, value=name: self.assigned.emit(value, CAP_ASR)
            )
            if row.btn_remove is not None:
                row.btn_remove.clicked.connect(
                    lambda _=False, value=name: self.removed.emit(value)
                )
            # 插在空态标签之后、stretch 之前
            self._list.insertWidget(self._list.count() - 1, row)
            self._rows.append(row)

        self._empty.setVisible(not self._rows)
        self.set_assignment(self._judge, self._asr)
        self._sync_height()

    def _sync_height(self) -> None:
        """行区高度贴着内容长，超过上限才固定下来出滚动条。"""
        wanted = max(self._inner.sizeHint().height(), MIN_LIST_HEIGHT)
        self._scroll.setFixedHeight(min(wanted, MAX_LIST_HEIGHT))