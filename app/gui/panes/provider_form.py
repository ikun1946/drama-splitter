"""厂商档案管理表单：新增 / 编辑 / 复制内置 / 删除 / 恢复默认。

为什么要一个独立对话框
----------------------
厂商从"代码里写死的一家"变成"用户可增删的数据"之后，编辑入口必须集中：
「云端模型」卡片只负责"选谁 + 填 Key + 测通"，改地址/协议/模型名一律来这里。
两处都能改同一份档案，必然出现"改了没生效"的扯皮。

几条硬规则
----------
- **内置厂商不可删除、id 不可改**（它们在代码里有默认值），但可以改字段；
  改完想还原就「恢复默认」（从配置里删掉该 id 的覆盖项）。
- **编辑内置 = 同 id 覆盖**：`merge_profiles` 本来就以 id 为键合并，
  `backend_prefs` 里存的也是 id，因此覆盖不会让"已选中的后端"失效。
- **删除/恢复默认必须连带处理引用**：把 `backend_prefs` 里指向它的能力改回本地，
  否则用户只会看到"云端静默失效"而不知道为什么。
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
    ProviderProfile,
    describe_egress,
    validate_profile,
)
from ..state import ProjectState
from ..widgets import (
    Card,
    LabeledField,
    NoWheelComboBox,
    scrollable,
)
from ..widgets import message

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

        # ---- 左栏：列表与动作 ------------------------------------------
        self.profile_list = QListWidget(self)
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
        self.edit_judge_model = QLineEdit(self)
        self.edit_asr_model = QLineEdit(self)
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

        label_field = LabeledField("名称（界面上显示）")
        label_field.attach(self.edit_label)
        self.base_field = LabeledField("接口地址（base_url，OpenAI 兼容要带 /v1）")
        self.base_field.attach(self.edit_base_url)
        self.judge_protocol_field = LabeledField("语义判断协议")
        self.judge_protocol_field.attach(self.edit_judge_protocol)
        self.judge_model_field = LabeledField("模型名（judge_model）")
        self.judge_model_field.attach(self.edit_judge_model)
        self.asr_protocol_field = LabeledField("转写协议")
        self.asr_protocol_field.attach(self.edit_asr_protocol)
        self.asr_model_field = LabeledField("转写模型名（asr_model_name）")
        self.asr_model_field.attach(self.edit_asr_model)
        self.asr_endpoint_field = LabeledField("火山转写接口（asr_endpoint）")
        self.asr_endpoint_field.attach(self.edit_asr_endpoint)
        self.asr_resource_field = LabeledField("火山资源 ID（asr_resource_id）")
        self.asr_resource_field.attach(self.edit_asr_resource)
        self.key_field = LabeledField("API Key")
        self.key_field.attach(self.edit_api_key)

        def pair(left, right) -> QHBoxLayout:
            """两个字段并成一排：常用字段首屏就能看完，不必滚动。"""
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            row.addWidget(left, 1)
            row.addWidget(right, 1)
            return row

        # ---- 高级字段：默认收起 ------------------------------------------
        #
        # 这些字段绝大多数时候不用动（id 只在新增时填、火山专用字段只在火山协议下
        # 才有意义、代理只在被本机代理拦截时才要打开）。放在常用区会把首屏撑成一长列。
        # 但**绝不藏必填项**：新增档案时、或选了火山转写协议时，自动展开（见
        # `_refresh_field_visibility`），否则用户会"看不到却必须填"。
        self.advanced_toggle = QCheckBox("显示高级字段（id / 火山专用 / 代理）", self)
        self.advanced_toggle.toggled.connect(self._refresh_field_visibility)
        id_field = LabeledField("id（小写字母/数字/-/_，保存后不可改内置）")
        id_field.attach(self.edit_id)
        self.advanced_host = QWidget(self)
        advanced = QVBoxLayout(self.advanced_host)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(4)
        advanced.addWidget(id_field)
        advanced.addLayout(pair(self.asr_endpoint_field, self.asr_resource_field))
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
        editor_card.body_layout.addWidget(self.base_field)
        editor_card.body_layout.addLayout(
            pair(self.judge_protocol_field, self.judge_model_field)
        )
        editor_card.body_layout.addLayout(
            pair(self.asr_protocol_field, self.asr_model_field)
        )
        editor_card.body_layout.addWidget(self.advanced_host)
        # Key 与「允许外发」并排：两者都是"这一家怎么被使用"的开关，且并排后
        # 首屏刚好放下全部常用字段（不必为了看 Key 输入框去滚动）
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(12)
        key_row.addWidget(self.key_field, 2)
        key_row.addWidget(self.allow_upload, 1, Qt.AlignmentFlag.AlignBottom)
        editor_card.body_layout.addLayout(key_row)
        editor_card.body_layout.addWidget(self.egress_preview)
        editor_card.body_layout.addWidget(self.hint_label)

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
            asr_model_name=self.edit_asr_model.text().strip(),
            judge_model=self.edit_judge_model.text().strip(),
            judge_protocol=self.edit_judge_protocol.currentData() or PROTO_DISABLED,
            asr_protocol=self.edit_asr_protocol.currentData() or PROTO_DISABLED,
            api_key=self.edit_api_key.text().strip(),
            # 内置档案的"启用"状态由云端卡片测通后写，这里不提供假开关
            enabled=self._stored_flag("enabled"),
            allow_upload=self.allow_upload.isChecked(),
            use_system_proxy=self.use_system_proxy.isChecked(),
        )

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
        self.edit_judge_model.setText(profile.judge_model)
        self.edit_asr_model.setText(profile.asr_model_name)
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
        self._refresh_field_visibility()
        self.hint_label.setText("")

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
        self.edit_judge_model.setText("")
        self.edit_asr_model.setText("")
        self.edit_asr_endpoint.setText("")
        self.edit_asr_resource.setText("")
        self.edit_api_key.setText("")
        self.allow_upload.setChecked(False)
        self.use_system_proxy.setChecked(False)
        self._loading = False
        self.profile_list.setCurrentItem(None)
        self.btn_delete.setEnabled(False)      # 还没保存的新档案无可删
        self.btn_restore.setEnabled(False)
        self.advanced_toggle.setChecked(True)  # 新增必须先填 id，直接把高级区摊开
        self._refresh_field_visibility()
        self.hint_label.setText("填好 id 与名称后选协议；保存即写入配置。")

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
        self.hint_label.setText(
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
        self.hint_label.setText(
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
        self.hint_label.setText(
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

    # ---- 保存 -----------------------------------------------------------

    def _on_save(self) -> None:
        draft = self.current_draft()
        taken = [
            profile.id
            for profile in self._config.profiles().all()
            if profile.id != self._profile_id
        ]
        problems = validate_profile(draft, existing_ids=taken)
        if problems:
            joined = "；".join(problems)
            # 常见缺项（id / 火山字段）就在折叠起来的高级区里：先摊开再报错，
            # 否则用户看到"缺 asr_endpoint"却在界面上找不到这个字段。
            if any(token in joined for token in ("id", "asr_endpoint", "asr_resource_id")):
                self.advanced_toggle.setChecked(True)
            self.hint_label.setText("保存失败：" + joined)
            return

        items = [dict(item) for item in self._config.providers]
        for index, item in enumerate(items):
            if str(item.get("id")) == draft.id:
                items[index] = draft.to_json()
                break
        else:
            items.append(draft.to_json())
        self._config.providers = items
        self._save_config()
        self._profile_id = draft.id
        # 先 reload 再写提示：reload 会选中新档案并清空提示行
        self.reload(select=draft.id)
        self.hint_label.setText(f"已保存「{draft.label}」（id={draft.id}）。")

    def _save_config(self) -> None:
        try:
            self._config.save(self._config_path)
        except OSError as exc:
            self.hint_label.setText(f"配置写入失败：{exc}")

    # ---- 字段显隐与预览 -------------------------------------------------

    def _refresh_field_visibility(self) -> None:
        """按协议决定哪些字段有意义，并维护"高级字段"的折叠。

        三条规则，都是"不能藏必填项"的推论：
        - 两条能力都停用时，接口地址没有意义；
        - 火山转写需要 asr_endpoint / asr_resource_id——选了它就**自动展开高级区**；
        - 新增档案必须先填 id——`_on_new()` 里同样自动展开。

        藏的是整个 `LabeledField`（标题 + 输入框一起），不是只藏输入框：
        留着一个"孤儿标题"比不藏更让人迷惑。
        """
        judge_protocol = self.edit_judge_protocol.currentData() or PROTO_DISABLED
        asr_protocol = self.edit_asr_protocol.currentData() or PROTO_DISABLED
        needs_advanced = asr_protocol == PROTO_VOLC_ARK

        if needs_advanced and not self.advanced_toggle.isChecked():
            self.advanced_toggle.setChecked(True)   # 会再进本方法一次，这次才落地
            return
        advanced = self.advanced_toggle.isChecked()
        self.advanced_host.setVisible(advanced)

        self.base_field.setVisible(
            judge_protocol != PROTO_DISABLED or asr_protocol == PROTO_OPENAI
        )
        self.judge_model_field.setVisible(judge_protocol != PROTO_DISABLED)
        self.asr_model_field.setVisible(asr_protocol != PROTO_DISABLED)
        self.asr_endpoint_field.setVisible(advanced and needs_advanced)
        self.asr_resource_field.setVisible(advanced and needs_advanced)
        env_name = ENV_KEY_TEMPLATE.format(
            id=(self.edit_id.text().strip() or "<id>").upper()
        )
        # 环境变量名写进占位符而不是字段下方的 help：help 多占两行，会把首屏挤到
        # 需要滚动（Key 是必看字段，不能让它的输入框掉到折叠线下）
        self.edit_api_key.setPlaceholderText(
            f"可直接填在这里（明文存 config.json），也可设环境变量 {env_name}"
        )
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