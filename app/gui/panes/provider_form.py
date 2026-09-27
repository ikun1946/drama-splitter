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

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
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
        for button in (self.btn_new, self.btn_duplicate, self.btn_delete, self.btn_restore):
            left.addWidget(button)
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
        self.allow_upload = QCheckBox("允许外发素材（不勾则为零外发）", self)
        self.use_system_proxy = QCheckBox("走系统代理（默认绕过，本地端点必须绕过）", self)
        for combo, capability in (
            (self.edit_judge_protocol, "judge"),
            (self.edit_asr_protocol, "asr"),
        ):
            for protocol in PROTOCOLS_BY_CAP[capability]:
                combo.addItem(PROTO_LABELS[protocol], protocol)
            combo.currentIndexChanged.connect(self._refresh_field_visibility)
        self.edit_id.textChanged.connect(self._refresh_field_visibility)

        id_field = LabeledField("id（小写字母/数字/-/_，不可改内置）")
        id_field.attach(self.edit_id)
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
        self.key_field = LabeledField(
            "API Key", help_text="明文存 config.json；可用它上方提示的环境变量覆盖"
        )
        self.key_field.attach(self.edit_api_key)

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

        protocol_note = QLabel(
            "协议决定信封与路径，不开放自定义：OpenAI 兼容 = /chat/completions 与 "
            "/audio/transcriptions（固定 verbose_json + 词级时间戳）；火山方舟 = 极速版"
            "录音文件识别协议。两条能力相互独立，可只配其一。",
            self,
        )
        protocol_note.setWordWrap(True)
        protocol_note.setProperty("role", "muted")

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        actions.addWidget(self.btn_save)
        actions.addWidget(self.btn_revert)
        actions.addStretch(1)

        editor_card = Card("档案字段", parent=self)
        editor_card.body_layout.addWidget(id_field)
        editor_card.body_layout.addWidget(label_field)
        editor_card.body_layout.addWidget(self.base_field)
        editor_card.body_layout.addWidget(self.judge_protocol_field)
        editor_card.body_layout.addWidget(self.judge_model_field)
        editor_card.body_layout.addWidget(self.asr_protocol_field)
        editor_card.body_layout.addWidget(self.asr_model_field)
        editor_card.body_layout.addWidget(self.asr_endpoint_field)
        editor_card.body_layout.addWidget(self.asr_resource_field)
        editor_card.body_layout.addWidget(self.key_field)
        editor_card.body_layout.addWidget(self.allow_upload)
        editor_card.body_layout.addWidget(self.use_system_proxy)
        editor_card.body_layout.addWidget(protocol_note)
        editor_card.body_layout.addWidget(self.egress_preview)
        editor_card.body_layout.addLayout(actions)
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
            self.hint_label.setText("保存失败：" + "；".join(problems))
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
        """按协议决定哪些字段有意义——把无关字段藏起来，减少填错的机会。

        藏的是整个 `LabeledField`（标题 + 输入框一起），不是只藏输入框：
        留着一个"孤儿标题"比不藏更让人迷惑。
        """
        judge_protocol = self.edit_judge_protocol.currentData() or PROTO_DISABLED
        asr_protocol = self.edit_asr_protocol.currentData() or PROTO_DISABLED
        self.base_field.setVisible(
            judge_protocol != PROTO_DISABLED or asr_protocol == PROTO_OPENAI
        )
        self.judge_model_field.setVisible(judge_protocol != PROTO_DISABLED)
        self.asr_model_field.setVisible(asr_protocol != PROTO_DISABLED)
        self.asr_endpoint_field.setVisible(asr_protocol == PROTO_VOLC_ARK)
        self.asr_resource_field.setVisible(asr_protocol == PROTO_VOLC_ARK)
        env_name = ENV_KEY_TEMPLATE.format(
            id=(self.edit_id.text().strip() or "<id>").upper()
        )
        self.key_field.set_help(f"明文存 config.json；也可设环境变量 {env_name} 覆盖")
        self._refresh_egress_preview()

    def _refresh_egress_preview(self) -> None:
        draft = self.current_draft()
        if not (draft.supports("judge") or draft.supports("asr")):
            self.egress_preview.setText("尚未配置可用的云端能力（两条协议都还停用或字段没填全）。")
            return
        self.egress_preview.setText(describe_egress(draft))