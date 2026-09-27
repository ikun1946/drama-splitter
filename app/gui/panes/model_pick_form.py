"""「选择云端模型」表单：挑一家能连通的厂商，再从它的模型清单里选一个。

为什么要一个专门的地方
----------------------
地址、协议、模型名都写在厂商档案里，改模型名要么去「厂商管理」手输、要么自己
先查厂商文档——每家命名都不一样。这里把这条链路缩短成"选一家 → 挑一个"：

- **只列能正常连接的厂商**：打开时对"填了 API Key 的厂商"实时拉一次
  ``GET {base_url}/models``，拉得到才出现在厂商下拉里；拉不到的不出现，原因
  写在状态行（不静默）；悬停厂商项即显示该厂商的全部可用模型——这份清单本来
  就来自这次请求，不额外发请求。
- **两条能力分开选**：顶部切「语义判断 / 语音转写」，各自回填当前的模型名。

边界（刻意不做的事）
--------------------
本页**只写模型名**（``judge_model`` / ``asr_model_name``），不动 ``enabled`` /
``allow_upload`` / ``backend_prefs``：「允许外发素材」是隐私授权，「测通才允许
启用」是硬门槛，两者都留在「模型管理 → 云端模型」。「生效」一行用
``block_reason()`` 如实说明还差什么（与日志、检查器同一份判据）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import AppConfig
from ...core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    CAP_LABELS,
    PROTO_OPENAI,
    ProviderProfile,
    block_reason,
)
from ..state import ProjectState
from ..widgets import Card, LabeledField, NoWheelComboBox
from ..workers import ModelScanWorker
from ._common import register_worker

__all__ = ["ModelPickForm"]

#: 悬停提示里最多列几个模型名：厂商清单可能上百个，全塞进 tooltip 会变成一道墙
HOVER_MODEL_LIMIT = 24

#: 能力 → 模型名写在档案的哪个字段（回填与保存都读这一份，不各写一遍）
MODEL_FIELDS = {CAP_JUDGE: "judge_model", CAP_ASR: "asr_model_name"}

#: 能力 → 看哪个协议字段。只有 OpenAI 兼容协议有 ``/models`` 清单；
#: 火山转写这类专有协议没有清单接口，因此不会出现在这里的候选里
PROTOCOL_FIELDS = {CAP_JUDGE: "judge_protocol", CAP_ASR: "asr_protocol"}


class ModelPickForm(QWidget):
    """能力切换 + 厂商下拉（悬停出清单）+ 模型下拉 + 状态行。"""

    def __init__(
        self,
        state: ProjectState,
        parent: QWidget | None = None,
        *,
        config_path=None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.setMinimumWidth(560)
        #: 测试注入点：默认写真实 config.json，测试指到临时文件
        self._config_path = config_path
        self._config = AppConfig.load(config_path)
        self._capability = CAP_JUDGE
        self._scan_worker = None
        #: profile_id → 模型清单（本次会话的探测结果；同一家厂商两条能力共用一份，
        #: 因为它们拉的是同一个 /models）
        self._models: dict[str, list[str]] = {}
        #: profile_id → 没连通的原因（用于状态行，不进下拉）
        self._blocked: dict[str, str] = {}
        #: 已探过（含失败）的 id：切能力来回点时不该反复打网络
        self._scanned: set[str] = set()
        #: 当前能力下"有 Key、可探测"的厂商 id（顺序 = 注册表顺序）
        self._targets: list[str] = []
        #: 填了 Key 却没填接口地址的厂商（这属于配置不完整，点名说清）
        self._need_url: list[str] = []
        #: 没配 Key 的厂商**数量**：只报家数不点名——十几家的名字会把状态行变成
        #: 一堵墙，而"哪家没配 Key"在「厂商管理」里一眼可见
        self._no_key = 0
        self._by_id: dict[str, ProviderProfile] = {}

        self._build()
        self.refresh()

    # ---- 装配 ----------------------------------------------------------

    def _build(self) -> None:
        self.radio_judge = QRadioButton("语义判断", self)
        self.radio_asr = QRadioButton("语音转写", self)
        self.radio_judge.setChecked(True)
        capacity_group = QButtonGroup(self)
        capacity_group.addButton(self.radio_judge)
        capacity_group.addButton(self.radio_asr)
        for radio in (self.radio_judge, self.radio_asr):
            radio.toggled.connect(self._on_capability_toggled)

        capacity_row = QHBoxLayout()
        capacity_row.setContentsMargins(0, 0, 0, 0)
        capacity_row.setSpacing(16)
        for radio, hint in (
            (self.radio_judge, "配云端对话模型（judge_model）"),
            (self.radio_asr, "配云端转写模型（asr_model_name）"),
        ):
            radio.setToolTip(hint)
            capacity_row.addWidget(radio)
        capacity_row.addStretch(1)

        self.provider_combo = NoWheelComboBox(self)
        self.provider_combo.setMaxVisibleItems(12)
        # 没有可用厂商时下拉是空的：占位文字让"空"看起来是"还没配 Key"，
        # 而不是"界面坏了"（QComboBox 只在 currentIndex 无效时显示它）
        self.provider_combo.setPlaceholderText("（没有可用厂商）")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.provider_field = LabeledField("厂商")
        self.provider_field.attach(self.provider_combo)

        self.model_combo = NoWheelComboBox(self)
        # 可编辑：清单里没有的模型名（厂商没在 /models 里列出、但确实可用）照样能填
        self.model_combo.setEditable(True)
        # 可编辑下拉的占位符必须设在它的 lineEdit 上：设在 combo 上在编辑态不显示
        self.model_combo.lineEdit().setPlaceholderText("（先选厂商，或直接输入模型名）")
        self.model_combo.setToolTip(
            "来自该厂商的 /models 接口；清单里没有的名字也可以直接输入。"
        )
        self.model_field = LabeledField("模型")
        self.model_field.attach(self.model_combo)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "muted")
        self.effect_label = QLabel("", self)
        self.effect_label.setWordWrap(True)
        self.effect_label.setProperty("role", "muted")
        self.hint_label = QLabel("", self)
        self.hint_label.setWordWrap(True)
        self.hint_label.setProperty("role", "muted")

        self.btn_save = QPushButton("保存模型名", self)
        self.btn_save.setProperty("role", "primary")
        self.btn_save.clicked.connect(self._on_save)

        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.setSpacing(12)
        save_row.addWidget(self.btn_save)
        save_row.addWidget(self.hint_label, 1)

        card = Card("选择模型", parent=self)
        card.body_layout.addLayout(capacity_row)
        card.body_layout.addWidget(self.provider_field)
        card.body_layout.addWidget(self.model_field)
        card.body_layout.addLayout(save_row)
        card.body_layout.addWidget(self.status_label)
        card.body_layout.addWidget(self.effect_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(card)
        outer.addStretch(1)

    # ---- 公开面（工具与测试用）------------------------------------------

    def set_capability(self, capability: str) -> None:
        """切换能力（``CAP_JUDGE`` / ``CAP_ASR``）；与点单选框等价。"""
        radio = self.radio_asr if capability == CAP_ASR else self.radio_judge
        radio.setChecked(True)

    def capability(self) -> str:
        return self._capability

    def available_ids(self) -> list[str]:
        """当前能力下"探测成功、出现在下拉里"的厂商 id。"""
        return [
            str(self.provider_combo.itemData(index))
            for index in range(self.provider_combo.count())
        ]

    def selected_profile_id(self) -> str | None:
        data = self.provider_combo.currentData()
        return str(data) if data else None

    def _selected_profile(self) -> ProviderProfile | None:
        profile_id = self.selected_profile_id()
        return self._by_id.get(profile_id) if profile_id else None

    def _label_of(self, profile_id: str) -> str:
        profile = self._by_id.get(profile_id)
        return profile.label if profile is not None else profile_id

    def refresh(self) -> None:
        """重新读盘 → 按当前能力筛候选 → 探测没探过的那些。

        每次都从磁盘读：厂商档案由「厂商管理」写、Key 由「模型管理」写，拿着
        构造时的旧副本会显示过期数据。
        """
        self._config = AppConfig.load(self._config_path)
        self._by_id = {p.id: p for p in self._config.profiles().all()}
        targets, need_url, no_key = self._candidates()
        self._targets = [p.id for p in targets]
        self._need_url = need_url
        self._no_key = no_key

        todo = [p for p in targets if p.id not in self._scanned]
        if not todo:
            self._rebuild_providers()
            self._refresh_status()
            return

        self._refresh_status()
        worker = ModelScanWorker(todo, self)
        worker.probed.connect(self._on_probed)
        worker.unresolved.connect(self._on_unresolved)
        # 绑定到具体 worker：切换能力时旧的一轮会被取消，它的结束信号不能当成
        # "本轮扫描完成"（否则状态行会写一半就被覆盖）
        worker.finished_ok.connect(lambda w=worker: self._on_scan_finished(w))
        self._scan_worker = worker
        register_worker(self, "model_scan", worker)
        worker.start()

    def shutdown(self) -> None:
        """对话框关闭时调用：停掉还在跑的探测，别留着线程等超时。"""
        if self._scan_worker is not None:
            self._scan_worker.cancel()

    # ---- 候选与探测 ----------------------------------------------------

    def _candidates(self) -> tuple[list[ProviderProfile], list[str], int]:
        """当前能力下"可探测"的厂商，以及被跳过者（缺地址的点名，缺 Key 的计数）。

        **没有清单接口的协议直接不参与**（如火山转写专有协议），它的模型名只能
        在「厂商管理」里填；因为清单接口只有 OpenAI 兼容协议有。
        """
        protocol_field = PROTOCOL_FIELDS[self._capability]
        targets: list[ProviderProfile] = []
        need_url: list[str] = []
        no_key = 0
        for profile in self._config.profiles().all():
            if getattr(profile, protocol_field) != PROTO_OPENAI:
                continue
            if not profile.base_url.strip():
                # 填了 Key 却没地址：这是配置会打不通的**具体原因**，点名
                if profile.resolved_api_key():
                    need_url.append(profile.label)
                else:
                    no_key += 1
                continue
            if not profile.resolved_api_key():
                no_key += 1
                continue
            targets.append(profile)
        return targets, need_url, no_key

    def _on_probed(self, profile_id: str, models) -> None:
        self._models[profile_id] = list(models)
        self._scanned.add(profile_id)
        self._rebuild_providers()
        self._refresh_status()

    def _on_unresolved(self, profile_id: str, reason: str) -> None:
        self._blocked[profile_id] = reason
        self._scanned.add(profile_id)
        self._refresh_status()

    def _on_scan_finished(self, worker) -> None:
        if worker is not self._scan_worker:
            return
        self._rebuild_providers()
        self._refresh_status()

    # ---- 下拉与状态 ----------------------------------------------------

    def _rebuild_providers(self) -> None:
        """按探测结果重建厂商下拉；只在有结果时才动当前选中项。"""
        order = [pid for pid in self._targets if pid in self._models]
        previous = self.selected_profile_id()
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for pid in order:
            profile = self._by_id.get(pid)
            self.provider_combo.addItem(profile.label if profile else pid, pid)
            self.provider_combo.setItemData(
                self.provider_combo.count() - 1,
                self._hover_text(pid),
                Qt.ItemDataRole.ToolTipRole,
            )
        # 预选：这条能力当前用的那家（在列表里的话）；否则第一项
        backend = self._config.backend_for(self._capability)
        if previous in order:
            index = self.provider_combo.findData(previous)
        else:
            index = self.provider_combo.findData(backend)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        self._on_provider_changed()

    def _hover_text(self, profile_id: str) -> str:
        models = self._models.get(profile_id, [])
        shown = "、".join(models[:HOVER_MODEL_LIMIT])
        if len(models) > HOVER_MODEL_LIMIT:
            shown += f"…（共 {len(models)} 个）"
        return f"可用模型（{len(models)} 个）：{shown}"

    def _on_capability_toggled(self, checked: bool) -> None:
        # 两个单选框各自会发一次 toggled：只在"被选中的那个"上走一遍
        if not checked:
            return
        self._capability = CAP_ASR if self.radio_asr.isChecked() else CAP_JUDGE
        self.hint_label.setText("")
        # 已探到的清单跨能力复用（同一家的 /models 是同一个），只重算候选与回填值
        self.refresh()

    def _on_provider_changed(self, *_args) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.model_combo.clear()
            self.provider_combo.setToolTip("")
            self.effect_label.setText("")
            return
        models = self._models.get(profile.id, [])
        current = str(getattr(profile, MODEL_FIELDS[self._capability], "") or "")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        # 用 setEditText 而不是 setText：NoWheelComboBox 是 QComboBox，
        # 后者根本没有 setText（会 AttributeError）
        self.model_combo.setEditText(current)
        self.model_combo.blockSignals(False)
        # 收起状态悬停在厂商框上也能看到清单（不必先展开列表去逐项悬停）
        self.provider_combo.setToolTip(self._hover_text(profile.id))
        self.hint_label.setText("")
        self._refresh_effect(profile)

    def _refresh_status(self) -> None:
        """状态行：探测进度 / 可用家数 / 未通过原因 / 未检测家数。"""
        parts: list[str] = []
        remaining = [pid for pid in self._targets if pid not in self._scanned]
        if remaining:
            done = len(self._targets) - len(remaining)
            parts.append(f"正在检测可用厂商…（已完成 {done}/{len(self._targets)}）")
        available = [pid for pid in self._targets if pid in self._models]
        if available:
            names = [self._label_of(pid) for pid in available]
            parts.append(f"可用 {len(names)} 家：{'、'.join(names)}。")
        elif not remaining and self._targets:
            parts.append("这些厂商都没连通，暂时没有可选的模型列表。")
        elif not remaining:
            if self._no_key:
                parts.append(
                    f"还没有配好 API Key 的厂商（{self._no_key} 家未检测）：先在"
                    "「厂商管理」里填 Key，或设环境变量 DRAMA_<厂商 id>_API_KEY。"
                )
            else:
                parts.append("没有走 OpenAI 兼容协议、能拉取模型清单的厂商。")
        # 只报"当前这条能力"里的失败项：切能力后上一轮的失败不该继续挂在状态行上
        blocked = [(pid, reason) for pid, reason in self._blocked.items() if pid in self._targets]
        if blocked:
            items = [
                f"{self._label_of(pid)}：{reason.rstrip('。')}" for pid, reason in blocked
            ]
            parts.append("未通过——" + "；".join(items) + "。")
        if self._need_url:
            parts.append(f"填了 Key 但缺接口地址：{'、'.join(self._need_url)}。")
        if self._no_key and self._targets:
            parts.append(f"另有 {self._no_key} 家没配 API Key，未检测。")
        self.status_label.setText(" ".join(parts))

    def _refresh_effect(self, profile: ProviderProfile, *, saved: bool = False) -> None:
        """「生效」一行：这条能力现在到底会不会走这家、还差什么。"""
        label = CAP_LABELS[self._capability]
        model = str(getattr(profile, MODEL_FIELDS[self._capability], "") or "（未填）")
        head = f"已保存：{label} = {model}（{profile.label}）。" if saved else ""
        reason = block_reason(profile, self._capability)
        if reason is not None:
            self.effect_label.setText(
                f"{head}尚未生效：{reason}。在「模型管理 → 云端模型」里处理。"
            )
            return
        if self._config.backend_for(self._capability) != profile.id:
            self.effect_label.setText(
                f"{head}档案可用，但这条能力当前没用它——在「模型管理 → 云端模型」里"
                f"勾选「{label}使用云端」并测通，就会切到 {model}。"
            )
            return
        self.effect_label.setText(f"{head}已生效：云端{label}用的是 {model}。")

    # ---- 保存 ----------------------------------------------------------

    def _on_save(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.hint_label.setText("先选一家厂商（列表里只有已配 Key 且能连通的厂商）。")
            return
        model = self.model_combo.currentText().strip()
        if not model:
            self.hint_label.setText("模型名不能为空：从下拉里选一个，或直接输入。")
            return
        updated = profile.with_updates(**{MODEL_FIELDS[self._capability]: model})
        if not self._store_profile(updated):
            return
        self._by_id[updated.id] = updated
        self.hint_label.setText("")
        self._refresh_effect(updated, saved=True)

    def _store_profile(self, profile: ProviderProfile) -> bool:
        """按 id 合并一份档案回配置并落盘（与「模型管理」的云端卡片同一套写法）。"""
        items = [dict(item) for item in self._config.providers]
        for index, item in enumerate(items):
            if str(item.get("id")) == profile.id:
                items[index] = profile.to_json()
                break
        else:
            items.append(profile.to_json())
        self._config.providers = items
        try:
            self._config.save(self._config_path)
        except OSError as exc:
            self.hint_label.setText(f"配置写入失败：{exc}")
            return False
        return True