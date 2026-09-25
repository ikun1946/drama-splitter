"""模型表单：转写与语义模型的下载、删除、目录与磁盘空间管理。

由 ``app/gui/dialogs/model_dialog.py`` 的 ModelDialog 承载。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.app_config import LOCAL_BACKEND, AppConfig, default_models_root
from ...core.cloud import build_asr_transport, build_judge_transport
from ...core.model_manager import (
    MODEL_CATALOG,
    delete_model,
    free_space_bytes,
    installed_state,
    spec_by_key,
)
from ...core.providers import (
    CAP_ASR,
    CAP_JUDGE,
    CAP_LABELS,
    ENV_KEY_TEMPLATE,
    ProviderError,
    describe_egress,
)
from ..state import ProjectState
from ..widgets import (
    Card,
    LogPanel,
    NoWheelComboBox,
)
from ..widgets import message
from ..workers import (
    ModelDownloadWorker,
)
from ._common import (
    register_worker,
)


__all__ = ["ModelForm"]


class ModelForm(QWidget):
    """模型选择与下载。"""

    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 同 SettingsPage：本页装在抽屉里，表格四列需要足够的横向空间。
        self.setMinimumWidth(640)
        self._worker: "ModelDownloadWorker | None" = None
        self._config = AppConfig.load()

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["模型", "体积", "状态", "用途"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)

        self.quant_combo = NoWheelComboBox()
        self.btn_discover = QPushButton("🔍  获取可用量化档位")
        self.btn_discover.clicked.connect(self._discover_quants)

        self.dir_edit = QLineEdit(str(self._config.resolved_models_dir()))
        self.btn_pick_dir = QPushButton("更改目录…")
        self.btn_pick_dir.clicked.connect(self._pick_dir)
        self.free_label = QLabel("")
        self.free_label.setProperty("role", "muted")

        # 目录卡片
        dir_card = Card("模型目录", parent=self)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(self.btn_pick_dir)
        old = dir_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        new_layout = QVBoxLayout()
        new_layout.setContentsMargins(12, 10, 12, 10)
        new_layout.setSpacing(8)
        new_layout.addLayout(dir_row)
        new_layout.addWidget(self.free_label)
        dir_card.body.setLayout(new_layout)
        dir_card.body_layout = new_layout

        # 模型表卡片
        table_card = Card("可用模型", parent=self)
        table_card.body_layout.addWidget(self.table, 1)

        # 量化档位卡片
        quant_card = Card("语义模型量化档位", parent=self)
        quant_row = QHBoxLayout()
        quant_row.addWidget(self.quant_combo, 1)
        quant_row.addWidget(self.btn_discover)
        old = quant_card.body.layout()
        if old is not None:
            placeholder = QWidget()
            placeholder.setLayout(old)
            placeholder.deleteLater()
        new_layout = QVBoxLayout()
        new_layout.setContentsMargins(12, 10, 12, 10)
        new_layout.setSpacing(8)
        new_layout.addLayout(quant_row)
        quant_card.body.setLayout(new_layout)
        quant_card.body_layout = new_layout

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)

        # ---- 云端模型卡片（厂商 API）----------------------------------
        #
        # 默认全关：档案未启用、未勾选「允许外发素材」时零外发（见 providers.py）。
        # 「启用」不是随手勾的开关——必须点「保存并测试连通性」通过之后才允许，
        # 否则会出现"以为通了、真正分析时才回退本地"。
        cloud_card = Card("云端模型（厂商 API）", parent=self, body_layout=QVBoxLayout())
        self.cloud_combo = NoWheelComboBox()
        self.cloud_combo.currentIndexChanged.connect(self._on_cloud_profile_changed)
        self.cloud_base_url = QLineEdit()
        self.cloud_model = QLineEdit()
        self.cloud_model.setPlaceholderText(
            "推理接入点 ID 或模型名，如 ep-2026xxxx / doubao-seed-1.6"
        )
        self.cloud_key = QLineEdit()
        self.cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_key.setPlaceholderText("明文保存在本地配置文件，可被环境变量覆盖")
        # 外发范围写在勾选框上而不是提示行里：提示行只在配好模型后才说得清"发往哪台主机"，
        # 而"会发什么"必须在这一刻就能读到——勾这个框就是在授权外发。
        self.cloud_allow = QCheckBox(
            "允许外发素材（转写＝整条音轨；语义判断＝候选点前后台词文本，不含画面）"
        )
        self.cloud_asr_enable = QCheckBox("语音转写使用云端（整条音轨分块上传，约 10 分钟/块）")
        self.cloud_enable = QCheckBox("语义判断使用云端（只发候选点前后台词文本）")
        self.btn_cloud_save = QPushButton("保存并测试连通性")
        self.btn_cloud_save.clicked.connect(self._save_cloud)
        self.cloud_hint = QLabel("")
        self.cloud_hint.setWordWrap(True)
        self.cloud_hint.setProperty("role", "muted")
        cloud_note = QLabel(
            "两条能力相互独立，可只用其一；未启用时走本地模型（转写＝whisper，"
            "语义判断＝llama.cpp / Ollama）。勾选后必须先「保存并测试连通性」，"
            "测通的那一项才会真正切到云端。"
        )
        cloud_note.setWordWrap(True)
        cloud_note.setProperty("role", "muted")

        cloud_card.body_layout.addWidget(self.cloud_combo)
        cloud_card.body_layout.addWidget(QLabel("接口地址（base_url）"))
        cloud_card.body_layout.addWidget(self.cloud_base_url)
        cloud_card.body_layout.addWidget(QLabel("模型名（judge_model）"))
        cloud_card.body_layout.addWidget(self.cloud_model)
        cloud_card.body_layout.addWidget(QLabel("API Key"))
        cloud_card.body_layout.addWidget(self.cloud_key)
        cloud_card.body_layout.addWidget(self.cloud_allow)
        cloud_card.body_layout.addWidget(self.cloud_enable)
        cloud_card.body_layout.addWidget(self.cloud_asr_enable)
        cloud_row = QHBoxLayout()
        cloud_row.addWidget(self.btn_cloud_save)
        cloud_row.addStretch(1)
        cloud_card.body_layout.addLayout(cloud_row)
        cloud_card.body_layout.addWidget(self.cloud_hint)
        cloud_card.body_layout.addWidget(cloud_note)

        self.btn_download = QPushButton("⬇  下载选中模型")
        self.btn_download.setProperty("role", "primary")
        self.btn_download.style().unpolish(self.btn_download)
        self.btn_download.style().polish(self.btn_download)
        self.btn_delete = QPushButton("删除选中模型")
        self.btn_delete.setProperty("role", "danger")
        self.btn_delete.style().unpolish(self.btn_delete)
        self.btn_delete.style().polish(self.btn_delete)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_download.clicked.connect(self._start_download)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_download)

        # 日志
        self.log = LogPanel()
        log_card = Card("日志", parent=self)
        log_card.body_layout.addWidget(self.log, 1)

        # 顶部说明
        header = QLabel(
            "选择并下载所需的模型。语音转写模型与语义判断模型相互独立，"
            "可只装其一；未安装时对应能力自动跳过并在日志中说明。"
        )
        header.setProperty("role", "muted")
        header.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(header)
        outer.addWidget(dir_card)
        outer.addWidget(table_card, 1)
        outer.addWidget(quant_card)
        outer.addWidget(cloud_card)
        outer.addWidget(self.progress)
        outer.addLayout(btn_row)
        outer.addWidget(log_card, 1)

        self._refresh()
        self._load_cloud_fields()

    # ---- 目录 ----------------------------------------------------------

    def _models_root(self):
        return Path(self.dir_edit.text().strip() or str(default_models_root()))

    def _refresh(self) -> None:
        self.table.setRowCount(0)
        for spec in MODEL_CATALOG:
            state = installed_state(spec, self._models_root())
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(spec.label))
            size = f"{spec.approx_mb} MB" if spec.approx_mb else "未知（下载时探测）"
            self.table.setItem(row, 1, QTableWidgetItem(size))
            item = QTableWidgetItem(state.describe())
            if not state.ready:
                item.setForeground(QColor("#9a6700"))
            self.table.setItem(row, 2, item)
            self.table.setItem(row, 3, QTableWidgetItem(spec.note))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, spec.key)
        self.table.resizeColumnsToContents()

        free = free_space_bytes(self._models_root())
        if free is None:
            self.free_label.setText("磁盘可用空间：未知")
        else:
            self.free_label.setText(
                f"磁盘可用空间：{free / 1024 / 1024 / 1024:.1f} GB"
                "（下载前会再检查一次，空间不足会明确拒绝）"
            )

        installed = [s.key for s in MODEL_CATALOG if installed_state(s, self._models_root()).ready]
        if self._config.last_download_key in installed:
            self.quant_combo.setCurrentText(self._config.last_download_key)

    def _selected_spec(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return spec_by_key(key)

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择模型目录", str(self._models_root())
        )
        if not chosen:
            return
        self.dir_edit.setText(chosen)
        self._config.models_dir = chosen
        try:
            self._config.save()
            self._append(f"模型目录已改为 {chosen}（已记住，重启后仍生效）")
        except OSError as exc:
            self._append(f"目录已切换，但保存配置失败（重启后需重新选择）：{exc}")
        self._refresh()

    def _discover_quants(self) -> None:
        from app.core.model_manager import LLM_SIZES, list_remote_gguf

        size = "2B"
        if self.quant_combo.count():
            current = self.quant_combo.currentText()
            for candidate, _ in LLM_SIZES:
                if candidate in current:
                    size = candidate
                    break
        repo = f"unsloth/Qwen3.5-{size}-GGUF"
        self._append(f"查询 {repo} 可用量化档位…")
        names = list_remote_gguf(repo)
        if not names:
            self._append("查询失败或无结果（网络/镜像不可达）。可先下载默认的 Q4_K_M。", "warn")
            return
        quants = []
        for name in names:
            stem = name.rsplit(".", 1)[0]
            parts = stem.split("-")
            if len(parts) >= 3:
                quants.append(parts[-1])
        self.quant_combo.clear()
        self.quant_combo.addItems(sorted(set(quants)))
        self._append(f"发现 {len(quants)} 个档位：{'、'.join(sorted(set(quants)))}")

    def _spec_from_quant(self) -> list:
        from app.core.model_manager import LLM_SIZES, llm_spec

        quant = self.quant_combo.currentText().strip() or "Q4_K_M"
        specs = []
        for size, _ in LLM_SIZES:
            if size in quant:
                clean = quant.split(f"{size}-")[-1]
                specs.append(llm_spec(size, clean))
                return specs
        return [llm_spec("2B", quant)]

    def _start_download(self) -> None:
        specs = []
        spec = self._selected_spec()
        if spec is not None:
            specs.append(spec)
        else:
            specs.extend(self._spec_from_quant())
        if not specs:
            message.information(self, "未选择", "请先在表格里选中要下载的模型。")
            return

        self.btn_download.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self._worker = ModelDownloadWorker(
            specs[0], self._models_root(), endpoint=self._config.endpoint, parent=self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append)
        self._worker.finished_with.connect(self._on_finished)
        register_worker(self, "download", self._worker)
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str, index: int, count: int) -> None:
        if total > 0:
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
        self.progress.setValue(100 if result.ok else 0)
        self.btn_download.setEnabled(True)
        self.btn_delete.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if result.ok:
            self._config.last_download_key = result.spec.key
            try:
                self._config.save()
            except OSError:
                pass
            self._refresh()
            message.information(
                self, "下载完成", f"{result.describe()}\n\n即可直接使用，无需重启。"
            )
        elif result.cancelled:
            self._append("已取消，临时文件已清理。", "warn")
        else:
            message.warning(self, "下载未完成", result.describe())

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self.btn_cancel.setEnabled(False)

    def _delete_selected(self) -> None:
        spec = self._selected_spec()
        if spec is None:
            message.information(self, "未选择", "请先选中要删除的模型。")
            return
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
        self._append(note)
        self._refresh()

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
            return
        self.cloud_base_url.setText(profile.base_url)
        self.cloud_model.setText(profile.judge_model)
        self.cloud_key.setText(profile.api_key)
        self.cloud_allow.setChecked(profile.allow_upload)
        self.cloud_enable.setChecked(self._capability_uses_cloud(profile, CAP_JUDGE))
        self.cloud_asr_enable.setChecked(self._capability_uses_cloud(profile, CAP_ASR))
        self._refresh_cloud_hint()

    def _capability_uses_cloud(self, profile, capability: str) -> bool:
        """该能力此刻是否真的走这家云端（选了 + 档案已测通）。"""
        return profile.enabled and self._config.backend_for(capability) == profile.id

    def _probe_cloud(self, profile, capability: str) -> str | None:
        """连通性测试的接缝：返回错误描述；可用时返回 None。

        单独一个方法是为了让测试能替换掉它——界面测试不允许真联网。
        """
        if not profile.allow_upload:
            return "未勾选「允许外发素材」"
        if not profile.resolved_api_key():
            env_name = ENV_KEY_TEMPLATE.format(id=profile.id.upper())
            return f"没有 API Key（可填在本页，或设环境变量 {env_name}）"
        if capability == CAP_JUDGE and not profile.judge_model:
            return "未填写模型名（judge_model）"
        if capability == CAP_ASR and not profile.supports(CAP_ASR):
            return "未配置转写接口（asr_endpoint / asr_resource_id）"
        transport = (
            build_judge_transport(profile)
            if capability == CAP_JUDGE
            else build_asr_transport(profile)
        )
        if transport is None:
            return f"暂不支持该厂商的{CAP_LABELS.get(capability, capability)}：{profile.id}"
        try:
            transport.probe()
        except ProviderError as exc:
            return exc.describe()
        except Exception as exc:  # noqa: BLE001 - 任何异常都算"不通"
            return str(exc)
        return None

    def _save_cloud(self) -> None:
        """保存字段 → 逐能力测连通 → 只把测通的能力切到云端。

        失败一律**不允许启用**：否则用户会以为通了，直到真正分析时才回退本地。
        """
        profile = self._cloud_profile()
        if profile is None:
            self._append("没有可用的厂商档案。", "error")
            return
        updated = profile.with_updates(
            base_url=self.cloud_base_url.text().strip(),
            judge_model=self.cloud_model.text().strip(),
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

    def _save_config(self) -> None:
        try:
            self._config.save()
        except OSError as exc:
            self._append(f"配置写入失败：{exc}", "error")

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
