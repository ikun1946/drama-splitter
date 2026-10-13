"""「环境自检」对话框：逐项报告本地环境，并对可修复项提供一键修复。

设计要点
--------
1. **只报告，不擅自动手**：打开对话框只跑检测（后台线程），绝不自动下载或安装
   任何东西。修复一律要用户点按钮。

2. **诚实区分"能修"与"只能指引"**：检测结果里 ``fix.kind == "download_model"``
   的才在应用内真能修好（走 ``model_manager``，用 curl 下载模型，不依赖 pip）。
   其余是 ``open_url``（打开网页）/ ``navigate``（跳到模型管理）/ ``none``
   （纯告知）。打包版缺 faster-whisper 这类问题**给不了下载按钮**——下载模型
   解决不了缺包，假装能修就是骗人。

3. **检测放后台线程**：检测里含 ``ffmpeg -version`` / ``-encoders``（可达数十秒）
   与若干 ``find_spec``，必须异步，否则开窗即冻结。

自动化约定
----------
与其它对话框一致：生产代码只在菜单处理器里 ``exec()``；测试/冒烟/截图只走
``MainWindow.build_env_check_dialog() + show()``，并可直接调 ``apply_report()``
注入一份构造好的报告，无需真正检测。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.env_check import (
    FIX_DOWNLOAD,
    FIX_INSTALL_ENGINE,
    FIX_NAVIGATE,
    FIX_OPEN_URL,
    LEVEL_BLOCK,
    LEVEL_OK,
    LEVEL_WARN,
    EnvCheckItem,
    EnvReport,
    check_environment,
)
from ..widgets import Card, StatusPill, scrollable
from .base import DialogShell

__all__ = ["EnvCheckDialog"]

#: 严重度 → 徽章配色与文案
_TONE = {LEVEL_OK: "success", LEVEL_WARN: "warn", LEVEL_BLOCK: "danger"}
_BADGE = {LEVEL_OK: "正常", LEVEL_WARN: "降级", LEVEL_BLOCK: "不可用"}


class _ItemRow(QFrame):
    """一行检测结果：徽章 + 名称 + 结论/细节 + 可选的修复按钮。"""

    fix_requested = Signal(str, str)   # (kind, payload)

    def __init__(self, item: EnvCheckItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setProperty("role", "issue-row")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(10)

        self.pill = StatusPill(_BADGE[item.level], tone=_TONE[item.level])
        self.pill.setFixedWidth(56)
        outer.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        name = QLabel(item.label)
        name.setProperty("role", "issue-title")
        text_col.addWidget(name)
        summary = QLabel(item.summary)
        summary.setWordWrap(True)
        summary.setProperty("role", "muted")
        text_col.addWidget(summary)
        if item.detail:
            detail = QLabel(item.detail)
            detail.setWordWrap(True)
            detail.setProperty("role", "issue-detail")
            text_col.addWidget(detail)
        outer.addLayout(text_col, 1)

        # 修复按钮：只有真能做点什么的才出现（none / 空文案一律不画）
        self.btn_fix: QPushButton | None = None
        if item.fix is not None and item.fix.kind != "none" and item.fix.label:
            self.btn_fix = QPushButton(item.fix.label)
            if item.fix.kind in (FIX_DOWNLOAD, FIX_INSTALL_ENGINE):
                # 这两种是**真的会在应用内动手装东西**的，样式给足分量
                self.btn_fix.setProperty("role", "primary")
            else:
                self.btn_fix.setProperty("role", "mini")
            self.btn_fix.setMinimumWidth(0)
            self.btn_fix.clicked.connect(self._on_fix)
            outer.addWidget(self.btn_fix, 0, Qt.AlignmentFlag.AlignVCenter)

    def _on_fix(self) -> None:
        assert self.item.fix is not None
        self.fix_requested.emit(self.item.fix.kind, self.item.fix.payload)


class EnvCheckDialog(DialogShell):
    """环境自检对话框。"""

    #: 用户点了"下载模型"类修复（payload = 模型 key）
    download_requested = Signal(str)
    #: 用户点了"安装转写引擎"（应用内下载运行期依赖）
    install_engine_requested = Signal()
    #: 用户点了"打开网页"类修复（payload = URL）
    open_url_requested = Signal(str)
    #: 用户点了"跳到某处"类修复（payload = 目标标识）
    navigate_requested = Signal(str)
    #: 用户点了"重新检测"
    recheck_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("环境自检", parent, width=840, height=640)

        self._rows: list[_ItemRow] = []

        self.headline = QLabel("检测中…")
        self.headline.setWordWrap(True)
        self.headline.setProperty("role", "subtitle")

        self._list_holder = QWidget()
        self._list_layout = QVBoxLayout(self._list_holder)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)

        card = Card("运行环境")
        card.body_layout.addWidget(scrollable(self._list_holder))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 12, 16, 0)
        body_layout.setSpacing(8)
        body_layout.addWidget(self.headline)
        body_layout.addWidget(card, 1)
        self.add_body(body)

        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setProperty("role", "mini")
        self.btn_recheck.clicked.connect(self.request_recheck)
        self.add_footer(self.btn_recheck)

        self.footer_hint.setText(
            "本页只检测、只报告——不会自动下载或安装任何东西，点按钮才会动手。\n"
            "「下载模型」与「安装转写引擎」能在应用内一键完成；"
            "FFmpeg 与部分可选依赖只能给出下载页与办法。"
        )
        self.add_close_button()
        self.install_footer()

    # ---- 对外 ----------------------------------------------------------

    def apply_report(self, report: EnvReport) -> None:
        """把一份检测结果铺进界面（测试可直接注入，跳过真实检测）。"""
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []

        for item in report.items:
            row = _ItemRow(item, self._list_holder)
            row.fix_requested.connect(self._on_fix_requested)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)

        self.headline.setText(self._headline_text(report))

    def items(self) -> list[dict]:
        """当前展示的条目（供测试断言，仿 ModelForm.rows()）。"""
        return [r.item.to_dict() for r in self._rows]

    def request_recheck(self) -> None:
        """发起一次重新检测（由主窗口接上后台 worker）。"""
        self.recheck_requested.emit()

    # ---- 内部 ----------------------------------------------------------

    @staticmethod
    def _headline_text(report: EnvReport) -> str:
        if report.usable:
            blocked = [i for i in report.items if i.level == LEVEL_WARN]
            if blocked:
                return (
                    "环境可用，但有 "
                    f"{len(blocked)} 项可选能力降级——核心流程（导入、分集、导出）不受影响。"
                )
            return "环境可用，所有检测项正常。"
        bad = [i.label for i in report.items if i.required and i.level == LEVEL_BLOCK]
        return "环境不可用：缺少必需组件 —— " + "、".join(bad)

    def _on_fix_requested(self, kind: str, payload: str) -> None:
        if kind == FIX_DOWNLOAD:
            self.download_requested.emit(payload)
        elif kind == FIX_INSTALL_ENGINE:
            self.install_engine_requested.emit()
        elif kind == FIX_OPEN_URL:
            self.open_url_requested.emit(payload)
        elif kind == FIX_NAVIGATE:
            self.navigate_requested.emit(payload)
