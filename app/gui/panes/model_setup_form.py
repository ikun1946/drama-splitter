"""「模型设置」表单：两条能力各选一个模型，本地与云端可以混着用。

一个下拉，两种来源
------------------
原先这三件事散在三个窗口：本地模型页签管本地档位、「云端模型」页签管厂商档案、
「选择云端模型」对话框管云端模型名。用户的问题是同一个——"语音转写到底在用哪个
模型"——却要在三个窗口之间来回跳。

现在每条能力只剩**一个可编辑下拉**：本机已装的模型排在最上面，下面是云端、
按厂商分子组。**选哪一条，后端就是哪边**——原先那两个「本地模型 / 云端模型」
切换按钮是冗余的（选模型已经隐含了走哪边），改成标题右侧一枚只读徽章说明现状。

为什么是**可编辑**下拉而不是普通下拉
------------------------------------
一次解决掉两件原本要额外控件才能做的事：

- **筛选**：厂商的 ``/models`` 动辄上百条，点框直接输入即可过滤，
  不必再摆一个筛选框（原先那一条占了半屏高度）；
- **手输**：方舟的 ``ep-xxx``、自建网关的别名不在任何 ``/models`` 里，
  可编辑下拉原生支持"框里打了字但不在清单中"。

手输的名字挂到**哪一家**厂商是个真问题：一个框里同时可能有几家的模型，
凭空猜一个会把模型名写进错误的档案。这里只在能确定时接受——该能力已选的云端
厂商，或全局唯一一家配了 Key 的厂商；否则明确告诉用户先去选一个。

状态只说两件事
--------------
第一行是**结论**（此刻到底跑什么），第二行才是细节（差哪一项、怎么修）。

原先把「已生效的」「刚选的」「为什么不生效」「怎么去修」连成一句话，96 个字里
从没说过"这样这条能力根本不会执行"——而那恰恰是用户最该先知道的一句。
短剧的分集质量直接押在语义判断上，界面对此闭嘴是失职。

边界（刻意不做的事）
--------------------
本页**只选模型**（本地档位 / 云端模型名），不碰厂商的地址、协议与 API Key，
也不负责下载模型：

- Key 与接口地址 → 「模型管理 → 云端模型」
- 下载本地模型 → 「模型管理 → 本地模型」

写 ``backend_prefs`` 不等于立刻外发：是否真的走云端由 ``providers.block_reason``
把关（档案必须"测通过 + 勾了允许外发素材"），核心层每次分析前都会重判一次。
所以本页可以放心让用户选云端——选中了但没生效时，状态行会**指名说出差哪一项**。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import LOCAL_BACKEND, AppConfig
from ...core.model_manager import (
    WHISPER_TIERS,
    installed_llm_specs,
    installed_whisper_tiers,
    spec_by_key,
)
from ...core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    CAP_LABELS,
    ProviderProfile,
    block_reason,
    merge_model_choices,
)
from ..state import ProjectState
from ..widgets import Card, NoWheelComboBox, SectionHeaderDelegate, scrollable
from ..workers import ModelListWorker
from ._common import register_worker

__all__ = ["ModelSetupForm"]

#: 能力 → 本地模型在 config.json 里的字段（两处都要写：档位/模型名 + 后端开关）
_LOCAL_FIELD = {CAP_ASR: "asr_model_tier", CAP_JUDGE: "llm_model_key"}
#: 能力 → 云端模型名写在档案的哪个字段
_MODEL_FIELD = {CAP_JUDGE: "judge_model", CAP_ASR: "asr_model_name"}

#: 能力 → 卡片右上角的一句话。回答"这条能力到底在干嘛"，
#: 否则"语义判断"三个字对没用过的人是没有落点的。
_CAPABILITY_HINTS = {
    CAP_ASR: "把视频里的声音转成带时间戳的文字",
    CAP_JUDGE: "判断哪里该断开，剧情才连贯",
}

#: 下拉项的四种角色。header/note 只作分组标题与提示，禁用不可选。
KIND_LOCAL = "local"
KIND_CLOUD = "cloud"
KIND_HEADER = "header"
KIND_NOTE = "note"


@dataclass(frozen=True)
class ModelChoice:
    """下拉里的一项。

    ``label`` 是给人看的，``(kind, value, provider_id)`` 是给代码看的——
    匹配"当前选中项"只比后者，因为 label 带了分组缩进与「（未安装）」等修饰。
    """

    kind: str
    label: str
    value: str = ""
    provider_id: str = ""
    selectable: bool = True

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.value, self.provider_id)


class _CapabilityCard(QWidget):
    """一条能力：一个下拉 + 一句结论 + 一行细节。"""

    #: 用户在这个下拉里选了一项（参数是能力 id）
    choice_picked = Signal(str)
    #: 用户点了状态行里的「去配置」/「去下载」
    fix_requested = Signal(str, str)

    def __init__(self, capability: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.capability = capability
        self._loading = False
        #: 最近一次**可选项**的下标。分类标题（本地模型 / 云端模型）被选中时
        #: 回退到这里——见 _on_index_changed。
        self._last_valid_index: int = -1

        # ---- 标题右侧：后端徽章（只读）+ 一句话说明 ----------------------
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        self.badge = QLabel("—", header)
        self.badge.setProperty("role", "badge")
        self.badge.setToolTip("后端由你选中的模型决定：选本机已安装的走本地，选云端就走云端")
        self.hint = QLabel(_CAPABILITY_HINTS.get(capability, ""), header)
        self.hint.setProperty("role", "muted")
        header_layout.addWidget(self.badge)
        header_layout.addStretch(1)
        header_layout.addWidget(self.hint)

        self.card = Card(title, parent=self)
        self.card.add_header_widget(header)

        # ---- 唯一需要动手的控件 ------------------------------------------
        self.model_label = QLabel("模型", self)
        self.model_label.setProperty("role", "muted")
        self.model_label.setFixedWidth(44)
        self.model_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )

        self.combo = NoWheelComboBox(self)
        self.combo.setEditable(True)
        # 分组标题画成弱化的一行——这是"降低误点"，真正的拦截在
        # _on_index_changed：setEnabled(False) 只管视图层，拦不住索引变化。
        self.combo.setItemDelegate(SectionHeaderDelegate(self.combo))
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMaxVisibleItems(20)
        self.combo.lineEdit().setPlaceholderText("点开可筛选，也能直接输入模型名")
        self.combo.currentIndexChanged.connect(self._on_index_changed)
        # 回车提交手输的名字：可编辑下拉里敲完直接回车才是主要用法，
        # 只靠 currentIndexChanged 会漏掉"打完之后恰好没触发选中变化"的情况
        self.combo.lineEdit().editingFinished.connect(self._on_index_changed)

        field = QHBoxLayout()
        field.setContentsMargins(0, 0, 0, 0)
        field.setSpacing(12)
        field.addWidget(self.model_label)
        field.addWidget(self.combo, 1)
        self.card.body_layout.addLayout(field)

        # ---- 状态：结论一行 + 细节一行 ------------------------------------
        self.status_main = QLabel("", self)
        self.status_main.setWordWrap(True)
        self.status_detail = QLabel("", self)
        self.status_detail.setProperty("role", "muted")
        self.status_detail.setWordWrap(True)
        self.btn_fix = QPushButton("去配置", self)
        self.btn_fix.setProperty("role", "ghost")
        self.btn_fix.setVisible(False)
        self.btn_fix.clicked.connect(
            lambda: self.fix_requested.emit(self.capability, self._fix_action)
        )

        detail_row = QWidget(self)
        detail_layout = QHBoxLayout(detail_row)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self.status_detail, 1)
        detail_layout.addWidget(self.btn_fix, 0, Qt.AlignmentFlag.AlignTop)

        self.card.body_layout.addWidget(self.status_main)
        self.card.body_layout.addWidget(detail_row)

        self._fix_action = "providers"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.card)

    # ---- 公开面 --------------------------------------------------------

    def fill(self, items: list[ModelChoice], current_key: tuple[str, str, str] | None) -> None:
        """重建下拉，并选中 ``current_key`` 那一项（找不到就不选）。

        重建期间必须屏蔽信号：清空再填充会一路触发 ``currentIndexChanged``，
        而那个槽会写盘——刷新列表不该被当成"用户改主意了"。
        """
        self._loading = True
        self.combo.blockSignals(True)
        self.combo.clear()
        for choice in items:
            self.combo.addItem(choice.label)
            index = self.combo.count() - 1
            self.combo.setItemData(index, choice, Qt.ItemDataRole.UserRole)
            if not choice.selectable:
                self.combo.model().item(index).setEnabled(False)
        target = -1
        if current_key is not None:
            for index, choice in enumerate(items):
                if choice.key == current_key:
                    target = index
                    break
        # 记下这次重建得到的有效选中项：分类标题被选中时退回这里
        self._last_valid_index = target
        self.combo.setCurrentIndex(target)
        self.combo.blockSignals(False)
        self._loading = False

    def items(self) -> list[ModelChoice]:
        return [
            self.combo.itemData(index, Qt.ItemDataRole.UserRole)
            for index in range(self.combo.count())
        ]

    def selected_choice(self) -> ModelChoice | None:
        """当前选中的是清单里的哪一项；手输的名字不算（走 :meth:`manual_text`）。"""
        index = self.combo.currentIndex()
        if index < 0:
            return None
        data = self.combo.itemData(index, Qt.ItemDataRole.UserRole)
        if isinstance(data, ModelChoice) and data.selectable:
            return data
        return None

    def manual_text(self) -> str:
        """用户在框里手打、且与当前选中项不同的文字。

        清单里的项选中后框里显示的就是它的文字；只有当两者不一致时，
        才说明这是用户自己敲进去的名字（方舟 ep-xxx、自建别名等）。

        分组标题（「本地模型」/「云端模型」）与提示行**永远不算手输**：
        它们不是模型名。放它们过去会报出「本地模型 没有归属：先点去配置 Key」
        这种用户完全看不懂的错。
        """
        text = (self.combo.lineEdit().text() or "").strip()
        if not text:
            return ""
        index = self.combo.currentIndex()
        if index >= 0 and text == (self.combo.itemText(index) or "").strip():
            return ""
        for position in range(self.combo.count()):
            if (text == (self.combo.itemText(position) or "").strip()
                    and not self._is_selectable_index(position)):
                return ""
        return text

    def set_placeholder(self, text: str) -> None:
        self.combo.lineEdit().setPlaceholderText(text)

    def set_badge(self, text: str, *, warn: bool = False) -> None:
        self.badge.setText(text)
        self.badge.setProperty("tone", "warn" if warn else "primary")
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def set_status(
        self,
        main: str,
        *,
        tone: str = "ok",
        detail: str = "",
        fix: tuple[str, str] | None = None,
    ) -> None:
        """结论 + 细节。``fix`` = (按钮文案, "providers" | "download" | "envcheck")。"""
        self.status_main.setText(main)
        self.status_main.setProperty("tone", tone)
        self.status_main.style().unpolish(self.status_main)
        self.status_main.style().polish(self.status_main)
        # 卡片标题的色条跟着结论走：语义判断一旦不会执行，用琥珀色说"留意"太轻了，
        # 它是阻断级的事实，色条直接给红。
        self.card.set_accent("success" if tone == "ok" else "danger")
        self.status_detail.setText(detail)
        self.status_detail.setVisible(bool(detail))
        if fix is None:
            self.btn_fix.setVisible(False)
            self._fix_action = "providers"
            return
        text, action = fix
        self._fix_action = action
        self.btn_fix.setText(text)
        self.btn_fix.setVisible(True)

    # ---- 内部 ----------------------------------------------------------

    def _on_index_changed(self, *_args) -> None:
        if self._loading:
            return
        # 分类标题（「本地模型」/「云端模型」）只是分组，不是可选项。一旦它成为
        # 当前项，立刻退回上一次的有效选择。
        #
        # 这里的判断**不能交给视图层**的 setEnabled(False)：实测对禁用项调用
        # setCurrentIndex 照样会成功，那样下拉框会一直显示「本地模型」，而
        # config.json 里的真实选择一点没变——界面显示与实际配置不一致，
        # 比"点错了"严重得多。
        #
        # 注意 index == -1 **不算**误选：那正是"没从清单里挑、正在手输名字"的
        # 正常状态（方舟 ep-xxx、自建别名都走这条路）。只有"指着一个具体的
        # 不可选项"才是误选。
        index = self.combo.currentIndex()
        if index >= 0 and not self._is_selectable_index(index):
            self._revert_to_valid()
            return
        if index >= 0:
            self._last_valid_index = index
        self.choice_picked.emit(self.capability)

    def _is_selectable_index(self, index: int) -> bool:
        """下拉第 ``index`` 项是不是真能选的（分类标题 / 提示行返回 False）。"""
        if index < 0:
            return False
        data = self.combo.itemData(index, Qt.ItemDataRole.UserRole)
        return isinstance(data, ModelChoice) and data.selectable

    def _revert_to_valid(self) -> None:
        """把当前项退回最近一次的可选项；一次都没选过就退回占位提示。"""
        target = self._last_valid_index
        if not self._is_selectable_index(target):
            target = -1
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(target)
        if target < 0:
            # 可编辑下拉在 index 为 -1 时**不会**自己清空输入框，标题文字会
            # 留在框里冒充一个模型名。必须显式清掉，占位提示才会回来。
            self.combo.lineEdit().clear()
        self.combo.blockSignals(False)


class ModelSetupForm(QWidget):
    """语音转写 / 语义判断各选一个模型；本地在最上，云端在下。"""

    #: 用户要配置厂商 Key —— 由对话框转交给主窗口打开「模型管理 → 云端模型」
    configure_providers_requested = Signal()
    #: 用户要下载本地模型 —— 打开「模型管理 → 本地模型」
    download_models_requested = Signal()
    #: 用户要看运行环境（缺转写引擎时唯一有效的下一步：改用源码版）
    env_check_requested = Signal()

    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path=None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        #: 测试注入点：默认写真实 config.json，测试指到临时文件
        self._config_path = config_path
        self._config = AppConfig.load(config_path)
        self._by_id: dict[str, ProviderProfile] = {}
        self._models: dict[str, list[str]] = {}
        self._workers: dict[str, object] = {}
        self._loading_for: set[str] = set()
        #: 能力 → 手输模型名归属的厂商（选过云端项之后才有）
        self._cloud_context: dict[str, str] = {}

        self.header = QLabel(
            "两条能力各选一个模型，本地与云端可以混着用："
            "下面两个下拉，本机已装的排在最上面，云端按厂商分组。选中即生效。",
            self,
        )
        self.header.setWordWrap(True)
        self.header.setProperty("role", "muted")

        self.asr = _CapabilityCard(CAP_ASR, "语音转写", self)
        self.judge = _CapabilityCard(CAP_JUDGE, "语义判断", self)
        self._cards = {CAP_ASR: self.asr, CAP_JUDGE: self.judge}
        for card in (self.asr, self.judge):
            card.choice_picked.connect(self._on_choice_picked)
            card.fix_requested.connect(self._on_fix_requested)

        self.btn_refresh = QPushButton("刷新清单", self)
        self.btn_refresh.setProperty("role", "ghost")
        self.btn_refresh.setToolTip("重新拉取云端厂商的模型清单（本机已安装的模型每次打开都会重扫）")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)

        self.btn_configure = QPushButton("去配置 Key…", self)
        self.btn_configure.setToolTip(
            "打开「模型管理 → 云端模型」填写接口地址与 API Key，并测试连通性"
        )
        self.btn_configure.clicked.connect(self.configure_providers_requested.emit)

        self.hint_label = QLabel("", self)
        self.hint_label.setProperty("role", "muted")

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addWidget(self.btn_refresh)
        footer.addWidget(self.btn_configure)
        footer.addStretch(1)
        footer.addWidget(self.hint_label, 1)

        inner = QWidget(self)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.header)
        layout.addWidget(self.asr)
        layout.addWidget(self.judge)
        layout.addLayout(footer)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scrollable(inner))

        self.refresh()

    # ---- 公开面 --------------------------------------------------------

    def cards(self) -> tuple[_CapabilityCard, ...]:
        return (self.asr, self.judge)

    def refresh(self) -> None:
        """重新读盘 → 重建两个下拉。配置可能被别的窗口改过，每次打开都要重读。"""
        self._config = AppConfig.load(self._config_path)
        self._by_id = {p.id: p for p in self._config.profiles().all()}
        self.hint_label.setText("")
        for card in self.cards():
            self._fill(card)
            self._refresh_status(card)
        self._fetch_keyed_providers()

    def shutdown(self) -> None:
        for worker in list(self._workers.values()):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        self._workers.clear()

    # ---- 下拉内容 ------------------------------------------------------

    def _fill(self, card: _CapabilityCard) -> None:
        capability = card.capability
        items = self._local_items(capability)
        items.append(ModelChoice(KIND_HEADER, "云端模型", selectable=False))
        items.extend(self._cloud_items(capability))
        card.fill(items, self._current_key(capability))
        card.set_placeholder(self._local_empty_hint(capability))

    def _local_items(self, capability: str) -> list[ModelChoice]:
        """本地组：只列**已安装**的档位；配置指向但没装的补一行标注。

        不列未安装的项，是为了让下拉里不存在"选了却用不了"的条目——
        要装就去「模型管理 → 本地模型」，那里本来就有下载和体积说明。
        """
        root = self._config.resolved_models_dir()
        if capability == CAP_ASR:
            installed = set(installed_whisper_tiers(root))
            rows = [
                ModelChoice(KIND_LOCAL, f"whisper {tier} — {note}", tier)
                for tier, _mb, note in WHISPER_TIERS
                if tier in installed
            ]
        else:
            rows = [
                ModelChoice(KIND_LOCAL, f"{spec.label}　{spec.note}".strip(), spec.key)
                for spec in installed_llm_specs(root)
            ]

        current = str(getattr(self._config, _LOCAL_FIELD[capability], "") or "")
        if current and not any(row.value == current for row in rows):
            rows.append(
                ModelChoice(
                    KIND_LOCAL,
                    f"{_local_display(current, capability)}（未安装）",
                    current,
                )
            )
        return [ModelChoice(KIND_HEADER, "本地模型", selectable=False), *rows]

    def _local_ready(self, capability: str) -> bool:
        """配置指向的本地模型此刻是不是真的能用。

        语音转写这一档**必须同时**满足两件事：模型文件在，且转写引擎
        （faster-whisper）可用。只看模型是不够的——打包版有意不含
        faster-whisper（体积），此时模型下得再齐，转写一次也跑不了。
        原先只查模型，于是界面给下了模型、却跑不了转写的用户打一句绿色的
        "正在使用 本地 whisper small"，与分析页/环境自检的口径自相矛盾。
        """
        value = str(getattr(self._config, _LOCAL_FIELD[capability], "") or "")
        if not value:
            return False
        if capability == CAP_ASR and not self._asr_engine_available():
            return False
        root = self._config.resolved_models_dir()
        if capability == CAP_ASR:
            return value in set(installed_whisper_tiers(root))
        return any(spec.key == value for spec in installed_llm_specs(root))

    def _asr_engine_available(self) -> bool:
        """转写引擎（faster-whisper）此刻是否可导入。

        复用环境自检的 ``_module_available``（用 find_spec 探测，不真正加载
        模块顶层）。做成实例方法是为了测试能打桩——测试机通常没装
        faster-whisper，不打桩就永远测不到"引擎可用"这条分支。
        """
        from ...core.env_check import _module_available

        return _module_available("faster_whisper")

    def _asr_model_present(self) -> bool:
        """语音转写的**模型文件**是否就位（不看引擎）。用于区分两种失败。"""
        value = str(getattr(self._config, "asr_model_tier", "") or "")
        if not value:
            return False
        root = self._config.resolved_models_dir()
        return value in set(installed_whisper_tiers(root))


    def _local_count(self, capability: str) -> int:
        """本机真正装了几个本地模型（"未安装"的补项不计入）。"""
        root = self._config.resolved_models_dir()
        if capability == CAP_ASR:
            return len(installed_whisper_tiers(root))
        return len(installed_llm_specs(root))

    def _local_empty_hint(self, capability: str) -> str:
        if self._local_count(capability):
            return "点开可筛选，也能直接输入模型名"
        name = "转写模型" if capability == CAP_ASR else "语义模型"
        return f"本机未安装{name} — 去「模型管理 → 本地模型」下载"

    def _cloud_items(self, capability: str) -> list[ModelChoice]:
        """云端组：按厂商分子组，当前生效的那家排最前。

        **只有配了 Key 的厂商才单独成组**，其余合并成一条提示。16 家全列会把这个
        下拉毁掉——每家可能上百条模型，铺平之后谁也找不到自己要的那一个。
        合并成一条而不是隐藏，是因为"看不见"会被读成"界面坏了"。
        """
        backend = self._config.backend_for(capability)
        field = _MODEL_FIELD[capability]
        keyed = [p for p in self._by_id.values() if p.resolved_api_key()]
        keyed.sort(key=lambda p: 0 if p.id == backend else 1)

        items: list[ModelChoice] = []
        for profile in keyed:
            mark = "　当前生效" if profile.id == backend else ""
            items.append(
                ModelChoice(KIND_HEADER, f"{profile.label}{mark}", selectable=False)
            )
            if profile.id in self._loading_for:
                items.append(
                    ModelChoice(KIND_NOTE, "　正在拉取模型清单…", selectable=False)
                )
                continue
            fetched = self._models.get(profile.id, [])
            chosen = str(getattr(profile, field, "") or "")
            names = merge_model_choices(fetched, [chosen] if chosen else [])
            if not names:
                reason = _short_block(profile, capability) or "该厂商没给出可用模型"
                items.append(
                    ModelChoice(KIND_NOTE, f"　{reason}，可直接输入模型名", selectable=False)
                )
                continue
            for name in names:
                items.append(ModelChoice(KIND_CLOUD, f"　{name}", name, profile.id))

        others = [p for p in self._by_id.values() if not p.resolved_api_key()]
        if others:
            items.append(
                ModelChoice(
                    KIND_NOTE,
                    f"另有 {len(others)} 家厂商未配置 Key —— 见下方「去配置 Key…」",
                    selectable=False,
                )
            )
        return items

    def _current_key(self, capability: str) -> tuple[str, str, str] | None:
        backend = self._config.backend_for(capability)
        if backend == LOCAL_BACKEND:
            value = str(getattr(self._config, _LOCAL_FIELD[capability], "") or "")
            return (KIND_LOCAL, value, "") if value else None
        profile = self._by_id.get(backend)
        if profile is None:
            return None
        value = str(getattr(profile, _MODEL_FIELD[capability], "") or "")
        return (KIND_CLOUD, value, backend) if value else None

    # ---- 状态 ----------------------------------------------------------

    def _refresh_status(self, card: _CapabilityCard) -> None:
        """第一行给**结论**（此刻到底跑什么），第二行才说差哪一项、怎么修。

        分支只有三条：云端能用 / 落到本地能用 / **落到本地也不能用**。
        最后那条最要紧——原先的界面从不直说"这条能力根本不会执行"，
        而用户是看着界面判断能不能开工的。
        """
        capability = card.capability
        label = CAP_LABELS[capability]
        backend = self._config.backend_for(capability)

        cloud = self._by_id.get(backend) if backend != LOCAL_BACKEND else None
        if backend != LOCAL_BACKEND and cloud is None:
            cloud_why = f"配置里选的厂商「{backend}」已经不存在了。"
        elif cloud is not None:
            reason = block_reason(cloud, capability)
            cloud_why = f"但尚未生效：{reason}。" if reason else ""
        else:
            cloud_why = ""

        if cloud is not None and not cloud_why:
            model = str(getattr(cloud, _MODEL_FIELD[capability], "") or "")
            card.set_badge("云端")
            card.set_status(f"正在使用 云端 {cloud.label} 的 {model}。")
            return

        # 语音转写专有的一种处境：**模型下好了，但转写引擎还没装**。
        # 必须单独说，且要早于下面的通用"未就绪"分支——否则用户会看到
        # "本机没有安装可用的模型，去下载"，而去下载根本解决不了缺引擎的问题
        # （他已经下过了）。
        #
        # 现在引擎**可以应用内安装**（见 deps_manager），所以出口是"去自检"
        # 而不是旧文案里的"改用源码版"——那已经不是唯一选择了。
        if capability == CAP_ASR and self._asr_model_present() and not self._asr_engine_available():
            card.set_badge("本地", warn=True)
            card.set_status(
                f"{label}不会执行：还没装转写引擎。",
                tone="warn",
                detail=(
                    "模型已就绪，但转写引擎（faster-whisper）不在安装包里。"
                    "到「环境自检 → 安装转写引擎」可以应用内一键安装"
                    "（约 80 MB，不需要 pip 或编译器），装好即可转写。"
                ),
                fix=("去自检", "envcheck"),
            )
            return

        if not self._local_ready(capability):
            card.set_badge("本地", warn=True)
            # 「装了但没选」和「压根没装」是两回事：前者只是没动手，后者要先去下载。
            # 混成一句"不会执行"会让本机明明有模型的人以为自己缺东西。
            if self._local_count(capability) and not self._local_value(capability):
                count = self._local_count(capability)
                name = "转写模型" if capability == CAP_ASR else "语义模型"
                card.set_status(
                    f"{label}还没选模型。",
                    tone="warn",
                    detail=f"本机已经装了 {count} 个{name}，在上方下拉里选一个即可。",
                )
                return
            if cloud is not None:
                detail = f"本机没有装模型；已选云端 {cloud.label}，{cloud_why}"
                fix = ("去配置", "providers")
            else:
                detail = "本机没有安装可用的模型，去「模型管理 → 本地模型」下载。"
                fix = ("去下载", "download")
            card.set_status(f"{label}不会执行。", tone="warn", detail=detail, fix=fix)
            return

        card.set_badge("本地", warn=bool(cloud_why))
        if cloud_why:
            where = f"已选云端 {cloud.label}，" if cloud is not None else ""
            card.set_status(
                f"{label}会退回本地。",
                tone="warn",
                detail=f"{where}{cloud_why}",
                fix=("去配置", "providers"),
            )
            return

        card.set_status(
            f"正在使用 本地 {_local_display(self._local_value(capability), capability)}。"
        )

    def _local_value(self, capability: str) -> str:
        return str(getattr(self._config, _LOCAL_FIELD[capability], "") or "")

    # ---- 选择即生效 -----------------------------------------------------

    def _on_choice_picked(self, capability: str) -> None:
        card = self._cards[capability]
        choice = card.selected_choice()
        if choice is not None:
            if choice.kind == KIND_CLOUD:
                self._cloud_context[capability] = choice.provider_id
            self._apply(choice.kind, choice.value, choice.provider_id, capability)
            return
        # 清单没选中 → 看看是不是手打了名字
        typed = card.manual_text()
        if typed:
            self._apply_manual(typed, capability)

    def _apply_manual(self, typed: str, capability: str) -> None:
        provider_id = self._manual_provider(capability)
        if provider_id is None:
            keyed = [p for p in self._by_id.values() if p.resolved_api_key()]
            why = (
                "先在上方选一个该厂商的模型，才能确定这个名字归谁"
                if keyed
                else "本机一个云端厂商都还没配 Key，先点右下「去配置 Key…」"
            )
            self.hint_label.setText(f"「{typed}」没有归属：{why}。")
            self._fill(self._cards[capability])
            return
        self.hint_label.setText("")
        self._apply(KIND_CLOUD, typed, provider_id, capability)

    def _manual_provider(self, capability: str) -> str | None:
        """手输的名字挂到哪家厂商——只在能确定时给答案。

        一个框里可能同时躺着好几家的模型，随便挑一个会把模型名写进错误的档案，
        而且用户很难察觉（下次分析才发现走了错的模型）。所以：先看该能力刚选过
        哪家云端模型，再退而求其次用全局唯一一家配了 Key 的厂商。
        """
        context = self._cloud_context.get(capability)
        if context and context in self._by_id:
            return context
        keyed = [p.id for p in self._by_id.values() if p.resolved_api_key()]
        return keyed[0] if len(keyed) == 1 else None

    def _apply(
        self, kind: str, value: str, provider_id: str, capability: str
    ) -> None:
        """把一次选择写进 config.json，然后刷新状态与下拉（● 与「当前生效」标记）。"""
        if not value:
            return
        prefs = dict(self._config.backend_prefs)
        providers = [dict(item) for item in self._config.providers]

        if kind == KIND_LOCAL:
            if (
                prefs.get(capability) == LOCAL_BACKEND
                and str(getattr(self._config, _LOCAL_FIELD[capability], "") or "") == value
            ):
                return          # 已经是这个选择了，别再写一遍盘
            setattr(self._config, _LOCAL_FIELD[capability], value)
            prefs[capability] = LOCAL_BACKEND
        else:
            profile = self._by_id.get(provider_id)
            if profile is None:
                return
            current = str(getattr(profile, _MODEL_FIELD[capability], "") or "")
            if prefs.get(capability) == provider_id and current == value:
                return          # 同上
            updated = profile.with_updates(**{_MODEL_FIELD[capability]: value})
            self._by_id[updated.id] = updated
            providers = _merge_provider(providers, updated)
            prefs[capability] = updated.id

        self._config.backend_prefs = prefs
        self._config.providers = providers
        try:
            self._config.save(self._config_path)
        except OSError as exc:
            self.hint_label.setText(f"配置写入失败：{exc}")
            return

        self.hint_label.setText(
            f"已生效：{CAP_LABELS[capability]} → "
            + (_local_display(value, capability) if kind == KIND_LOCAL else f"云端 {value}")
        )
        card = self._cards[capability]
        self._fill(card)
        self._refresh_status(card)

    def _on_fix_requested(self, capability: str, action: str) -> None:
        if action == "download":
            self.download_models_requested.emit()
        elif action == "envcheck":
            # 缺转写引擎时，唯一有效的下一步是看环境自检（那里会说明该改用
            # 源码版）。**刻意不落到 providers**：把"缺引擎"的人引到厂商配置
            # 页，等于让他白忙一场。
            self.env_check_requested.emit()
        else:
            self.configure_providers_requested.emit()

    def _on_refresh_clicked(self) -> None:
        self._models.clear()
        self._loading_for.clear()
        self.refresh()

    # ---- 云端清单（多厂商并发） ------------------------------------------

    def _fetch_keyed_providers(self) -> None:
        """开窗就把配了 Key 的厂商清单都拉一遍。

        统一下拉必须在**展开之前**就有内容，否则第一次点开是空的。
        只拉有 Key 的：没凭据去打网络必然 401，还要白等一个超时。
        """
        for profile in self._by_id.values():
            if not profile.resolved_api_key():
                continue
            if profile.id in self._models or profile.id in self._loading_for:
                continue
            self._loading_for.add(profile.id)
            worker = ModelListWorker(profile, parent=self)
            pid = profile.id
            worker.listed.connect(lambda models, key=pid: self._on_listed(key, models))
            worker.failed.connect(
                lambda reason, key=pid: self._on_list_failed(key, reason)
            )
            worker.finished_ok.connect(lambda key=pid: self._on_list_done(key))
            # 按厂商分键登记：主窗口禁止同类任务并发，单键会让几家互相顶掉
            register_worker(self, f"model_list::{pid}", worker)
            self._workers[pid] = worker
            worker.start()

    def _on_listed(self, provider_id: str, models) -> None:
        self._models[provider_id] = [
            str(item) for item in (models or []) if str(item).strip()
        ]
        self._repopulate()

    def _on_list_failed(self, provider_id: str, reason: str) -> None:
        self._models[provider_id] = []
        self.hint_label.setText(f"{self._label_of(provider_id)} 的清单没拉到：{reason}")
        self._repopulate()

    def _on_list_done(self, provider_id: str) -> None:
        self._loading_for.discard(provider_id)
        self._workers.pop(provider_id, None)
        self._repopulate()

    def _repopulate(self) -> None:
        """谁先回来先刷新，不必等最慢的一家。"""
        for card in self.cards():
            self._fill(card)
            self._refresh_status(card)

    def _label_of(self, provider_id: str) -> str:
        profile = self._by_id.get(provider_id)
        return profile.label if profile is not None else provider_id


def _short_block(profile: ProviderProfile, capability: str) -> str:
    """厂商分组头行尾的**短**标记（完整原因留给状态行）。

    不能直接用 ``block_reason``：它第一句就是"档案未启用"，于是十几家厂商会
    顶着同一句话挤满下拉——而用户真正想知道的是"这家我缺什么"。按"缺什么"分类
    给几个词，下拉才读得出差别。
    """
    if not profile.enabled:
        return "档案未启用"
    if not profile.allow_upload:
        return "未允许外发素材"
    if not profile.resolved_api_key():
        return "无 Key"
    return ""


def _merge_provider(items: list[dict], profile: ProviderProfile) -> list[dict]:
    """按 id 合并一份档案（与两个表单同一套写法）。"""
    for index, item in enumerate(items):
        if str(item.get("id")) == profile.id:
            items[index] = profile.to_json()
            return items
    items.append(profile.to_json())
    return items


def _local_display(value: str, capability: str) -> str:
    """本地档位/模型名的人话（配置里存的是 key）。"""
    if not value:
        return "（未选）"
    if capability == CAP_ASR:
        return f"whisper {value}"
    spec = spec_by_key(value)
    return spec.label if spec is not None else value
