"""模型清单（ModelTable）：厂商 ``/models`` 清单 + 手填项，可指派给两条能力。

两个来源合在一张表里，用行尾标记区分：

- **自动**：该厂商 ``GET /models`` 拉回来的（点「刷新模型列表」重取）；
- **手填**：用户自己敲进去的，落盘在档案的 ``custom_models``，可单独删除。

存在的理由：有些厂商的可用模型**不在清单里**——方舟用推理接入点 ID
（``ep-xxx``）、自建网关常只暴露别名，手填的名字要能留下来。

版式（实测改过一次）
------------------
原先每行并排两个宽按钮（「设为语义判断」「设为转写」）。厂商清单动辄几百条，
那样排的结果是模型名被挤成省略号、一屏只剩四五行、长清单根本没法用。现在改成
:class:`~app.gui.widgets.model_list.ModelList` 的"筛选框 + 单选列表"，指派动作
收在选中项下方的一行按钮里——模型名拿回整行宽度，几百条也能靠筛选定位。

职责边界：表格只管显示与交互，**不碰配置**——指派 / 添加 / 删除都发信号，
由表单写档案（与 ``panes`` 层"只通过公开信号向上汇报"的约定一致）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.providers import CAP_ASR, CAP_JUDGE
from .model_list import ModelList

__all__ = ["ModelTable"]

EMPTY_HINT = (
    "还没有模型信息：点「刷新模型列表」从该厂商拉一次，"
    "或在下面直接添加一个模型名。"
)


class ModelTable(QWidget):
    """模型清单；对外只发信号，不写配置。"""

    assigned = Signal(str, str)   # 模型名, 能力（CAP_JUDGE / CAP_ASR）
    added = Signal(str)           # 手动添加的模型名
    removed = Signal(str)         # 删除一个手填的模型名

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._names: tuple[str, ...] = ()
        self._fetched: set[str] = set()
        self._tags: dict[str, str] = {}
        self._judge = ""
        self._asr = ""

        self.list = ModelList(
            self,
            placeholder=EMPTY_HINT,
            filter_placeholder="筛选模型名（几百个模型时靠它定位）",
        )

        # 指派动作作用在**选中项**上，不再逐行重复
        self.btn_judge = QPushButton("设为语义判断", self)
        self.btn_asr = QPushButton("设为转写", self)
        self.btn_remove = QPushButton("删除", self)
        self.btn_judge.setProperty("role", "ghost")
        self.btn_asr.setProperty("role", "ghost")
        self.btn_remove.setProperty("role", "ghost")
        self.btn_judge.clicked.connect(
            lambda: self._emit_assigned(self.list.selected(), CAP_JUDGE)
        )
        self.btn_asr.clicked.connect(
            lambda: self._emit_assigned(self.list.selected(), CAP_ASR)
        )
        self.btn_remove.clicked.connect(
            lambda: self.list.selected() and self.removed.emit(self.list.selected())
        )
        self.btn_remove.setEnabled(False)
        self.list.selection_changed.connect(self._sync_buttons)

        self.edit_add = QLineEdit(self)
        self.edit_add.setPlaceholderText("输入模型名，回车即添加（清单里没有的名字也能用）")
        self.edit_add.returnPressed.connect(self._emit_add)
        self.btn_add = QPushButton("添加", self)
        self.btn_add.clicked.connect(self._emit_add)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        action_row.addWidget(self.btn_judge)
        action_row.addWidget(self.btn_asr)
        action_row.addWidget(self.btn_remove)
        action_row.addStretch(1)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(8)
        add_row.addWidget(self.edit_add, 1)
        add_row.addWidget(self.btn_add)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self.list, 1)
        outer.addLayout(action_row)
        outer.addLayout(add_row)

    # ---- 公开面（表单、工具与测试用）------------------------------------

    def set_models(self, names, *, fetched=(), tags=None) -> None:
        """重建全部行。

        ``fetched`` 里的算"自动"来源（不标手填、不能删）；``tags`` 是调用方给的
        额外行尾标记（参数表单用它显示"128k / 思考·中 / 文本+图"），表格自己不
        认识那些字段——显示归表格，判断归调用方。
        """
        self._names = tuple(str(name) for name in names)
        self._fetched = {str(name).strip() for name in fetched if str(name).strip()}
        self._tags = {str(k): str(v) for k, v in (tags or {}).items() if str(v).strip()}
        self._rebuild()

    def set_assignment(self, judge_model: str, asr_model: str) -> None:
        """标记哪一行给了哪条能力（档案里的 judge_model / asr_model_name）。"""
        self._judge = (judge_model or "").strip()
        self._asr = (asr_model or "").strip()
        self._rebuild()

    def set_tags(self, tags: dict) -> None:
        """更新行尾的参数标记（只刷新显示，不动清单本身）。"""
        self._tags = {str(k): str(v) for k, v in (tags or {}).items() if str(v).strip()}
        self._rebuild()

    def rows(self) -> list[dict]:
        """各行的当前状态（工具与测试按这个读，不去摸内部控件）。

        按**全部**模型返回，不受筛选框影响——筛选只是显示层的便利。
        """
        return [
            {
                "name": name,
                "custom": name not in self._fetched,
                "in_judge": name == self._judge,
                "in_asr": name == self._asr,
            }
            for name in self._names
        ]

    def names(self) -> list[str]:
        return list(self._names)

    # ---- 内部 ----------------------------------------------------------

    def _emit_add(self) -> None:
        name = self.edit_add.text().strip()
        if name:
            self.added.emit(name)

    def _emit_assigned(self, name: str, capability: str) -> None:
        if name:
            self.assigned.emit(name, capability)

    def _tag_of(self, name: str) -> str:
        tags: list[str] = []
        if self._tags.get(name):
            tags.append(self._tags[name])
        if name not in self._fetched:
            tags.append("手填")
        if name == self._judge:
            tags.append("● 语义判断")
        if name == self._asr:
            tags.append("● 转写")
        return "　".join(tags)

    def _rebuild(self) -> None:
        current = self._judge or self._asr
        self.list.set_items(
            [(name, self._tag_of(name)) for name in self._names],
            current=current,
            placeholder=EMPTY_HINT,
        )
        self._sync_buttons(self.list.selected())

    def _sync_buttons(self, name: str) -> None:
        """按钮状态跟着选中项走：没选中、或已经指派过的那条就置灰。

        "已指派"这件事显示在列表行的标记上（● 语义判断 / ● 转写），按钮只负责
        还没做过的动作——重复指派没有意义，置灰而不是让用户点完发现没反应。
        """
        name = str(name or "")
        picked = bool(name)
        self.btn_judge.setEnabled(picked and name != self._judge)
        self.btn_asr.setEnabled(picked and name != self._asr)
        self.btn_remove.setEnabled(picked and name not in self._fetched)
