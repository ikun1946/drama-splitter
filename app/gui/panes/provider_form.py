"""厂商表单：「模型管理 → 云端厂商」页签（列表 + 编辑区 + 模型表格 + 能力开关）。

一个界面管一家厂商的全部
------------------------
Key、外发许可、能力开关、模型名原先分散在「模型管理」的云端卡片与独立的
「厂商管理」对话框里——同一份档案两处都能改，用户实测就撞上了。现在全部收在
这一页：左列表选厂商，右侧填字段、看/选模型（`/models` 自动刷新 + 手填可记住）、
勾能力并「保存并测试连通性」。

几条硬规则
----------
- **内置厂商不可删除、id 不可改**（它们在代码里有默认值），但可以改字段；
  改完想还原就「恢复默认」（从配置里删掉该 id 的覆盖项）。
- **编辑内置 = 同 id 覆盖**：`merge_profiles` 本来就以 id 为键合并，
  `backend_prefs` 里存的也是 id，因此覆盖不会让"已选中的后端"失效。
- **删除/恢复默认必须连带处理引用**：把 `backend_prefs` 里指向它的能力改回本地，
  否则用户只会看到"云端静默失效"而不知道为什么。
- **测通才允许启用**：勾了能力必须点「保存并测试连通性」，通过的才切后端。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import LOCAL_BACKEND, AppConfig
from ...core.cloud import build_asr_transport, build_judge_transport
from ...core.model_info import (
    MODALITIES,
    MODALITY_LABELS,
    REASONING_LEVELS,
    ModelInfo,
    format_context,
    infer_model_info,
)
from ...core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    CAP_LABELS,
    DEFAULT_PROFILES,
    ENV_KEY_TEMPLATE,
    PROTOCOLS_BY_CAP,
    PROTO_DISABLED,
    PROTO_LABELS,
    PROTO_OPENAI,
    PROTO_VOLC_ARK,
    ProviderError,
    ProviderProfile,
    describe_egress,
    merge_model_choices,
    missing_config_reason,
    validate_profile,
)
from ..state import ProjectState
from ..widgets import (
    Card,
    ElidingItemDelegate,
    LabeledField,
    ModelTable,
    NoWheelComboBox,
    NoWheelSpinBox,
    scrollable,
)
from ..widgets import message
from ..workers import ModelListWorker
from ._common import register_worker

__all__ = ["ProviderForm"]


class ProviderForm(QWidget):
    """左栏列表 + 右栏编辑器；纯逻辑可脱离对话框单测。"""

    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path=None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.setMinimumWidth(760)
        self._config = AppConfig.load(config_path)
        #: 测试注入点：默认写真实 config.json，测试指到临时文件
        self._config_path = config_path
        self._profile_id: str | None = None
        self._loading = False
        self._models_worker = None
        #: (base_url, key) → 模型清单。同一家不重复拉：切换厂商来回点时不该反复打网络
        self._model_cache: dict[tuple[str, str], list[str]] = {}
        #: 两条能力当前指派的模型名 + 用户手填的模型清单。模型表格只发信号，
        #: 真值存在这里；与其它字段一样，等「保存」才写进配置。
        self._judge_model = ""
        self._asr_model = ""
        self._custom_models: list[str] = []
        #: 最近一次从接口拉到的清单（表格显示的是它 + 手填的合并结果）
        self._fetched_models: list[str] = []
        #: 每个模型的参数档案（上下文窗口 / 思考等级 / 模态）。与其它字段一样，
        #: 只改内存，等「保存」才写进配置。
        self._model_info: list[ModelInfo] = []
        #: 回填参数面板时的护栏：挡住控件自己发出来的信号（见 `_refresh_info_panel`）
        self._info_syncing = False

        # ---- 左栏：列表与动作 ------------------------------------------
        self.profile_list = QListWidget(self)
        # 条目要**省略**而不是被裁：「阿里云百炼（通义千问）（内置）」单行要 499px，
        # 而这一栏只有 220px。QListWidget 自己不省略号，拉宽这一栏又会把右侧
        # 编辑区挤没，所以用一个只改"画什么"的委托，原始文案仍在 item 里，
        # 悬停由委托给出全文。
        self.profile_list.setItemDelegate(ElidingItemDelegate(self.profile_list))
        self.profile_list.currentItemChanged.connect(self._on_selected)
        self.profile_list.setMinimumWidth(220)
        self.btn_new = QPushButton("新增")
        self.btn_duplicate = QPushButton("复制为自定义")
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setProperty("role", "danger")
        self.btn_restore = QPushButton("恢复默认")
        self.btn_new.clicked.connect(self._on_new)
        self.btn_duplicate.clicked.connect(self._on_duplicate)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_restore.clicked.connect(self._on_restore)
        for button in (self.btn_delete, self.btn_restore):
            button.style().unpolish(button)
            button.style().polish(button)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        left.addWidget(self.profile_list, 1)
        # 四个动作排成 2×2：占的宽度与列表一致，比一列四行更紧凑
        pairs = QGridLayout()
        pairs.setContentsMargins(0, 0, 0, 0)
        pairs.setHorizontalSpacing(8)
        pairs.setVerticalSpacing(6)
        for index, button in enumerate(
            (self.btn_new, self.btn_duplicate, self.btn_delete, self.btn_restore)
        ):
            pairs.addWidget(button, index // 2, index % 2)
        left.addLayout(pairs)
        left_host = QWidget(self)
        left_host.setLayout(left)
        left_host.setFixedWidth(240)

        # ---- 右栏：字段编辑器 ------------------------------------------
        self.edit_id = QLineEdit(self)
        self.edit_label = QLineEdit(self)
        self.edit_base_url = QLineEdit(self)
        self.edit_judge_protocol = NoWheelComboBox(self)
        self.edit_asr_protocol = NoWheelComboBox(self)
        # 模型名不再两个下拉各填一遍：下面那张表格一次列出该厂商的全部模型
        # （接口拉回来的 + 手填的），行内点「设为语义判断 / 设为转写」即可指派
        self.model_table = ModelTable(self)
        self.edit_asr_endpoint = QLineEdit(self)
        self.edit_asr_resource = QLineEdit(self)
        self.edit_api_key = QLineEdit(self)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.allow_upload = QCheckBox("允许外发素材", self)
        self.allow_upload.setToolTip(
            "转写＝整条音轨；语义判断＝候选点前后台词文本（不含画面）。"
            "不勾选则该项能力零外发。"
        )
        self.use_system_proxy = QCheckBox("走系统代理（默认绕过，本地端点必须绕过）", self)
        for combo, capability in (
            (self.edit_judge_protocol, "judge"),
            (self.edit_asr_protocol, "asr"),
        ):
            for protocol in PROTOCOLS_BY_CAP[capability]:
                combo.addItem(PROTO_LABELS[protocol], protocol)
            combo.currentIndexChanged.connect(self._refresh_field_visibility)
        self.edit_id.textChanged.connect(self._refresh_field_visibility)
        # 填完地址或 Key 失焦时自动拉模型列表：这是"我配好了"最自然的时刻
        self.edit_base_url.editingFinished.connect(self._request_models)
        self.edit_api_key.editingFinished.connect(self._request_models)

        label_field = LabeledField("名称（界面上显示）")
        label_field.attach(self.edit_label)
        self.base_field = LabeledField("接口地址（base_url，OpenAI 兼容要带 /v1）")
        self.base_field.attach(self.edit_base_url)
        self.judge_protocol_field = LabeledField("语义判断协议")
        self.judge_protocol_field.attach(self.edit_judge_protocol)
        self.asr_protocol_field = LabeledField("转写协议")
        self.asr_protocol_field.attach(self.edit_asr_protocol)
        self.asr_endpoint_field = LabeledField("火山转写接口（asr_endpoint）")
        self.asr_endpoint_field.attach(self.edit_asr_endpoint)
        self.asr_resource_field = LabeledField("火山资源 ID（asr_resource_id）")
        self.asr_resource_field.attach(self.edit_asr_resource)
        self.key_field = LabeledField("API Key")
        self.key_field.attach(self.edit_api_key)

        # 「刷新模型列表」：`GET {base_url}/models`；表格里的"自动"行就是它拉回来的。
        # 指派 / 添加 / 删除都只改内存，跟其它字段一样等「保存」写入配置。
        self.model_table.assigned.connect(self._on_model_assigned)
        self.model_table.added.connect(self._on_model_added)
        self.model_table.removed.connect(self._on_model_removed)
        self.model_table.list.selection_changed.connect(self._on_model_selected)

        # ---- 每个模型的参数（上下文窗口 / 思考等级 / 模态）------------------
        #
        # 放在表格**下面**而不是每行一组输入框：厂商的清单动辄几百条，逐行挂控件
        # 会让列表没法看（这正是旧版表格被抱怨的地方）。改成"选中哪个就配哪个"，
        # 面板只服务当前选中项。
        #
        # 三项都遵循"留空 = 不干预"（见 core/model_info.py）：窗口 0 不截断、
        # 思考等级空则请求里不带 reasoning_effort、模态全不勾则不做能力过滤。
        # 所以自动填充只填有把握的，猜不出的留空——填错会直接把能用的模型弄坏。
        self.info_title = QLabel("模型参数", self)
        self.info_title.setProperty("role", "muted")
        self.info_title.setWordWrap(True)

        self.info_context = NoWheelSpinBox(self)
        self.info_context.setRange(0, 100_000_000)
        self.info_context.setSingleStep(1_000)
        self.info_context.setSpecialValueText("未知（不截断）")
        self.info_context.setSuffix(" token")
        self.info_context.setToolTip(
            "模型一次能吃下的最大 token 数。留 0 表示不限制（不截断）。"
            "发送前会按它裁剪输入，并为回复预留四分之一窗口。"
        )
        context_field = LabeledField("上下文窗口")
        context_field.attach(self.info_context)

        self.info_reasoning = NoWheelComboBox(self)
        self.info_reasoning.addItem("不发送", "")
        for level in REASONING_LEVELS:
            self.info_reasoning.addItem(level, level)
        self.info_reasoning.setToolTip(
            "按这个值发送 reasoning_effort。留「不发送」则请求里不带该参数——"
            "不少模型不认它，盲传会直接报错。"
        )
        reasoning_field = LabeledField("思考等级")
        reasoning_field.attach(self.info_reasoning)

        self.info_modalities: dict[str, QCheckBox] = {}
        modality_row = QWidget(self)
        modality_layout = QHBoxLayout(modality_row)
        modality_layout.setContentsMargins(0, 0, 0, 0)
        modality_layout.setSpacing(10)
        for key in MODALITIES:
            box = QCheckBox(MODALITY_LABELS[key], modality_row)
            box.setToolTip(
                {
                    "text": "不勾任何一项 = 未知，不做能力过滤（保持原行为）",
                    "image": "勾上后这个模型不能用于语音转写",
                    "audio": "勾上后这个模型不能用于语义判断",
                }[key]
            )
            modality_layout.addWidget(box)
            self.info_modalities[key] = box
        modality_layout.addStretch(1)
        modality_field = LabeledField("模态")
        modality_field.attach(modality_row)

        self.btn_autofill = QPushButton("自动填充", self)
        self.btn_autofill.setToolTip(
            "按模型名推断上下文窗口与模态（内置常见模型知识库 + 名称模式）。"
            "推断值是估计值，确认无误再保存。"
        )
        self.btn_autofill.clicked.connect(self._on_autofill_info)
        self.btn_clear_info = QPushButton("清除参数", self)
        self.btn_clear_info.setToolTip("把这个模型的参数恢复成「未设置」，不影响其余模型")
        self.btn_clear_info.clicked.connect(self._on_clear_info)
        self.info_note = QLabel("", self)
        self.info_note.setProperty("role", "muted")
        self.info_note.setWordWrap(True)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(12)
        info_row.addWidget(context_field)
        info_row.addWidget(reasoning_field)
        info_row.addWidget(modality_field, 1)
        info_actions = QHBoxLayout()
        info_actions.setContentsMargins(0, 0, 0, 0)
        info_actions.setSpacing(8)
        info_actions.addWidget(self.btn_autofill)
        info_actions.addWidget(self.btn_clear_info)
        info_actions.addStretch(1)
        info_row.addLayout(info_actions)

        # 改任何一项都立刻写回内存（表格里的标记同步刷新），但**不落盘**——
        # 与其它字段一致，等「保存」才进 config.json
        self.info_context.valueChanged.connect(self._on_info_edited)
        self.info_reasoning.currentIndexChanged.connect(self._on_info_edited)
        for _key, _box in self.info_modalities.items():
            _box.toggled.connect(self._on_info_edited)

        # ---- 云端能力开关（合并自原「模型管理」的云端卡片）------------------
        #
        # 为什么搬到厂商这里：Key、外发许可、能力开关都是"这家厂商怎么被使用"，
        # 拆在两个对话框里等于同一份档案两处都能改（用户实测就是这么撞上的）。
        # 排成一行而不是分组框：两个复选框 + 一个按钮的信息量不值得多占三行高度，
        # 外发/上传范围写进各自的 tooltip（勾选那一刻就能读到）。
        self.use_judge = QCheckBox("语义判断使用云端", self)
        self.use_judge.setToolTip("只发候选点前后台词文本（不含画面）")
        self.use_asr = QCheckBox("语音转写使用云端", self)
        self.use_asr.setToolTip("整条音轨分块上传，约 10 分钟/块")
        self.btn_probe = QPushButton("保存并测试连通性", self)
        self.btn_probe.setToolTip(
            "保存本页字段，并对勾选的能力各发一次连通性请求；只有测通的那条才会切到云端"
        )
        self.btn_probe.clicked.connect(self._save_and_probe)
        self.cloud_hint = QLabel("", self)
        self.cloud_hint.setWordWrap(True)
        self.cloud_hint.setProperty("role", "muted")
        ability_row = QHBoxLayout()
        ability_row.setContentsMargins(0, 0, 0, 0)
        ability_row.setSpacing(12)
        ability_row.addWidget(self.use_judge)
        ability_row.addWidget(self.use_asr)
        ability_row.addStretch(1)
        ability_row.addWidget(self.btn_probe)
        self.btn_refresh_models = QPushButton("刷新模型列表", self)
        self.btn_refresh_models.clicked.connect(lambda: self._request_models(force=True))
        self.model_note = QLabel("", self)
        self.model_note.setWordWrap(True)
        self.model_note.setProperty("role", "muted")
        models_row = QHBoxLayout()
        models_row.setContentsMargins(0, 0, 0, 0)
        models_row.setSpacing(8)
        models_row.addWidget(self.btn_refresh_models)
        models_row.addWidget(self.model_note, 1)

        def pair(left, right) -> QHBoxLayout:
            """两个字段并成一排：常用字段首屏就能看完，不必滚动。"""
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            row.addWidget(left, 1)
            row.addWidget(right, 1)
            return row

        # 火山转写的两个字段：只在火山协议下出现（与接口地址一起收进高级区）
        self.volcano_host = QWidget(self)
        volcano = QVBoxLayout(self.volcano_host)
        volcano.setContentsMargins(0, 0, 0, 0)
        volcano.setSpacing(4)
        volcano.addLayout(pair(self.asr_endpoint_field, self.asr_resource_field))
        self.volcano_host.setVisible(False)

        # ---- 高级字段：默认收起 ------------------------------------------
        #
        # 常用区只留「填 Key + 加模型」：接口地址、协议、火山专用字段、id、代理
        # 绝大多数人一辈子不动，逐项摊在首屏反而让人以为每个都得填。收进这里，
        # 需要时勾一下展开。
        #
        # 有意**不设例外**：早期版本把"必填项不许折叠"当规矩，于是接口地址与
        # 火山 endpoint 一直占着首屏。真正会卡住保存的情况由校验给出明确报错，
        # 且 `_validated_draft()` 会自动把本区展开——用户不会"看不到却必须填"。
        # 文案必须短：QCheckBox 的文字是**单行不换行**的，它的 minimumSizeHint 就是
        # 整行文字宽度。原来那句「显示高级字段（接口地址 / 协议 / 火山专用 / id / 代理）」
        # 要 598px，把整个表单的最小宽度顶到 768px > 视口 698px，于是保存按钮那一
        # 栏被切掉。枚举内容移到 tooltip。
        self.advanced_toggle = QCheckBox("显示高级字段", self)
        self.advanced_toggle.setToolTip(
            "接口地址 / 协议 / 火山专用字段 / 厂商 id / 代理设置"
        )
        self.advanced_toggle.toggled.connect(self._refresh_field_visibility)
        id_field = LabeledField("id（小写字母/数字/-/_，保存后不可改内置）")
        id_field.attach(self.edit_id)
        self.advanced_host = QWidget(self)
        advanced = QVBoxLayout(self.advanced_host)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(6)
        advanced.addWidget(self.base_field)
        advanced.addLayout(pair(self.judge_protocol_field, self.asr_protocol_field))
        advanced.addWidget(self.volcano_host)
        advanced.addWidget(id_field)
        advanced.addWidget(self.use_system_proxy)
        self.advanced_host.setVisible(False)

        self.egress_preview = QLabel(self)
        self.egress_preview.setWordWrap(True)
        self.egress_preview.setProperty("role", "muted")
        self.hint_label = QLabel(self)
        self.hint_label.setWordWrap(True)
        self.hint_label.setProperty("role", "muted")

        self.btn_save = QPushButton("保存", self)
        self.btn_save.setProperty("role", "primary")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_revert = QPushButton("放弃改动", self)
        self.btn_revert.clicked.connect(lambda: self._load_profile(self._profile_id))

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.btn_save)
        actions.addWidget(self.btn_revert)
        actions.addWidget(self.advanced_toggle)
        # 动作排在卡片标题栏（右上角）而不是表单末尾：正文比一屏长时需要滚动，
        # 把「保存」放在文末等于每次改完都要先滚到底
        self.actions_host = QWidget(self)
        self.actions_host.setLayout(actions)

        editor_card = Card("档案字段", parent=self)
        editor_card.add_header_widget(self.actions_host)
        editor_card.body_layout.addWidget(label_field)
        # Key 与「允许外发」并排：两者都是"这一家怎么被使用"的开关
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(12)
        key_row.addWidget(self.key_field, 2)
        key_row.addWidget(self.allow_upload, 1, Qt.AlignmentFlag.AlignBottom)
        editor_card.body_layout.addLayout(key_row)
        editor_card.body_layout.addLayout(ability_row)
        # 结果提示紧跟在「保存并测试连通性」按钮下面，而不是表单末尾。
        # 放在末尾时，用户点了按钮、结果写进了折叠线以下——**看上去就是"点了没反应"**。
        # 这一页正文比一屏长，滚动区之外的消息等于没有消息。
        self.hint_label.setVisible(False)
        editor_card.body_layout.addWidget(self.hint_label)
        editor_card.body_layout.addWidget(self.cloud_hint)
        # 模型清单：它可能很长（自带滚动），但排在配置项之后
        models_title = QLabel("模型（该厂商 /models 拉回来的 + 你手填的）", editor_card)
        models_title.setProperty("role", "muted")
        # QLabel 默认单行不换行，minimumSizeHint 即整行文字宽度——那是硬下限，
        # 布局压不下去。这几处长说明文字以前把厂商表单顶宽到超出对话框视口。
        models_title.setWordWrap(True)
        editor_card.body_layout.addWidget(models_title)
        editor_card.body_layout.addWidget(self.model_table)
        editor_card.body_layout.addLayout(models_row)
        editor_card.body_layout.addWidget(self.info_title)
        editor_card.body_layout.addLayout(info_row)
        editor_card.body_layout.addWidget(self.info_note)
        editor_card.body_layout.addWidget(self.advanced_host)
        editor_card.body_layout.addWidget(self.egress_preview)

        right_host = scrollable(editor_card)
        right_host.setMinimumWidth(480)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(left_host)
        outer.addWidget(right_host, 1)

        self.reload()

    # ---- 公开面（工具与测试用）------------------------------------------

    def reload(self, select: str | None = None) -> None:
        """重新读盘 → 重建列表 → 选中 ``select``（默认沿用当前选中）。

        每次都从磁盘读：这份表单不是配置的唯一编辑者（模型页的云端卡片也会写同一
        份 config.json），拿着构造时的旧副本重建列表会显示过期数据。
        """
        self._config = AppConfig.load(self._config_path)
        wanted = select or self._profile_id
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self._config.profiles().all():
            item = QListWidgetItem(
                profile.label + ("（内置）" if self._is_builtin(profile.id) else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self.profile_list.addItem(item)
        self.profile_list.blockSignals(False)

        row = 0
        for index in range(self.profile_list.count()):
            if self.profile_list.item(index).data(Qt.ItemDataRole.UserRole) == wanted:
                row = index
                break
        self.profile_list.setCurrentRow(row)

    def selected_id(self) -> str | None:
        item = self.profile_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select_profile(self, profile_id: str) -> bool:
        """按 id 选中某一行；找不到返回 False（工具与测试都按 id 定位）。"""
        for index in range(self.profile_list.count()):
            item = self.profile_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == profile_id:
                self.profile_list.setCurrentRow(index)
                return True
        return False

    def current_draft(self) -> ProviderProfile:
        """把界面上的字段收成一份档案（未校验）。"""
        return ProviderProfile(
            id=self.edit_id.text().strip(),
            label=self.edit_label.text().strip(),
            base_url=self.edit_base_url.text().strip(),
            asr_endpoint=self.edit_asr_endpoint.text().strip(),
            asr_resource_id=self.edit_asr_resource.text().strip(),
            asr_model_name=self._asr_model,
            judge_model=self._judge_model,
            judge_protocol=self.edit_judge_protocol.currentData() or PROTO_DISABLED,
            asr_protocol=self.edit_asr_protocol.currentData() or PROTO_DISABLED,
            api_key=self.edit_api_key.text().strip(),
            # 内置档案的"启用"状态由云端卡片测通后写，这里不提供假开关
            enabled=self._stored_flag("enabled"),
            allow_upload=self.allow_upload.isChecked(),
            use_system_proxy=self.use_system_proxy.isChecked(),
            custom_models=tuple(self._custom_models),
            model_info=self._tidy_model_info(),
        )

    def _tidy_model_info(self) -> tuple[ModelInfo, ...]:
        """三个字段全空的条目直接丢掉，不写进配置。

        留着的唯一效果是让"配过但配成空"和"从没配过"在界面上长得一样，分不清。
        """
        return tuple(info for info in self._model_info if not info.is_empty())

    def _stored_flag(self, name: str) -> bool:
        """取当前档案已保存的某个开关（表单不暴露它，但不能在保存时把它清掉）。"""
        profile = self._stored_profile(self._profile_id)
        return bool(getattr(profile, name, False)) if profile is not None else False

    def _stored_profile(self, profile_id: str | None) -> ProviderProfile | None:
        return self._config.profiles().get(profile_id) if profile_id else None

    def _is_builtin(self, profile_id: str) -> bool:
        """内置 = **代码里有默认值**的那些（可「恢复默认」）。

        不能用"是否出现在用户配置里"来判断：用户改过字段的内置档案一样是内置——
        只是被覆盖了，恢复默认就是把这层覆盖删掉。
        """
        return any(profile.id == profile_id for profile in DEFAULT_PROFILES)

    # ---- 列表动作 -------------------------------------------------------

    def _on_selected(self, *_args) -> None:
        if not self._loading:
            self._load_profile(self.selected_id())

    def _load_profile(self, profile_id: str | None) -> None:
        profile = self._stored_profile(profile_id)
        if profile is None:
            return
        self._loading = True
        self._profile_id = profile.id
        self.edit_id.setText(profile.id)
        self.edit_id.setEnabled(not self._is_builtin(profile.id))
        self.edit_label.setText(profile.label)
        self.edit_base_url.setText(profile.base_url)
        self.edit_judge_protocol.setCurrentIndex(
            max(0, self.edit_judge_protocol.findData(profile.judge_protocol))
        )
        self.edit_asr_protocol.setCurrentIndex(
            max(0, self.edit_asr_protocol.findData(profile.asr_protocol))
        )
        self._judge_model = profile.judge_model
        self._asr_model = profile.asr_model_name
        self._custom_models = list(profile.custom_models)
        self._model_info = list(profile.model_info)
        self.model_table.set_assignment(self._judge_model, self._asr_model)
        self.edit_asr_endpoint.setText(profile.asr_endpoint)
        self.edit_asr_resource.setText(profile.asr_resource_id)
        self.edit_api_key.setText(profile.api_key)
        self.allow_upload.setChecked(profile.allow_upload)
        self.use_system_proxy.setChecked(profile.use_system_proxy)
        self._loading = False
        # 换一家厂商就回到默认视图：折叠状态不跨档案粘住（否则看了火山的必填字段后
        # 切到一家只做语义判断的厂商，高级区还摊着）。需要时会由
        # `_refresh_field_visibility()` 自动展开。
        self.advanced_toggle.blockSignals(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.blockSignals(False)
        builtin = self._is_builtin(profile.id)
        # 按钮状态跟着选中项走：给内置档案亮着一个红色「删除」像是能删，
        # 点下去才发现被拦——不如一开始就置灰。
        self.btn_delete.setEnabled(not builtin)
        self.btn_restore.setEnabled(builtin)
        self.use_judge.setChecked(self._capability_uses_cloud(profile, CAP_JUDGE))
        self.use_asr.setChecked(self._capability_uses_cloud(profile, CAP_ASR))
        self._refresh_cloud_hint()
        self._refresh_field_visibility()
        self._refresh_info_panel()
        self._set_hint("")
        # 换厂先清掉上一家的清单：留着会让表格在新厂商名下短暂显示别人的模型
        self._fetched_models = []
        # 切到一家厂商就顺手拉一次它的模型列表（地址与 Key 齐了才会真发请求；
        # 同一对 (地址, Key) 命中缓存则不重复拉）
        self._request_models()

    def _on_new(self) -> None:
        """新增：把所有字段清空、两个协议都置停用，等用户自己填。"""
        self._loading = True
        self._profile_id = None
        self.edit_id.setText("")
        self.edit_id.setEnabled(True)
        self.edit_label.setText("")
        self.edit_base_url.setText("")
        self.edit_judge_protocol.setCurrentIndex(
            self.edit_judge_protocol.findData(PROTO_DISABLED)
        )
        self.edit_asr_protocol.setCurrentIndex(
            self.edit_asr_protocol.findData(PROTO_DISABLED)
        )
        self._judge_model = ""
        self._asr_model = ""
        self._custom_models = []
        self._model_info = []
        self.model_table.set_models(())
        self.model_table.set_assignment("", "")
        self.edit_asr_endpoint.setText("")
        self.edit_asr_resource.setText("")
        self.edit_api_key.setText("")
        self.allow_upload.setChecked(False)
        self.use_system_proxy.setChecked(False)
        self.use_judge.setChecked(False)
        self.use_asr.setChecked(False)
        self.cloud_hint.setText("")
        self._loading = False
        self._refresh_info_panel()
        self._loading = False
        self.profile_list.setCurrentItem(None)
        self.btn_delete.setEnabled(False)      # 还没保存的新档案无可删
        self.btn_restore.setEnabled(False)
        self.advanced_toggle.setChecked(True)  # 新增必须先填 id，直接把高级区摊开
        self._refresh_field_visibility()
        self._set_hint("填好 id 与名称后选协议；保存即写入配置。")

    def _on_duplicate(self) -> None:
        """复制为自定义：同一家厂商两套 Key / 两个账号时用。"""
        source = self._stored_profile(self._profile_id)
        if source is None:
            return
        self._profile_id = None
        self.edit_id.setText("")
        self.edit_id.setEnabled(True)
        self.edit_label.setText(f"{source.label} 副本")
        self.edit_label.setFocus()
        self._set_hint(
            f"从「{source.label}」复制：请填一个新的 id（不能与已有 id 重复）。"
        )

    def _on_delete(self) -> None:
        profile = self._stored_profile(self._profile_id)
        if profile is None or self._is_builtin(profile.id):
            return
        affected = [
            CAP_LABELS[capability]
            for capability in (CAP_LABELS.keys())
            if self._config.backend_for(capability) == profile.id
        ]
        tail = (
            f"\n目前引用它的能力（{'、'.join(affected)}）会回落到本地模型。"
            if affected
            else ""
        )
        agreed = message.confirm(
            self,
            "删除厂商档案",
            f"删除自定义厂商「{profile.label}」（id={profile.id}）？{tail}\n此操作不可撤销。",
            ok_text="删除",
        )
        if not agreed:
            return
        self._config.providers = [
            item
            for item in self._config.providers
            if str(item.get("id")) != profile.id
        ]
        self._save_config()
        reverted = self._release_backend(profile.id)
        self._profile_id = None
        self.reload()
        self._set_hint(
            f"已删除「{profile.label}」。"
            + (f"引用它的能力已回落本地：{'、'.join(reverted)}。" if reverted else "")
        )

    def _on_restore(self) -> None:
        profile = self._stored_profile(self._profile_id)
        if profile is None or not self._is_builtin(profile.id):
            return
        agreed = message.confirm(
            self,
            "恢复内置默认",
            f"恢复「{profile.label}」的内置默认值？\n"
            "将丢弃你对地址/协议/模型名的改动，并清空已保存的 API Key；"
            "引用它的能力会回落本地。",
            ok_text="恢复默认",
        )
        if not agreed:
            return
        self._config.providers = [
            item
            for item in self._config.providers
            if str(item.get("id")) != profile.id
        ]
        self._save_config()
        reverted = self._release_backend(profile.id)
        self.reload(select=profile.id)
        self._set_hint(
            f"已恢复「{profile.label}」的内置默认。"
            + (f"引用它的能力已回落本地：{'、'.join(reverted)}。" if reverted else "")
        )

    def _release_backend(self, profile_id: str) -> list[str]:
        """把指向该档案的能力改回本地；返回受影响的能力中文名。"""
        prefs = dict(self._config.backend_prefs)
        reverted: list[str] = []
        for capability in CAP_LABELS:
            if prefs.get(capability) == profile_id:
                prefs[capability] = LOCAL_BACKEND
                reverted.append(CAP_LABELS[capability])
        if reverted:
            self._config.backend_prefs = prefs
            self._save_config()
        return reverted

    # ---- 模型列表（按厂商自动刷新）--------------------------------------

    def _request_models(self, *, force: bool = False) -> None:
        """拉取当前厂商的模型清单：``GET {base_url}/models``。

        触发时机：切换厂商时自动（`_load_profile`）、填完 Key/地址失焦时、点
        「刷新模型列表」、以及保存成功后。三种情况不拉：地址没填、Key 没填、
        同一对 (地址, Key) 已在缓存里（除非 ``force``）——都会在提示行说清原因，
        而不是静默什么都不做。
        """
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        draft = self.current_draft()
        base_url = (draft.base_url or "").strip()
        api_key = draft.resolved_api_key()
        if not base_url:
            self._show_models([])
            self.model_note.setText(
                "填好接口地址后可自动获取该厂商的模型列表；也可以在表格里直接添加模型名。"
            )
            return
        if not api_key:
            env_name = ENV_KEY_TEMPLATE.format(
                id=(draft.id or "<id>").upper()
            )
            self._show_models([])
            self.model_note.setText(
                f"填好 API Key（或设环境变量 {env_name}）后自动获取模型列表；"
                "也可以在表格里直接添加模型名。"
            )
            return
        cached = self._model_cache.get((base_url, api_key))
        if cached is not None and not force:
            self._show_models(cached, cached=True)
            return

        self._show_models([])          # 先把已手填的显示出来，别让表格空着
        self.model_note.setText("正在获取模型列表…")
        self.btn_refresh_models.setEnabled(False)
        worker = ModelListWorker(draft, parent=self)
        worker.listed.connect(self._on_models_listed)
        worker.failed.connect(self._on_models_failed)
        worker.finished_ok.connect(lambda: self.btn_refresh_models.setEnabled(True))
        register_worker(self, "model_list", worker)
        self._models_worker = worker
        worker.start()

    def _on_models_listed(self, models) -> None:
        listed = [str(item) for item in (models or []) if str(item).strip()]
        draft = self.current_draft()
        self._model_cache[(draft.base_url.strip(), draft.resolved_api_key())] = listed
        self._show_models(listed)

    def _on_models_failed(self, message: str) -> None:
        """拉不到不是错误：不少厂商没有 /models（方舟只认推理接入点 ID）。"""
        self._show_models([])
        self.model_note.setText(f"{message}；可在表格里手动添加模型名。")

    def _show_models(self, fetched, *, cached: bool = False) -> None:
        """把（接口拉回来的）清单交给表格，手填的自动接在后面。

        合并规则来自 ``providers.merge_model_choices``（唯一一份定义）——
        表单里的手填清单可能还没落盘，所以不走 ``profile.model_choices()``。
        """
        self._fetched_models = [str(item) for item in fetched if str(item).strip()]
        # 指派的模型名也要进表格：它可能存在档案里但既不在接口清单、也不在手填
        # 清单里（老配置就是这样），不显示出来用户就看不到"现在用的是哪个"
        names = merge_model_choices(
            self._fetched_models,
            self._custom_models + [self._judge_model, self._asr_model],
        )
        self.model_table.set_models(
            names, fetched=self._fetched_models, tags=self._meta_tags(names)
        )
        self.model_table.set_assignment(self._judge_model, self._asr_model)
        manual = len(self._custom_models)
        if self._fetched_models:
            suffix = "（来自缓存）" if cached else ""
            extra = f"，另有 {manual} 个是手填的" if manual else ""
            self.model_note.setText(
                f"模型列表{suffix}：{len(self._fetched_models)} 个来自该厂商的 /models"
                f"{extra}。"
            )
        elif manual or self._judge_model or self._asr_model:
            self.model_note.setText(
                "没有拉到清单（没填 Key 或该厂商不提供 /models）：表格里是档案中已有的"
                "模型名，也可以继续手动添加。"
            )

    # ---- 模型表格（指派 / 添加 / 删除）----------------------------------

    def _on_model_assigned(self, name: str, capability: str) -> None:
        """把某个模型名指派给一条能力。与其它字段一样，等「保存」才写进配置。"""
        if capability == CAP_ASR:
            self._asr_model = name
        else:
            self._judge_model = name
        self.model_table.set_assignment(self._judge_model, self._asr_model)
        self._refresh_egress_preview()
        label = CAP_LABELS.get(capability, capability)
        self._set_hint(f"{label}模型已改为「{name}」（点「保存」后写入配置）。")
        # 模态可能与这条能力冲突（比如把纯文本模型指给了转写），说清比事后报错好
        self._warn_if_modality_conflicts()

    def _warn_if_modality_conflicts(self) -> None:
        """指派与已配模态冲突时，在提示行点名。"""
        checks = (
            (self._judge_model, CAP_JUDGE, "文本"),
            (self._asr_model, CAP_ASR, "音频"),
        )
        for name, capability, needed in checks:
            if not name:
                continue
            info = self._info_of(name)
            if not info.modalities:
                continue
            if needed == "文本" and not info.can_judge():
                self._set_hint(
                    f"「{name}」的模态里没有文本，不能做{CAP_LABELS[capability]}——"
                    "在下面「模型参数」里改，或换一个模型。"
                )
            elif needed == "音频" and not info.can_transcribe():
                self._set_hint(
                    f"「{name}」的模态里没有音频，不能做{CAP_LABELS[capability]}——"
                    "在下面「模型参数」里改，或换一个模型。"
                )

    # ---- 模型参数 -------------------------------------------------------

    def _info_of(self, name: str) -> ModelInfo:
        """当前编辑中的某个模型的参数档案；没有就返回全空的一份。"""
        for info in self._model_info:
            if info.name == name:
                return info
        return ModelInfo(name=name)

    def _store_info(self, info: ModelInfo) -> None:
        """写回内存里的参数档案，并刷新表格里的标记。"""
        self._model_info = [item for item in self._model_info if item.name != info.name]
        if not info.is_empty():
            self._model_info.append(info)
        self._show_models(self._fetched_models)
        self._refresh_info_panel()

    def _selected_model_name(self) -> str:
        return self.model_table.list.selected()

    def _on_model_selected(self, name: str) -> None:
        """表格选中项变了 → 参数面板切到那个模型。"""
        self._refresh_info_panel()

    def _on_info_edited(self, *_args) -> None:
        """面板被改了：写回内存并刷新标记与说明。"""
        if self._loading or self._info_syncing:
            return
        info = self._current_info_from_panel()
        if not info.name:
            return
        self._model_info = [item for item in self._model_info if item.name != info.name]
        if not info.is_empty():
            self._model_info.append(info)
        self.model_table.set_tags(self._meta_tags(self.model_table.names()))
        self._refresh_info_panel()
        self._warn_if_modality_conflicts()

    def _meta_tag(self, name: str) -> str:
        """某个模型在表格行尾的参数摘要（没配就空串）。"""
        info = self._info_of(name)
        if info.is_empty():
            return ""
        bits: list[str] = []
        if info.context_window:
            bits.append(format_context(info.context_window))
        if info.reasoning:
            bits.append(f"思考·{info.reasoning}")
        if info.modalities:
            bits.append("+".join(
                MODALITY_LABELS[key] for key in MODALITIES if key in info.modalities
            ))
        return "·".join(bits)

    def _meta_tags(self, names) -> dict[str, str]:
        return {name: self._meta_tag(name) for name in names if self._meta_tag(name)}

    def _on_autofill_info(self) -> None:
        """按模型名推断参数并写回。

        推断**不会**凭空多出字段：`infer_model_info` 对认不出的模型留空上下文
        窗口与思考等级（见 core/model_info.py 的"未知就留空"），所以点一下不会
        把能用的模型弄坏。
        """
        name = self._selected_model_name()
        if not name:
            self._set_hint("先在上面选中一个模型，再点「自动填充」。")
            return
        inferred = infer_model_info(name)
        if inferred.is_empty():
            self._set_hint(
                f"认不出「{name}」的规格——上下文窗口与思考等级需要手填。"
            )
            return
        current = self._info_of(name)
        # 已经手填过的值不被推断覆盖：用户的选择优先于知识库
        merged = inferred.with_updates(
            context_window=current.context_window or inferred.context_window,
            reasoning=current.reasoning or inferred.reasoning,
            modalities=current.modalities or inferred.modalities,
            note=inferred.note,
        )
        self._store_info(merged.with_updates(source="auto"))
        self._set_hint(
            f"已按模型名推断「{name}」的参数（估计值，可直接改；点「保存」后生效）。"
        )

    def _on_clear_info(self) -> None:
        name = self._selected_model_name()
        if not name:
            return
        self._store_info(ModelInfo(name=name))
        self._set_hint(
            f"已清空「{name}」的参数（恢复成未设置，不影响其它模型；点「保存」后生效）。"
        )

    def _current_info_from_panel(self) -> ModelInfo:
        """把面板上的控件读成一份 :class:`ModelInfo`。"""
        name = self._selected_model_name()
        current = self._info_of(name)
        modalities = tuple(
            key for key in MODALITIES if self.info_modalities[key].isChecked()
        )
        return ModelInfo(
            name=name,
            context_window=self.info_context.value(),
            reasoning=str(self.info_reasoning.currentData() or ""),
            modalities=modalities,
            source="manual" if (self.info_context.value() or modalities
                               or self.info_reasoning.currentData()) else current.source,
            note=current.note,
        )

    def _refresh_info_panel(self) -> None:
        """把选中模型的参数填进面板；没选中就置灰并说明。

        ``_info_syncing`` 是必需的：``setValue`` / ``setCurrentIndex`` /
        ``setChecked`` 全都会发信号，而信号槽正是"用户改了 → 写回"那一条。
        不挡住的话，回填一半的面板会被读成"用户的新值"，把刚填进去的参数覆盖成
        半截（上下文窗口还在、模态没了）。这类 bug 只在特定顺序下出现，极难复现。
        """
        self._info_syncing = True
        try:
            self._refresh_info_panel_inner()
        finally:
            self._info_syncing = False

    def _refresh_info_panel_inner(self) -> None:
        name = self._selected_model_name()
        enabled = bool(name)
        for widget in (
            self.info_context,
            self.info_reasoning,
            self.btn_autofill,
            self.btn_clear_info,
        ):
            widget.setEnabled(enabled)
        for box in self.info_modalities.values():
            box.setEnabled(enabled)
        if not enabled:
            self.info_title.setText("模型参数：先在上面选中一个模型")
            self.info_note.setText("")
            self.info_context.setValue(0)
            self.info_reasoning.setCurrentIndex(0)
            for box in self.info_modalities.values():
                box.setChecked(False)
            return

        info = self._info_of(name)
        self.info_title.setText(f"模型参数：{name}")
        self.info_context.setValue(info.context_window)
        index = self.info_reasoning.findData(info.reasoning)
        self.info_reasoning.setCurrentIndex(index if index >= 0 else 0)
        for key, box in self.info_modalities.items():
            box.setChecked(key in info.modalities)

        bits: list[str] = []
        if info.context_window:
            bits.append(f"上下文 {format_context(info.context_window)}")
        if info.reasoning:
            bits.append(f"思考 {info.reasoning}")
        if info.modalities:
            bits.append("模态 " + "+".join(
                MODALITY_LABELS[key] for key in MODALITIES if key in info.modalities
            ))
        origin = "自动推断（估计值）" if info.source == "auto" else "手填"
        if info.is_empty():
            self.info_note.setText(
                "未设置——不截断、不发送思考等级、不做模态过滤（保持原样）。"
            )
        else:
            self.info_note.setText("｜".join(bits) + f"。来源：{origin}。{info.note}")

    def _on_model_added(self, name: str) -> None:
        """手动添加一个模型名：留在清单里（下次打开还能选）。"""
        if name not in self._custom_models:
            self._custom_models.append(name)
        self.model_table.edit_add.clear()
        self._show_models(self._fetched_models)
        self._set_hint(f"已添加模型「{name}」（点「保存」后写入配置）。")

    def _on_model_removed(self, name: str) -> None:
        """删除一个手填的模型名；它若正被指派，一并清空指派（不留悬空引用）。"""
        self._custom_models = [item for item in self._custom_models if item != name]
        cleared = []
        if self._judge_model == name:
            self._judge_model = ""
            cleared.append(CAP_LABELS[CAP_JUDGE])
        if self._asr_model == name:
            self._asr_model = ""
            cleared.append(CAP_LABELS[CAP_ASR])
        self._show_models(self._fetched_models)
        note = f"已删除模型「{name}」（点「保存」后写入配置）。"
        if cleared:
            note += f"它正被指派给{'、'.join(cleared)}，指派也已清空。"
        self._set_hint(note)

    # ---- 保存 -----------------------------------------------------------

    def _on_save(self) -> None:
        draft = self._validated_draft()
        if draft is None:
            return
        self._write_profile(draft)
        self._profile_id = draft.id
        # 先 reload 再写提示：reload 会选中新档案并清空提示行
        self.reload(select=draft.id)
        self._set_hint(f"已保存「{draft.label}」（id={draft.id}）。")

    def _validated_draft(self) -> ProviderProfile | None:
        """校验当前编辑的档案；有问题返回 None（原因写进提示行）。

        「保存」与「保存并测试连通性」共用这一处：两条路径各写一遍校验，
        迟早出现"一个按钮放行、另一个拦下"。
        """
        draft = self.current_draft()
        taken = [
            profile.id
            for profile in self._config.profiles().all()
            if profile.id != self._profile_id
        ]
        problems = validate_profile(draft, existing_ids=taken)
        if problems:
            joined = "；".join(problems)
            # 缺项（id / 接口地址 / 火山字段）都在折叠起来的高级区里：先摊开再报错，
            # 否则用户看到"缺 base_url"却在界面上找不到这个字段。
            if any(
                token in joined
                for token in ("id", "base_url", "接口地址", "asr_endpoint", "asr_resource_id")
            ):
                self.advanced_toggle.setChecked(True)
            self._set_hint("保存失败：" + joined)
            return None
        return draft

    def _write_profile(self, profile: ProviderProfile) -> None:
        """按 id 合并一份档案回配置并落盘。"""
        items = [dict(item) for item in self._config.providers]
        for index, item in enumerate(items):
            if str(item.get("id")) == profile.id:
                items[index] = profile.to_json()
                break
        else:
            items.append(profile.to_json())
        self._config.providers = items
        self._save_config()

    # ---- 云端能力：开关 / 测通 / 切换后端 --------------------------------

    def _cloud_switches(self):
        """能力 → 勾选框（顺序固定，界面与测试都按这个顺序读）。"""
        return ((CAP_JUDGE, self.use_judge), (CAP_ASR, self.use_asr))

    def _capability_uses_cloud(self, profile: ProviderProfile, capability: str) -> bool:
        """该能力此刻是否真的走这家云端（选了 + 档案已测通）。"""
        return profile.enabled and self._config.backend_for(capability) == profile.id

    def _set_hint(self, text: str) -> None:
        """写提示行，并同步显隐。

        显隐必须跟文字走：这一行的提示有 20 多处写入点，漏配一次就变成
        「消息设了但看不见」——而这页正文比一屏长，看不见就等于没写。
        """
        self.hint_label.setText(text)
        self.hint_label.setVisible(bool(text))

    def _save_and_probe(self) -> None:
        """保存 + 逐能力测连通：**只把测通的能力切到云端**。

        失败一律不允许启用：否则用户会以为通了，直到真正分析时才回退本地并
        看到一次"莫名其妙的回退"。测不通的勾选框会自动取消，并写明原因。
        """
        draft = self._validated_draft()
        if draft is None:
            return
        self._write_profile(draft)
        self._profile_id = draft.id

        results: dict[str, str | None] = {}
        for capability, checkbox in self._cloud_switches():
            if not checkbox.isChecked():
                results[capability] = None       # 没勾就不测（也不算启用）
                continue
            error = self._probe(draft, capability)
            results[capability] = error
            if error is not None:
                checkbox.setChecked(False)

        enabled_any = False
        passed: list[str] = []
        failures: list[str] = []
        for capability, checkbox in self._cloud_switches():
            uses_cloud = checkbox.isChecked() and results.get(capability) is None
            label = CAP_LABELS.get(capability, capability)
            if uses_cloud:
                enabled_any = True
                passed.append(label)
            elif results.get(capability) is not None:
                failures.append(f"{label}：{results[capability]}")
            self._set_cloud_backend(capability, draft.id if uses_cloud else LOCAL_BACKEND)

        self._write_profile(draft.with_updates(enabled=enabled_any))
        self.reload(select=draft.id)
        if failures:
            self._set_hint("连通性测试未通过——" + "；".join(failures) + "。")
        elif enabled_any:
            self._set_hint(
                f"连通性正常：{draft.label}。已切到云端的是：{'、'.join(passed)}。"
            )
        else:
            self._set_hint("已保存字段；未勾选任何能力，这条能力仍走本地模型。")

    def _probe(self, profile: ProviderProfile, capability: str) -> str | None:
        """连通性测试的接缝：返回错误描述；可用时返回 None。

        单独一个方法是为了让测试能替换掉它——界面测试不允许真联网。能力是否
        **配置完整**统一问 `missing_config_reason()`（与日志/检查器同一份判据），
        否则多协议之后这里会与核心层各说各话。
        """
        if not profile.allow_upload:
            return "未勾选「允许外发素材」"
        if not profile.resolved_api_key():
            env_name = ENV_KEY_TEMPLATE.format(id=profile.id.upper())
            return f"没有 API Key（可填在本页，或设环境变量 {env_name}）"
        missing = missing_config_reason(profile, capability)
        if missing is not None:
            return missing
        transport = (
            build_judge_transport(profile)
            if capability == CAP_JUDGE
            else build_asr_transport(profile)
        )
        if transport is None:
            protocol = (
                profile.judge_protocol if capability == CAP_JUDGE else profile.asr_protocol
            )
            label = PROTO_LABELS.get(protocol, protocol)
            return (
                f"暂不支持「{label}」协议的"
                f"{CAP_LABELS.get(capability, capability)}：{profile.id}"
            )
        try:
            transport.probe()
        except ProviderError as exc:
            return exc.describe()
        except Exception as exc:  # noqa: BLE001 - 任何异常都算"不通"
            return str(exc)
        return None

    def _set_cloud_backend(self, capability: str, backend: str) -> None:
        prefs = dict(self._config.backend_prefs)
        prefs[capability] = backend
        self._config.backend_prefs = prefs
        self._save_config()

    def _refresh_cloud_hint(self) -> None:
        profile = self._stored_profile(self._profile_id)
        if profile is None:
            self.cloud_hint.setText("")
            return
        used = [
            CAP_LABELS[capability]
            for capability, _ in self._cloud_switches()
            if self._capability_uses_cloud(profile, capability)
        ]
        self.cloud_hint.setText(
            ("已启用：" + "、".join(used)) if used else "未启用（走本地模型）"
        )

    def _save_config(self) -> None:
        try:
            self._config.save(self._config_path)
        except OSError as exc:
            self._set_hint(f"配置写入失败：{exc}")

    # ---- 字段显隐与预览 -------------------------------------------------

    def _refresh_field_visibility(self) -> None:
        """按协议决定哪些字段有意义，并维护"高级字段"的折叠。

        规则：
        - 高级区（接口地址 / 协议 / 火山专用 / id / 代理）默认收起，需要时勾一下；
        - 两条能力都停用时，接口地址没有意义，连它带标题一起藏；
        - 火山转写的 asr_endpoint / asr_resource_id 按协议显隐。

        藏的是整个 `LabeledField`（标题 + 输入框一起），不是只藏输入框：
        留着一个"孤儿标题"比不藏更让人迷惑。
        """
        judge_protocol = self.edit_judge_protocol.currentData() or PROTO_DISABLED
        asr_protocol = self.edit_asr_protocol.currentData() or PROTO_DISABLED
        volcano = asr_protocol == PROTO_VOLC_ARK

        self.advanced_host.setVisible(self.advanced_toggle.isChecked())
        self.volcano_host.setVisible(volcano)
        self.base_field.setVisible(
            judge_protocol != PROTO_DISABLED or asr_protocol == PROTO_OPENAI
        )
        env_name = ENV_KEY_TEMPLATE.format(
            id=(self.edit_id.text().strip() or "<id>").upper()
        )
        # 环境变量名写进占位符而不是字段下方的 help：help 多占两行，会把首屏挤到
        # 需要滚动（Key 是必看字段，不能让它的输入框掉到折叠线下）。文案只留环境
        # 变量名这一件事——"明文存 config.json" 写在对话框底栏，两处都写会被输入框
        # 截断成半句（实测）
        self.edit_api_key.setPlaceholderText(f"留空则用环境变量 {env_name}")
        self._refresh_egress_preview()

    def _refresh_egress_preview(self) -> None:
        """一行只读预览：两条能力各用什么协议、会发往哪台主机。

        只留这两件事——接口与模型名就在上方的字段里，重复一遍会把首屏再撑高一行。
        """
        draft = self.current_draft()
        judge_protocol = PROTO_LABELS.get(draft.judge_protocol, draft.judge_protocol)
        asr_protocol = PROTO_LABELS.get(draft.asr_protocol, draft.asr_protocol)
        if not (draft.supports(CAP_JUDGE) or draft.supports(CAP_ASR)):
            self.egress_preview.setText(
                f"协议：语义判断 = {judge_protocol}，转写 = {asr_protocol}。"
                "尚未配置可用的云端能力（协议停用或字段没填全）。"
            )
            return
        self.egress_preview.setText(
            f"协议：语义判断 = {judge_protocol}，转写 = {asr_protocol}。"
            f"{describe_egress(draft)}"
        )