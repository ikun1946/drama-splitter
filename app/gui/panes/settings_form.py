"""设置：分集参数（模式 / 集数 / 时长 / 浮动 / 策略）与可行性实时反馈。

两种模式下"用户输入哪个、哪个是算出来的"是相反的：

- 按目标时长：时长可编辑，**集数只读并实时反算**（N = round(T/D)，夹到可行区间）
- 按目标集数：集数可编辑，**时长只读并实时反算**（D = T/N）

派生值只作展示与确认，不反向影响内核：``SplitSettings.derive()`` 在该模式下
本来就用另一个字段（模式B 用 ``Fraction(T, N)``，模式A 用用户输入的目标时长）。
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.settings import (
    CountPolicy,
    RangeMode,
    SplitMode,
    Strategy,
    check_feasibility,
)
from ...core.timebase import format_seconds_brief, format_timecode, round_half_up
from ..state import ProjectState
from ..widgets import (
    Card,
    LabeledField,
    NoWheelComboBox,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
    StatusPill,
)


__all__ = ["SettingsForm"]


class SettingsForm(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 反算写回派生控件时会再次触发 _refresh，用它挡住递归
        self._syncing = False
        # 上一次刷新时「允许集数区间」是否可见：只在"刚切进弹性集数"那一次
        # 做自洽兜底，之后用户怎么改都不再插手
        self._was_flexible = False
        # 本表单由设置对话框承载（对话框正文套滚动容器）。显式给出能容纳双栏的
        # 最小宽度，窄窗口下横向滚动兜底，而不是把两栏压成"一列 90px"。
        self.setMinimumWidth(640)

        # ---- 模式 ----
        self.radio_by_duration = QRadioButton("按目标时长（系统求集数）")
        self.radio_by_count = QRadioButton("按目标集数")
        self.radio_recommend = QRadioButton("推荐方案（先按时长估算，分析后再给剧情建议）")
        self.radio_by_duration.setChecked(True)
        for w in (self.radio_by_duration, self.radio_by_count, self.radio_recommend):
            w.toggled.connect(self._refresh)
        mode_card = Card("分集模式", parent=self)
        mode_card.body_layout.addWidget(self.radio_by_duration)
        mode_card.body_layout.addWidget(self.radio_by_count)
        mode_card.body_layout.addWidget(self.radio_recommend)
        self._mode_card = mode_card

        # ---- 目标数值 ----
        self.duration_spin = NoWheelDoubleSpinBox()
        self.duration_spin.setRange(1.0, 3600.0)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(120.0)
        self.duration_spin.setSuffix(" 秒")
        self.duration_spin.valueChanged.connect(self._refresh)

        self.count_spin = NoWheelSpinBox()
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(60)
        self.count_spin.valueChanged.connect(self._refresh)

        self.policy_exact = QRadioButton("严格集数：必须恰好 N 集，无解就明确报错")
        self.policy_flexible = QRadioButton("弹性集数：在允许区间内比较方案")
        self.policy_exact.setChecked(True)
        self.allowed_min = NoWheelSpinBox()
        self.allowed_min.setRange(1, 9999)
        self.allowed_min.setValue(58)
        self.allowed_max = NoWheelSpinBox()
        self.allowed_max.setRange(1, 9999)
        self.allowed_max.setValue(62)
        for w in (self.policy_exact, self.policy_flexible):
            w.toggled.connect(self._refresh)
        for w in (self.allowed_min, self.allowed_max):
            w.valueChanged.connect(self._refresh)

        # 允许集数区间只在「按目标集数 + 弹性集数」下有意义，其余情况整行隐藏
        # （原先是一直显示但灰掉，默认值 58–62 对多数素材都是无意义且自相矛盾的）
        self._flexible_row = QWidget(self)
        flexible_row = QHBoxLayout(self._flexible_row)
        flexible_row.setContentsMargins(0, 0, 0, 0)
        flexible_row.addWidget(QLabel("允许集数区间："))
        flexible_row.addWidget(self.allowed_min)
        flexible_row.addWidget(QLabel("–"))
        flexible_row.addWidget(self.allowed_max)
        flexible_row.addStretch(1)
        # 默认隐藏：只有「按目标集数 + 弹性集数」下它才有定义（见 _refresh）
        self._flexible_row.setVisible(False)

        count_card = Card("目标集数 / 时长", parent=self)
        # 同一个框在两种模式下含义不同：手里的输入 vs 系统算出来的值
        dfield = LabeledField("目标时长", suffix="秒")
        dfield.attach(self.duration_spin)
        count_card.body_layout.addWidget(dfield)
        nfield = LabeledField("预计集数（自动）")
        nfield.attach(self.count_spin)
        count_card.body_layout.addWidget(nfield)
        self._duration_field = dfield
        self._count_field = nfield
        count_card.body_layout.addSpacing(4)
        count_card.body_layout.addWidget(self.policy_exact)
        count_card.body_layout.addWidget(self.policy_flexible)
        count_card.body_layout.addWidget(self._flexible_row)
        self._count_card = count_card

        # ---- 范围来源 ----
        self.range_manual = QRadioButton("手动上下限")
        self.range_percent = QRadioButton("按目标时长百分比浮动")
        self.range_percent.setChecked(True)
        self.min_spin = NoWheelDoubleSpinBox()
        self.min_spin.setRange(0.1, 3600.0)
        self.min_spin.setDecimals(3)
        self.min_spin.setValue(96.0)
        self.max_spin = NoWheelDoubleSpinBox()
        self.max_spin.setRange(0.1, 3600.0)
        self.max_spin.setDecimals(3)
        self.max_spin.setValue(144.0)
        self.tolerance_spin = NoWheelDoubleSpinBox()
        self.tolerance_spin.setRange(0.0, 99.0)
        self.tolerance_spin.setDecimals(2)
        self.tolerance_spin.setValue(20.0)
        self.tolerance_spin.setSuffix(" %")
        for w in (self.range_manual, self.range_percent):
            w.toggled.connect(self._refresh)
        for w in (self.min_spin, self.max_spin, self.tolerance_spin):
            w.valueChanged.connect(self._refresh)

        range_card = Card("集长范围（两种来源互斥）", parent=self)
        range_form = QFormLayout()
        range_form.addRow(self.range_percent)
        pfield = LabeledField("浮动比例 p", suffix="%")
        pfield.attach(self.tolerance_spin)
        range_form.addRow(pfield)
        range_form.addRow(self.range_manual)
        lfield = LabeledField("最短集长 L", suffix="秒")
        lfield.attach(self.min_spin)
        range_form.addRow(lfield)
        ufield = LabeledField("最长集长 U", suffix="秒")
        ufield.attach(self.max_spin)
        range_form.addRow(ufield)
        # 把 QFormLayout 嫁接到 range_card.body
        old_layout = range_card.body.layout()
        if old_layout is not None:
            placeholder = QWidget()
            placeholder.setLayout(old_layout)
            placeholder.deleteLater()
        range_card.body.setLayout(range_form)
        range_card.body_layout = range_form
        range_form.setContentsMargins(12, 10, 12, 10)
        range_form.setSpacing(8)
        self._range_card = range_card

        # ---- 策略 ----
        self.strategy_combo = NoWheelComboBox()
        self.strategy_combo.addItem("剧情优先", Strategy.STORY.value)
        self.strategy_combo.addItem("悬念优先", Strategy.SUSPENSE.value)
        self.strategy_combo.addItem("时长优先", Strategy.DURATION.value)
        self.allow_exception = QCheckBox("允许单集例外（默认关闭，启用后须逐集记录原因）")

        strategy_card = Card("策略与例外", parent=self)
        sfield = LabeledField("策略")
        sfield.attach(self.strategy_combo)
        strategy_card.body_layout.addWidget(sfield)
        strategy_card.body_layout.addWidget(self.allow_exception)
        self._strategy_card = strategy_card

        # ---- 可行性 ----
        self._feas_pill = StatusPill("可行", tone="success")
        feasibility_card = Card("即时可行性检查（调用 AI 之前就应给出结论）", parent=self)
        pill_row = QHBoxLayout()
        pill_row.addWidget(self._feas_pill)
        pill_row.addStretch(1)
        feasibility_card.body_layout.insertLayout(0, pill_row)
        self.feasibility = QTextEdit()
        self.feasibility.setReadOnly(True)
        feasibility_card.body_layout.addWidget(self.feasibility, 1)
        self._feasibility_card = feasibility_card

        # ---- 装配 ----
        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        left_col.addWidget(mode_card)
        left_col.addWidget(count_card)
        left_col.addWidget(range_card)
        left_col.addWidget(strategy_card)
        left_col.addStretch(1)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(feasibility_card, 1)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addLayout(left_col, 1)
        outer.addLayout(right_col, 1)

    def collect_settings(self):
        settings = self.state.settings

        if self.radio_by_duration.isChecked():
            settings.split_mode = SplitMode.TARGET_DURATION
        elif self.radio_by_count.isChecked():
            settings.split_mode = SplitMode.TARGET_EPISODE_COUNT
        else:
            settings.split_mode = SplitMode.RECOMMEND

        settings.target_duration_seconds = Fraction(str(self.duration_spin.value()))
        settings.target_episode_count = self.count_spin.value()
        settings.count_policy = (
            CountPolicy.EXACT if self.policy_exact.isChecked() else CountPolicy.FLEXIBLE
        )
        settings.allowed_count_min = self.allowed_min.value()
        settings.allowed_count_max = self.allowed_max.value()

        spec = settings.range
        if self.range_manual.isChecked():
            spec.switch_to(RangeMode.MANUAL)
            spec.manual_min = Fraction(str(self.min_spin.value()))
            spec.manual_max = Fraction(str(self.max_spin.value()))
        else:
            spec.switch_to(RangeMode.PERCENT)
            spec.tolerance = Fraction(str(self.tolerance_spin.value())) / 100

        settings.strategy = Strategy(self.strategy_combo.currentData())
        settings.allow_episode_exceptions = self.allow_exception.isChecked()
        return settings

    def refresh_from_state(self) -> None:
        self._refresh()

    def apply_settings(self, settings) -> None:
        """把一份 SplitSettings 回填到控件（打开项目 / 恢复会话用）。

        控件是参数的唯一录入面（collect_settings 从控件收集），因此恢复
        也必须走控件：直接改 state.settings 会被下一次 _refresh 用控件值
        覆盖回去。回填后立即刷新一次可行性提示。
        """
        mode = settings.split_mode
        self.radio_by_duration.setChecked(mode == SplitMode.TARGET_DURATION)
        self.radio_by_count.setChecked(mode == SplitMode.TARGET_EPISODE_COUNT)
        self.radio_recommend.setChecked(mode == SplitMode.RECOMMEND)
        # 各字段都可能为 None（取决于模式），None 就保留控件现值
        if settings.target_duration_seconds is not None:
            self.duration_spin.setValue(float(settings.target_duration_seconds))
        if settings.target_episode_count is not None:
            self.count_spin.setValue(settings.target_episode_count)
        exact = settings.count_policy == CountPolicy.EXACT
        self.policy_exact.setChecked(exact)
        self.policy_flexible.setChecked(not exact)
        if settings.allowed_count_min is not None:
            self.allowed_min.setValue(settings.allowed_count_min)
        if settings.allowed_count_max is not None:
            self.allowed_max.setValue(settings.allowed_count_max)

        spec = settings.range
        percent = spec.mode == RangeMode.PERCENT
        self.range_percent.setChecked(percent)
        self.range_manual.setChecked(not percent)
        if spec.tolerance is not None:
            self.tolerance_spin.setValue(float(spec.tolerance) * 100)
        if spec.manual_min is not None:
            self.min_spin.setValue(float(spec.manual_min))
        if spec.manual_max is not None:
            self.max_spin.setValue(float(spec.manual_max))

        index = self.strategy_combo.findData(settings.strategy.value)
        if index >= 0:
            self.strategy_combo.setCurrentIndex(index)
        self.allow_exception.setChecked(settings.allow_episode_exceptions)

        self.state.settings = settings
        self._refresh()

    def _refresh(self) -> None:
        if self._syncing:
            # 反算写回派生控件会再次触发刷新，直接跳过，避免递归
            return
        settings = self.collect_settings()

        by_count = settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
        self._sync_editable_fields(by_count)
        self.policy_exact.setEnabled(by_count)
        self.policy_flexible.setEnabled(by_count)
        # 允许集数区间只在「按目标集数 + 弹性集数」下有定义，其余情况整行隐藏
        flexible = by_count and settings.count_policy == CountPolicy.FLEXIBLE
        self._flexible_row.setVisible(flexible)
        if flexible and not self._was_flexible:
            # 刚切进弹性集数：若区间与名义目标自相矛盾（如默认 58–62）先兜底一次，
            # 此后用户怎么改都不再插手
            self._seed_allowed_range(settings)
            settings = self.collect_settings()   # 把兜底后的区间纳入本次校验与判定
        self._was_flexible = flexible
        manual = self.range_manual.isChecked()
        self.min_spin.setEnabled(manual)
        self.max_spin.setEnabled(manual)
        self.tolerance_spin.setEnabled(not manual)

        lines: list[str] = []
        total = self.state.total_seconds
        if total is None:
            self._set_derived_help(by_count, "导入源片后自动计算")
            lines.append("尚未导入源片，无法检查可行性。")
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("就绪")
            self._feas_pill.set_tone("muted")
            return

        problems = settings.validate()
        if problems:
            self._set_derived_help(by_count, "参数不完整，暂不反算")
            lines.append("【参数不完整】")
            lines.extend(f"  · {text}" for text in problems)
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("参数不完整")
            self._feas_pill.set_tone("danger")
            return

        report = check_feasibility(settings, total)
        self._apply_derived(settings, total, report, by_count)

        lines.append(f"源片总时长：{format_timecode(total)}")
        if report.derived is not None:
            lines.append(
                f"目标时长 D：{format_timecode(report.derived.target_duration)}"
                "（内部全精度，界面显示才取整）"
            )
            lines.append(
                f"集长范围：{format_seconds_brief(report.derived.min_duration)}"
                f" – {format_seconds_brief(report.derived.max_duration)}"
            )
        if report.min_episodes > report.max_episodes:
            lines.append("可行集数范围：无（不存在任何集数能满足当前集长范围）")
        else:
            lines.append(
                f"可行集数范围：{report.min_episodes} – {report.max_episodes} 集"
                f"（最少 = ⌈T/U⌉，最多 = ⌊T/L⌋）"
            )
        if (
            settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
            and settings.count_policy == CountPolicy.FLEXIBLE
        ):
            if report.allowed_min > report.allowed_max:
                lines.append("与用户允许区间取交集后：无交集")
            else:
                lines.append(
                    f"与用户允许区间取交集后：{report.allowed_min} – {report.allowed_max} 集"
                )

        if report.issues:
            lines.append("")
            for issue in report.issues:
                tag = "阻断" if issue.level == "block" else ("警告" if issue.level == "warn" else "提示")
                lines.append(f"[{tag}] {issue.message}")
                if issue.hint:
                    lines.append(f"        {issue.hint}")

        lines.append("")
        if report.blocking:
            lines.append("结论：当前参数无解，请先调整参数，不要进入分析。")
            self._feas_pill.set_text("无解")
            self._feas_pill.set_tone("danger")
        else:
            # 可行但可能有警告
            has_warn = any(iss.level == "warn" for iss in report.issues)
            if has_warn:
                lines.append("结论：参数可行但存在警告，可继续。")
                self._feas_pill.set_text("可行（警告）")
                self._feas_pill.set_tone("warn")
            else:
                lines.append("结论：参数可行，可以开始分析。")
                self._feas_pill.set_text("可行")
                self._feas_pill.set_tone("success")

        self.feasibility.setPlainText("\n".join(lines))

    # ---- 自动反算（派生字段只读） --------------------------------------

    def _seed_allowed_range(self, settings) -> None:
        """刚切进弹性集数时的一次性兜底：区间不含名义目标就收回 N-1 – N+1。

        58–62 这类与名义目标冲突的默认值（或项目里残留的旧值）会让校验直接报
        「名义目标 N 不在允许区间内」，一选弹性就卡住。**只在切进来的这一次**
        做，且只在区间确实不含名义目标时做——用户随后的任何修改都不再被覆盖。
        """
        nominal = settings.target_episode_count or 0
        if nominal <= 0:
            return
        low = settings.allowed_count_min or 0
        high = settings.allowed_count_max or 0
        if low <= nominal <= high:
            return
        self._set_value(self.allowed_min, max(1, nominal - 1))
        self._set_value(self.allowed_max, nominal + 1)

    def _sync_editable_fields(self, by_count: bool) -> None:
        """源字段可编辑、派生字段只读；标题随模式说明哪个是算出来的。

        只读字段去掉步进箭头（``NoButtons``）：既避免误触，也不给它"可以改"
        的暗示；控件保持 enabled 以便正常读出数值。
        """
        self.duration_spin.setReadOnly(by_count)
        self.count_spin.setReadOnly(not by_count)
        self.duration_spin.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
            if by_count
            else QAbstractSpinBox.ButtonSymbols.UpDownArrows
        )
        self.count_spin.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.UpDownArrows
            if by_count
            else QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        self._duration_field.set_label("预计目标时长（自动）" if by_count else "目标时长")
        self._count_field.set_label("目标集数 N" if by_count else "预计集数（自动）")

        # 推荐方案：分析时由系统把三种策略各解一遍再取代价最低者，此时手选
        # 「策略」既不会决定最终取向、又让人以为"我选的生效了"，索性置灰说清。
        recommend = self.radio_recommend.isChecked()
        self.strategy_combo.setEnabled(not recommend)
        self._strategy_field.set_help(
            "推荐方案会自动比较三种策略，取总代价最低者" if recommend else ""
        )

    def _set_derived_help(self, by_count: bool, text: str) -> None:
        field = self._duration_field if by_count else self._count_field
        field.set_help(text)

    def _apply_derived(self, settings, total, report, by_count: bool) -> None:
        """把派生目标值写回只读控件：模式A 反算集数，模式B 反算时长。"""
        if by_count:
            count = settings.target_episode_count or 0
            if count <= 0:
                self._duration_field.set_help("目标集数未设置，无法反算时长")
                return
            self._set_value(self.duration_spin, float(Fraction(total, count)))
            self._duration_field.set_help(
                "由总时长 ÷ 目标集数自动计算（内部全精度，界面显示才取整）"
            )
            return

        target = settings.target_duration_seconds
        if target is None or target <= 0:
            self._count_field.set_help("目标时长未设置，无法反算集数")
            return
        if report.min_episodes > report.max_episodes:
            # 无解时不猜值：保留上一次的数值，理由交给可行性面板说清楚
            self._count_field.set_help("当前参数无解，无法反算集数")
            return

        nominal = round_half_up(Fraction(total) / target)
        clamped = max(report.min_episodes, min(nominal, report.max_episodes))
        self._set_value(self.count_spin, clamped)
        if report.min_episodes == report.max_episodes:
            self._count_field.set_help(
                f"由总时长 ÷ 目标时长自动计算（只读）：{report.min_episodes} 集"
            )
        else:
            self._count_field.set_help(
                "由总时长 ÷ 目标时长自动计算（只读）；求解器可在 "
                f"{report.min_episodes}–{report.max_episodes} 集间择优"
            )

    def _set_value(self, spin, value) -> None:
        """写派生控件：置 _syncing 挡递归，临时断信号防多余的刷新。"""
        previous = self._syncing
        self._syncing = True
        try:
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        finally:
            self._syncing = previous

    def can_continue(self) -> tuple[bool, str]:
        settings = self.collect_settings()
        problems = settings.validate()
        if problems:
            return False, problems[0]
        if self.state.total_seconds is None:
            return False, "尚未导入源片。"
        report = check_feasibility(settings, self.state.total_seconds)
        if report.blocking:
            first = report.issues[0]
            return False, f"{first.message} {first.hint}".strip()
        return True, ""
