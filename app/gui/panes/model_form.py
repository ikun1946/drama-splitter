"""模型表单：按用途分组的下载 / 删除 / 「使用中」切换。

界面为什么这样组织
------------------
旧版把"选中哪一行"（要操作谁）与"已安装 / 未安装"（状态）混在一张表里：
选中态只是淡底色，用户看不出选没选中；而"分析时真正会用哪个模型"既不
可见也不可控（转写档位写死在代码里、语义模型按体积自动挑）。

现在：

- **按用途分两组卡片**：语音转写（whisper 五档全列）、语义判断（本机已装档位）；
- **每行自带动作按钮**（未装→下载 / 已装→删除），不再有"先选行再点底部按钮"；
- **每行一个「使用中」圆钮**（仅已安装行可点）。它写进 ``AppConfig``，
  ``workers`` 在真实分析时按它取模型——标记的与用到的是同一个东西。

「使用中」说的是**本地模型**；若把某项能力切到云端（见下方「云端模型」卡），
则由厂商 API 承担，本地档位只在该能力回退本地时才用得上。

由 ``app/gui/dialogs/model_dialog.py`` 的 ModelDialog 承载。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import LOCAL_BACKEND, AppConfig, default_models_root
from ...core.cloud import build_asr_transport, build_judge_transport
from ...core.model_manager import (
    WHISPER_TIERS,
    default_whisper_tier,
    delete_model,
    free_space_bytes,
    installed_llm_file,
    installed_llm_specs,
    installed_state,
    list_remote_gguf,
    llm_spec,
    spec_by_key,
    whisper_spec,
)
from ...core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    CAP_LABELS,
    ENV_KEY_TEMPLATE,
    PROTO_LABELS,
    ProviderError,
    describe_egress,
    missing_config_reason,
)
from ..state import ProjectState
from ..theme import current_mode, qcolor
from ..widgets import (
    Card,
    LabeledField,
    LogPanel,
    NoWheelComboBox,
    StatusPill,
)
from ..widgets import message
from ..workers import (
    JudgeProbeWorker,
    ModelDownloadWorker,
)
from ._common import (
    register_worker,
)

__all__ = ["ModelForm"]

#: 语义模型默认下载档位（与 MODEL_CATALOG 的预置项一致）
DEFAULT_QUANT = "Q4_K_M"


def _short_label(spec) -> str:
    """组内显示用的短名。组标题已说明用途，不必重复 "语音转写"/"语义判断" 前缀。"""
    parts = spec.key.split(":")
    if spec.kind == "whisper":
        return f"whisper-{parts[1]}"
    return f"Qwen3.5-{parts[1]} {parts[2]}"


def _clear_layout(layout) -> None:
    """清空布局并**立刻**摘掉旧控件。

    只 ``deleteLater()`` 不 ``setParent(None)`` 的话，旧控件要到事件循环
    空转时才消失——刷新期间用户会看到新旧两批行叠在一起。
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())
            item.layout().deleteLater()


class _ModelRow(QFrame):
    """一行模型：使用中圆钮 + 名称 + 体积 + 状态徽章 + 行内动作。

    行内动作（下载 / 删除）取代了旧的"选中行"隐式交互：按钮就在这一行上，
    "要对谁动手"不再需要靠底色去猜。
    """

    picked = Signal(object)       # ModelSpec：用户把它设为「使用中」
    action = Signal(object, str)  # ModelSpec, "download" | "delete"

    def __init__(self, spec, state, *, in_use: bool, busy: bool, parent=None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setToolTip(f"{spec.label}\n{spec.note}\n{state.describe()}")

        self.radio = QRadioButton(self)
        self.radio.setChecked(in_use)
        # 未安装的行不可设为「使用中」：否则会出现"选了却用不了"的假状态
        self.radio.setEnabled(state.ready)
        self.radio.setToolTip(
            "分析时使用这个模型" if state.ready else "先下载该模型，才能把它设为使用中"
        )
        # 用 clicked 而不是 toggled：刷新时的 setChecked 不该被当成用户选择
        self.radio.clicked.connect(lambda: self.picked.emit(self.spec))

        name = QLabel(_short_label(spec), self)
        if in_use:
            font = name.font()
            font.setBold(True)
            name.setFont(font)

        size = QLabel(self)
        size.setProperty("role", "muted")
        size.setText(f"{spec.approx_mb} MB" if spec.approx_mb else "体积未知（下载时探测）")

        if state.ready:
            pill = StatusPill("已安装", tone="success", parent=self)
        elif state.partial:
            pill = StatusPill("不完整", tone="warn", parent=self)
        else:
            pill = StatusPill("未安装", tone="muted", parent=self)
        pill.setToolTip(state.describe())

        self.button = QPushButton(self)
        if state.ready:
            self.button.setText("删除")
            self.button.setProperty("role", "danger")
            self.button.clicked.connect(lambda: self.action.emit(self.spec, "delete"))
        else:
            self.button.setText("继续下载" if state.partial else "下载")
            self.button.setProperty("role", "primary")
            self.button.clicked.connect(lambda: self.action.emit(self.spec, "download"))
        # dynamic property 要重新 polish 才生效（与旧版的下载按钮同一处理）
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)
        self.button.setEnabled(not busy)

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 4, 2, 4)
        row.setSpacing(10)
        row.addWidget(self.radio)
        row.addWidget(name)
        row.addWidget(size)
        row.addWidget(pill)
        row.addStretch(1)
        row.addWidget(self.button)


class ModelForm(QWidget):
    """模型管理与「使用中」切换。"""

    #: 请求打开「厂商管理」对话框（携带配置路径，便于注入到临时文件）
    manage_providers_requested = Signal(object)

    LOG_COLLAPSED = 64    # 折叠时约三行
    LOG_EXPANDED = 220

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setMinimumWidth(680)
        self._worker: "ModelDownloadWorker | None" = None
        self._probe: "JudgeProbeWorker | None" = None
        self._config = AppConfig.load()
        # 测试注入点：默认写真实的 config.json，测试指到临时文件，避免污染用户配置
        self._config_path: Path | None = None
        self._rows: list[_ModelRow] = []
        # 两组各一个互斥组，只建一次（行销毁时 Qt 会自动把按钮摘出去）
        self._asr_group = QButtonGroup(self)
        self._llm_group = QButtonGroup(self)

        header = QLabel(
            "按用途分为两组：语音转写与语义判断相互独立，可只装其一。"
            "每行右侧直接操作；标着「使用中」的那一行，才是分析时真正会用的模型。"
        )
        header.setProperty("role", "muted")
        header.setWordWrap(True)

        # ---- 语音转写 --------------------------------------------------
        self.asr_card = Card("语音转写", parent=self)
        self.asr_status = QLabel("", self.asr_card)
        self.asr_status.setProperty("role", "muted")
        self.asr_card.add_header_widget(self.asr_status)
        self._asr_rows = QVBoxLayout()
        self._asr_rows.setContentsMargins(0, 0, 0, 0)
        self._asr_rows.setSpacing(2)
        self.asr_card.body_layout.addLayout(self._asr_rows)
        asr_hint = QLabel(
            "档位越大越准、也越慢。切换档位后重新分析会自动重跑转写"
            "（转写缓存按档位分开存，不会串用旧结果）。",
            self.asr_card,
        )
        asr_hint.setProperty("role", "muted")
        asr_hint.setWordWrap(True)
        self.asr_card.body_layout.addWidget(asr_hint)

        # ---- 语义判断 --------------------------------------------------
        self.llm_card = Card("语义判断", parent=self)
        self.llm_status = QLabel("", self.llm_card)
        self.llm_status.setProperty("role", "muted")
        self.llm_card.add_header_widget(self.llm_status)
        self._llm_rows = QVBoxLayout()
        self._llm_rows.setContentsMargins(0, 0, 0, 0)
        self._llm_rows.setSpacing(2)
        self.llm_card.body_layout.addLayout(self._llm_rows)

        self.unknown_label = QLabel("", self.llm_card)
        self.unknown_label.setProperty("role", "muted")
        self.unknown_label.setWordWrap(True)
        self.unknown_label.setVisible(False)
        self.llm_card.body_layout.addWidget(self.unknown_label)

        self.quant_combo = NoWheelComboBox(self.llm_card)
        self.quant_combo.addItem(DEFAULT_QUANT)
        self.btn_discover = QPushButton("获取可用档位", self.llm_card)
        self.btn_discover.clicked.connect(self._discover_quants)
        self.btn_download_quant = QPushButton("下载", self.llm_card)
        self.btn_download_quant.setProperty("role", "primary")
        self.btn_download_quant.style().unpolish(self.btn_download_quant)
        self.btn_download_quant.style().polish(self.btn_download_quant)
        self.btn_download_quant.clicked.connect(self._download_quant)
        quant_row = QHBoxLayout()
        quant_row.addWidget(QLabel("其他量化档位（Qwen3.5-2B）：", self.llm_card))
        quant_row.addWidget(self.quant_combo, 1)
        quant_row.addWidget(self.btn_discover)
        quant_row.addWidget(self.btn_download_quant)
        self.llm_card.body_layout.addLayout(quant_row)

        self.stack_label = QLabel("推理栈：探测中…", self.llm_card)
        self.stack_label.setProperty("role", "muted")
        self.stack_label.setWordWrap(True)
        self.llm_card.body_layout.addWidget(self.stack_label)

        # ---- 模型目录 --------------------------------------------------
        self.dir_edit = QLineEdit(str(self._config.resolved_models_dir()), self)
        self.btn_pick_dir = QPushButton("更改目录…", self)
        self.btn_pick_dir.clicked.connect(self._pick_dir)
        self.free_label = QLabel("", self)
        self.free_label.setProperty("role", "muted")
        self.dir_card = Card("模型目录", parent=self)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(self.btn_pick_dir)
        self.dir_card.body_layout.addLayout(dir_row)
        self.dir_card.body_layout.addWidget(self.free_label)

        # ---- 云端模型卡片（厂商 API）----------------------------------
        #
        # 默认全关：档案未启用、未勾选「允许外发素材」时零外发（见 providers.py）。
        # 「启用」不是随手勾的开关——必须点「保存并测试连通性」通过之后才允许，
        # 否则会出现"以为通了、真正分析时才回退本地"。
        #
        # 版式：地址与模型名并成两列、能力开关收进分组框并各带一行说明。
        # 此前是"标签＋输入框"一路平铺下来，卡片独占整页近一半高度，
        # 读的人找不到"哪几项是凭据、哪几项是能力开关"。
        self.cloud_card = Card("云端模型（厂商 API）", parent=self, body_layout=QVBoxLayout())
        self.cloud_combo = NoWheelComboBox()
        self.cloud_combo.setMaxVisibleItems(12)   # 预置厂商有十几家，别拉成一长条
        self.cloud_combo.currentIndexChanged.connect(self._on_cloud_profile_changed)
        self.btn_manage_providers = QPushButton("管理厂商…")
        self.btn_manage_providers.clicked.connect(self._request_manage_providers)
        self.cloud_key = QLineEdit()
        self.cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_key.setPlaceholderText("明文保存在本地配置文件，可被环境变量覆盖")
        # 外发范围写在勾选框上而不是提示行里：提示行只在配好模型后才说得清"发往哪台主机"，
        # 而"会发什么"必须在这一刻就能读到——勾这个框就是在授权外发。
        self.cloud_allow = QCheckBox(
            "允许外发素材（转写＝整条音轨；语义判断＝候选点前后台词文本，不含画面）"
        )
        self.cloud_enable = QCheckBox("语义判断使用云端")
        self.cloud_asr_enable = QCheckBox("语音转写使用云端")
        self.btn_cloud_save = QPushButton("保存并测试连通性")
        self.btn_cloud_save.clicked.connect(self._save_cloud)
        self.cloud_hint = QLabel("")
        self.cloud_hint.setWordWrap(True)
        self.cloud_hint.setProperty("role", "muted")
        # 地址与模型名搬去「厂商管理」后，这一行必须把"现在到底怎么答的"摆出来，
        # 否则用户改完协议回到本页，看不出改没改到、生效没有。
        self.cloud_preview = QLabel("")
        self.cloud_preview.setWordWrap(True)
        self.cloud_preview.setProperty("role", "muted")
        cloud_note = QLabel(
            "两条能力相互独立，可只用其一；未启用时走本地模型（转写＝「使用中」的 whisper，"
            "语义判断＝llama.cpp / Ollama 里「使用中」的档位）。地址、协议与模型名在"
            "「管理厂商…」里改；勾选后必须先「保存并测试连通性」，测通的那一项才会真正切到云端。"
        )
        cloud_note.setWordWrap(True)
        cloud_note.setProperty("role", "muted")

        profile_field = LabeledField("厂商档案")
        profile_field.attach(self.cloud_combo)
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.setSpacing(12)
        profile_row.addWidget(profile_field, 3)
        profile_row.addWidget(
            self.btn_manage_providers, 0, Qt.AlignmentFlag.AlignBottom
        )

        key_field = LabeledField("API Key")
        key_field.attach(self.cloud_key)

        # 两条能力各占一行：勾选框 + 它自己的外发范围说明。
        # 说明不塞进勾选框文本，否则一行长句会把"勾的是什么"淹掉。
        ability_box = QGroupBox("云端能力开关", parent=self)
        ability_grid = QGridLayout(ability_box)
        ability_grid.setContentsMargins(12, 8, 12, 10)
        ability_grid.setHorizontalSpacing(16)
        ability_grid.setVerticalSpacing(6)
        for row, (switch, note) in enumerate(
            (
                (self.cloud_enable, "只发候选点前后台词文本"),
                (self.cloud_asr_enable, "整条音轨分块上传，约 10 分钟/块"),
            )
        ):
            note_label = QLabel(note)
            note_label.setProperty("role", "muted")
            ability_grid.addWidget(switch, row, 0)
            ability_grid.addWidget(note_label, row, 1)
        ability_grid.setColumnStretch(2, 1)

        cloud_action_row = QHBoxLayout()
        cloud_action_row.setContentsMargins(0, 0, 0, 0)
        cloud_action_row.setSpacing(12)
        cloud_action_row.addWidget(self.btn_cloud_save)
        cloud_action_row.addWidget(self.cloud_hint, 1)

        self.cloud_card.body_layout.addLayout(profile_row)
        self.cloud_card.body_layout.addWidget(self.cloud_preview)
        self.cloud_card.body_layout.addWidget(key_field)
        self.cloud_card.body_layout.addWidget(self.cloud_allow)
        self.cloud_card.body_layout.addWidget(ability_box)
        self.cloud_card.body_layout.addLayout(cloud_action_row)
        self.cloud_card.body_layout.addWidget(cloud_note)

        # ---- 进度（只在下load时出现）-----------------------------------
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.btn_cancel = QPushButton("取消下载", self)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.btn_cancel)
        self.progress_host = QWidget(self)
        self.progress_host.setLayout(progress_row)
        self.progress_host.setVisible(False)

        # ---- 日志（默认小条，可展开）------------------------------------
        self.log = LogPanel(self)
        self.log.setFixedHeight(self.LOG_COLLAPSED)
        self.log_card = Card("日志", parent=self)
        self.btn_log_toggle = QPushButton("展开", self.log_card)
        self.btn_log_toggle.setCheckable(True)
        self.btn_log_toggle.toggled.connect(self._toggle_log)
        self.log_card.add_header_widget(self.btn_log_toggle)
        self.log_card.body_layout.addWidget(self.log)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(header)
        outer.addWidget(self.asr_card)
        outer.addWidget(self.llm_card)
        outer.addWidget(self.dir_card)
        outer.addWidget(self.cloud_card)
        outer.addWidget(self.progress_host)
        outer.addWidget(self.log_card)
        outer.addStretch(1)

        self.refresh()
        self._load_cloud_fields()
        self._start_probe()

    # ---- 公开面（工具与测试用）------------------------------------------

    def refresh(self) -> None:
        """按磁盘实际状态重建两组卡片。"""
        root = self._models_root()
        busy = bool(self._worker and self._worker.isRunning())
        self._rows = []

        active_tier = self.active_whisper_tier()
        _clear_layout(self._asr_rows)
        for tier, _, _ in WHISPER_TIERS:
            self._append_row(
                self._asr_rows, self._asr_group, whisper_spec(tier), root,
                in_use=tier == active_tier, busy=busy,
            )

        active_llm = self.active_llm_key()
        llm_specs = installed_llm_specs(root)
        if active_llm and all(spec.key != active_llm for spec in llm_specs):
            pinned = spec_by_key(active_llm)
            # 「使用中」的档位已被删掉时也要列出来：否则用户只看到红字，
            # 却不知道是哪一个、也无从重新下载。
            if pinned is not None:
                llm_specs.insert(0, pinned)
        _clear_layout(self._llm_rows)
        if not llm_specs:
            empty = QLabel(
                "本机尚未安装语义模型 → 候选只做规则评分，不做语义判断。", self.llm_card
            )
            empty.setProperty("role", "muted")
            empty.setWordWrap(True)
            self._llm_rows.addWidget(empty)
        for spec in llm_specs:
            self._append_row(
                self._llm_rows, self._llm_group, spec, root,
                in_use=spec.key == active_llm, busy=busy,
            )

        self._refresh_status(root)
        self._refresh_dir(root, busy)

    def rows(self) -> list[dict]:
        """当前界面上的每一行（工具与测试用；对应于界面所见）。"""
        root = self._models_root()
        result = []
        for row in self._rows:
            state = installed_state(row.spec, root)
            result.append(
                {
                    "kind": row.spec.kind,
                    "key": row.spec.key,
                    "label": _short_label(row.spec),
                    "size_text": (
                        f"{row.spec.approx_mb} MB"
                        if row.spec.approx_mb
                        else "未知（下载时探测）"
                    ),
                    "state": state.describe(),
                    "installed": state.ready,
                    "in_use": row.radio.isChecked(),
                    "pickable": row.radio.isEnabled(),
                    "action": row.button.text(),
                }
            )
        return result

    def active_whisper_tier(self) -> str:
        """本次分析会用的转写档位（必须有值：未配置时用默认档位）。"""
        return self._config.asr_model_tier or default_whisper_tier()

    def active_llm_key(self) -> str | None:
        """本次分析会用的语义模型 key；``None`` 表示没有可用模型（降级）。

        未显式指定时返回**自动挑选**的那个（按体积最大，与 ``create_judge``
        走 Ollama 时的规则一致）——界面上必须能指出"正在用哪个"，
        而不是含糊地说一句"自动"。
        """
        if self._config.llm_model_key:
            return self._config.llm_model_key
        spec = self._auto_llm_spec()
        return spec.key if spec is not None else None

    def _auto_llm_spec(self):
        """自动挑选的语义模型：权重文件最大的那一个（与 locate_llm_model 同规则）。"""
        root = self._models_root()
        best, best_bytes = None, -1
        for spec in installed_llm_specs(root):
            path = installed_llm_file(spec, root)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > best_bytes:
                best, best_bytes = spec, size
        return best

    def _refresh(self) -> None:
        """旧名（工具里在用的）。"""
        self.refresh()

    # ---- 行 ------------------------------------------------------------

    def _append_row(self, layout, group, spec, root, *, in_use: bool, busy: bool) -> None:
        state = installed_state(spec, root)
        row = _ModelRow(spec, state, in_use=in_use, busy=busy, parent=self)
        row.picked.connect(self._on_pick)
        row.action.connect(self._on_action)
        group.addButton(row.radio)
        layout.addWidget(row)
        self._rows.append(row)

    def _on_pick(self, spec) -> None:
        """把某行设为「使用中」并落盘（幂等：重复点同一行不再写盘）。"""
        if spec.kind == "whisper":
            tier = spec.key.split(":", 1)[1]
            if self._config.asr_model_tier == tier:
                return
            self._config.asr_model_tier = tier
            what = "转写档位"
        else:
            if self._config.llm_model_key == spec.key:
                return
            self._config.llm_model_key = spec.key
            what = "语义模型"
        if self._save_config():
            self._append(f"{what}已切换为 {_short_label(spec)}，下次分析生效。")
        else:
            self._append(
                f"{what}已临时切到 {_short_label(spec)}，但没写进配置"
                "（重启后会回到原值）。",
                "warn",
            )
        self.refresh()

    def _on_action(self, spec, kind: str) -> None:
        if kind == "delete":
            self._delete(spec)
        else:
            self._start_download(spec)

    # ---- 目录 ----------------------------------------------------------

    def _models_root(self) -> Path:
        return Path(self.dir_edit.text().strip() or str(default_models_root()))

    def _save_config(self) -> bool:
        """把配置落盘（`_config_path` 为测试注入点）；失败只记一行，不抛。

        返回是否写入成功——调用方据此决定该说"已切换"还是"没写进配置"。
        """
        try:
            self._config.save(self._config_path)
        except OSError as exc:
            self._append(f"配置写入失败：{exc}", "error")
            return False
        return True

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择模型目录", str(self._models_root())
        )
        if not chosen:
            return
        self.dir_edit.setText(chosen)
        self._config.models_dir = chosen
        if self._save_config():
            self._append(f"模型目录已改为 {chosen}（已记住，重启后仍生效）")
        else:
            self._append("目录已切换，但没记住（重启后需重新选择）。", "warn")
        self.refresh()

    def _refresh_dir(self, root: Path, busy: bool) -> None:
        free = free_space_bytes(root)
        if free is None:
            self.free_label.setText("磁盘可用空间：未知")
        else:
            self.free_label.setText(
                f"磁盘可用空间：{free / 1024 / 1024 / 1024:.1f} GB"
                "（下载前会再检查一次，空间不足会明确拒绝）"
            )
        self.btn_discover.setEnabled(not busy)
        self.btn_download_quant.setEnabled(not busy)

    # ---- 状态文案 ------------------------------------------------------

    def _refresh_status(self, root: Path) -> None:
        danger = f"color: {qcolor(current_mode(), 'DANGER')};"

        tier = self.active_whisper_tier()
        if installed_state(whisper_spec(tier), root).ready:
            self.asr_status.setText(f"使用中：whisper-{tier}")
            self.asr_status.setStyleSheet("")
        else:
            # 不静默换档：分析确实会用这个档位，缺了就跳过转写，必须提前说清
            self.asr_status.setText(f"使用中：whisper-{tier}（未安装，分析会跳过转写）")
            self.asr_status.setStyleSheet(danger)

        key = self.active_llm_key()
        pinned = self._config.llm_model_key
        if key:
            spec = spec_by_key(key)
            name = _short_label(spec) if spec is not None else key
            if spec is not None and installed_llm_file(spec, root) is not None:
                tail = "" if pinned else "（自动选择，点圆钮可固定）"
                self.llm_status.setText(f"使用中：{name}{tail}")
                self.llm_status.setStyleSheet("")
            else:
                fallback = self._auto_llm_spec()
                tail = f" → 用 {_short_label(fallback)} 兜底" if fallback else " → 语义判断降级"
                self.llm_status.setText(f"使用中：{name}（未安装{tail}）")
                self.llm_status.setStyleSheet(danger)
        else:
            self.llm_status.setText("未启用（候选退回规则评分）")
            self.llm_status.setStyleSheet("")

        unknown = self._unrecognized_gguf(root)
        if unknown:
            names = "、".join(unknown[:3]) + ("…" if len(unknown) > 3 else "")
            self.unknown_label.setText(
                f"忽略 {len(unknown)} 个无法识别的 GGUF（名字不符合 "
                f"Qwen3.5-{'{大小}'}-{'{量化}'}.gguf）：{names}"
            )
        self.unknown_label.setVisible(bool(unknown))

    def _unrecognized_gguf(self, root: Path) -> list[str]:
        """`llm/` 里解析不出档位的 GGUF：列出来，避免"文件在却看不见"。"""
        known = {spec.files[-1].name for spec in installed_llm_specs(root)}
        directory = root / "llm"
        if not directory.exists():
            return []
        try:
            return sorted(
                path.name for path in directory.glob("*/*.gguf") if path.name not in known
            )
        except OSError:
            return []

    # ---- 推理栈探测 ----------------------------------------------------

    def _start_probe(self) -> None:
        """后台探测推理栈：Ollama 探测最长 3 秒，绝不放主线程。"""
        self._probe = JudgeProbeWorker(parent=self)
        self._probe.probed.connect(self._on_probed)
        register_worker(self, "judge_probe", self._probe)
        self._probe.start()

    def _on_probed(self, description) -> None:
        if description:
            self.stack_label.setText(f"语义判断后端：{description}")
        else:
            self.stack_label.setText(
                "语义判断后端：不可用 → 候选退回规则评分"
                "（本地：装好下面任一档位并让 Ollama 提供它；"
                "云端：在「云端模型」卡里填好并测通）"
            )

    # ---- 下载 ----------------------------------------------------------

    def _discover_quants(self) -> None:
        repo = "unsloth/Qwen3.5-2B-GGUF"
        self._append(f"查询 {repo} 可用量化档位…")
        names = list_remote_gguf(repo)
        if not names:
            self._append("查询失败或无结果（网络/镜像不可达）。可直接用默认的 Q4_K_M。", "warn")
            return
        quants = set()
        for name in names:
            stem = name.rsplit(".", 1)[0]
            base = stem.rsplit("-", 1)[-1]
            if base:
                quants.add(base)
        discovered = sorted(quants)
        self.quant_combo.clear()
        self.quant_combo.addItems(discovered)
        self._append(f"发现 {len(discovered)} 个档位：{'、'.join(discovered)}")

    def _download_quant(self) -> None:
        quant = self.quant_combo.currentText().strip() or DEFAULT_QUANT
        self._start_download(llm_spec("2B", quant))

    def _start_download(self, spec) -> None:
        if self._worker is not None and self._worker.isRunning():
            message.information(self, "下载进行中", "请等当前下载结束，或先取消。")
            return
        self._append(f"开始下载 {spec.describe()}…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_host.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self._worker = ModelDownloadWorker(
            spec, self._models_root(), endpoint=self._config.endpoint, parent=self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append)
        self._worker.finished_with.connect(self._on_finished)
        register_worker(self, "download", self._worker)
        self.refresh()
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str, index: int, count: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done / total * 100))
            self.progress.setFormat(
                f"{name}（{index}/{count}）{done / 1024 / 1024:.0f}/"
                f"{total / 1024 / 1024:.0f} MB"
            )
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"{name} 已下载 {done / 1024 / 1024:.0f} MB（总大小未知）")

    def _on_finished(self, result) -> None:
        self.progress.setRange(0, 100)
        self.progress_host.setVisible(False)
        self.btn_cancel.setEnabled(False)
        if result.ok:
            self._append(f"下载完成：{result.spec.describe()}")
            self.refresh()
            message.information(
                self,
                "下载完成",
                f"{result.describe()}\n\n即可直接使用，无需重启。"
                "要指定用这一个，请在对应那一行点「使用中」。",
            )
        elif result.cancelled:
            self._append("已取消，临时文件已清理。", "warn")
            self.refresh()
        else:
            self.refresh()
            message.warning(self, "下载未完成", result.describe())

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self.btn_cancel.setEnabled(False)

    def _delete(self, spec) -> None:
        agreed = message.confirm(
            self,
            "确认删除",
            f"删除 {spec.label}？\n目录：{spec.target_dir(self._models_root())}\n"
            "只删除该模型自己的目录，不影响其他模型。",
            ok_text="删除",
        )
        if not agreed:
            return
        ok, note = delete_model(spec, self._models_root())
        self._append(note, "info" if ok else "warn")
        if ok and (
            self._config.asr_model_tier == spec.key.split(":", 1)[1]
            or self._config.llm_model_key == spec.key
        ):
            # 不偷偷改配置：把"现在用不了"如实说出来，由用户决定换档还是重下
            self._append(
                f"注意：{_short_label(spec)} 正是「使用中」的模型，"
                "在那之前分析会跳过它或用其他已装档位。",
                "warn",
            )
        self.refresh()

    # ---- 日志 ----------------------------------------------------------

    def _toggle_log(self, expanded: bool) -> None:
        self.btn_log_toggle.setText("收起" if expanded else "展开")
        self.log.setFixedHeight(self.LOG_EXPANDED if expanded else self.LOG_COLLAPSED)

    def _append(self, text: str, level: str = "info") -> None:
        self.log.append(text, level=level)

    # ---- 云端模型（厂商 API）-------------------------------------------

    def _cloud_profile(self):
        """当前下拉选中的档案（注册表里至少有一个内置档案）。"""
        profile_id = self.cloud_combo.currentData()
        if not profile_id:
            return None
        return self._config.profiles().get(str(profile_id))

    def _on_cloud_profile_changed(self, *_args) -> None:
        self._load_cloud_fields()

    def _load_cloud_fields(self) -> None:
        """把配置回填到控件；下拉按当前后端偏好预选。

        注：后端偏好可能是 "local"（用户选了本地），此时下拉仍停在第一个档案上，
        但勾选态来自档案自身的 enabled，不会假装已启用。
        """
        registry = self._config.profiles()
        self.cloud_combo.blockSignals(True)
        self.cloud_combo.clear()
        for profile in registry.all():
            self.cloud_combo.addItem(profile.label, profile.id)
        backend = self._config.backend_for(CAP_JUDGE)
        if backend == LOCAL_BACKEND:
            backend = self._config.backend_for(CAP_ASR)
        index = self.cloud_combo.findData(backend)
        self.cloud_combo.setCurrentIndex(max(0, index))
        self.cloud_combo.blockSignals(False)

        profile = self._cloud_profile()
        if profile is None:
            self.cloud_preview.setText("")
            return
        self.cloud_key.setText(profile.api_key)
        self.cloud_allow.setChecked(profile.allow_upload)
        self.cloud_enable.setChecked(self._capability_uses_cloud(profile, CAP_JUDGE))
        self.cloud_asr_enable.setChecked(self._capability_uses_cloud(profile, CAP_ASR))
        self._refresh_cloud_preview(profile)
        self._refresh_cloud_hint()

    def _refresh_cloud_preview(self, profile) -> None:
        """只读预览：这家厂商的两条能力各用什么协议、发往哪台主机、用的是哪个模型。

        地址与模型名搬去「厂商管理」后，本页必须留下这一行：否则用户改完协议回来，
        看不出"现在到底会怎么发"，也无从核对是不是改对了。
        """
        judge_protocol = PROTO_LABELS.get(profile.judge_protocol, profile.judge_protocol)
        asr_protocol = PROTO_LABELS.get(profile.asr_protocol, profile.asr_protocol)
        self.cloud_preview.setText(
            "协议：语义判断 = "
            f"{judge_protocol}，转写 = {asr_protocol}\n"
            f"接口：{profile.base_url or '（未填）'}\n"
            f"模型：语义判断 = {profile.judge_model or '（未填）'}，"
            f"转写 = {profile.asr_model_name or '（未填）'}\n"
            f"{describe_egress(profile)}"
        )

    def _capability_uses_cloud(self, profile, capability: str) -> bool:
        """该能力此刻是否真的走这家云端（选了 + 档案已测通）。"""
        return profile.enabled and self._config.backend_for(capability) == profile.id

    def _request_manage_providers(self) -> None:
        """请求主窗口打开「厂商管理」（本分区不自己开对话框，见 panes/__init__ 的约定）。"""
        self.manage_providers_requested.emit(self._config_path)

    def reload_after_provider_edit(self) -> None:
        """厂商管理关闭后回来：重新读盘并刷新本页。

        刻意**重新 load** 而不是共用同一个 AppConfig 对象——厂商对话框自己会保存
        配置，两边各持一份可变对象必然互相覆盖。
        """
        self._config = AppConfig.load(self._config_path)
        self._load_cloud_fields()
        self.refresh()

    def _probe_cloud(self, profile, capability: str) -> str | None:
        """连通性测试的接缝：返回错误描述；可用时返回 None。

        单独一个方法是为了让测试能替换掉它——界面测试不允许真联网。
        能力是否**配置完整**统一问 `missing_config_reason()`（与日志/检查器同一份
        判据），否则多协议之后这里会与核心层各说各话。
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
            protocol = profile.judge_protocol if capability == CAP_JUDGE else profile.asr_protocol
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
        # 有的厂商不接受静音样片，只能退化为"验过鉴权"：如实写进日志，不假装全测过
        note = getattr(transport, "probe_note", "")
        if note:
            self._append(
                f"{profile.label}／{CAP_LABELS.get(capability, capability)}：{note}",
                "warn",
            )
        return None

    def _save_cloud(self) -> None:
        """保存本页的开关与 Key → 逐能力测连通 → 只把测通的能力切到云端。

        失败一律**不允许启用**：否则用户会以为通了，直到真正分析时才回退本地。
        地址/协议/模型名由「厂商管理」负责，这里只动本页的字段。
        """
        profile = self._cloud_profile()
        if profile is None:
            self._append("没有可用的厂商档案。", "error")
            return
        updated = profile.with_updates(
            api_key=self.cloud_key.text().strip(),
            allow_upload=self.cloud_allow.isChecked(),
        )
        self._store_profile(updated)

        results: dict[str, str | None] = {}
        for capability, checkbox in self._cloud_switches():
            if not checkbox.isChecked():
                results[capability] = None       # 没勾就不测（也不算启用）
                continue
            error = self._probe_cloud(updated, capability)
            results[capability] = error
            if error is not None:
                checkbox.setChecked(False)
                self._append(
                    f"{CAP_LABELS.get(capability, capability)}连通性测试未通过：{error}",
                    "error",
                )

        enabled_any = False
        passed: list[str] = []
        for capability, checkbox in self._cloud_switches():
            uses_cloud = checkbox.isChecked() and results.get(capability) is None
            if uses_cloud:
                enabled_any = True
                passed.append(CAP_LABELS.get(capability, capability))
            self._set_cloud_backend(capability, updated.id if uses_cloud else LOCAL_BACKEND)

        self._store_profile(updated.with_updates(enabled=enabled_any))
        if enabled_any:
            self._append(f"连通性正常：{updated.label}，已切到云端的是：{'、'.join(passed)}。")
        elif any(box.isChecked() for _, box in self._cloud_switches()):
            self._append("没有能力通过连通性测试，云端未启用。", "error")
        else:
            self._append("已保存档案字段；未勾选任何能力，仍走本地模型。")
        self._refresh_cloud_hint()

    def _cloud_switches(self):
        """能力 → 勾选框（顺序固定，界面与测试都按这个顺序读）。"""
        return ((CAP_JUDGE, self.cloud_enable), (CAP_ASR, self.cloud_asr_enable))

    def _store_profile(self, profile) -> None:
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

    def _set_cloud_backend(self, capability: str, backend: str) -> None:
        prefs = dict(self._config.backend_prefs)
        prefs[capability] = backend
        self._config.backend_prefs = prefs
        self._save_config()

    def _refresh_cloud_hint(self) -> None:
        profile = self._cloud_profile()
        if profile is None:
            self.cloud_hint.setText("")
            return
        used = [
            CAP_LABELS[capability]
            for capability, _ in self._cloud_switches()
            if self._capability_uses_cloud(profile, capability)
        ]
        state = ("已启用：" + "、".join(used)) if used else "未启用（走本地模型）"
        capabilities = [c for c, _ in self._cloud_switches() if profile.supports(c)]
        self.cloud_hint.setText(f"{state}。{describe_egress(profile, capabilities)}")

    def can_continue(self) -> tuple[bool, str]:
        return True, ""