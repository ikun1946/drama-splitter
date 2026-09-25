"""页 2 设置：分集参数（模式 / 集数 / 时长 / 浮动 / 策略）与可行性实时反馈。"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
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
from ...core.timebase import format_seconds_brief, format_timecode
from ..state import ProjectState
from ..widgets import (
    Card,
    LabeledField,
    StatusPill,
)


__all__ = ["SettingsForm"]


class SettingsForm(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 本页会被装进 420px 宽的抽屉里。默认尺寸策略下，双栏布局的最小
        # 宽度之和会把版面挤成"一列 90px"，文字被迫竖排并溢出卡片——
        # 因此显式给出能容纳双栏的最小宽度，并允许横向滚动兜底。
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
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(1.0, 3600.0)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(120.0)
        self.duration_spin.setSuffix(" 秒")
        self.duration_spin.valueChanged.connect(self._refresh)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(60)
        self.count_spin.valueChanged.connect(self._refresh)

        self.policy_exact = QRadioButton("严格集数：必须恰好 N 集，无解就明确报错")
        self.policy_flexible = QRadioButton("弹性集数：在允许区间内比较方案")
        self.policy_exact.setChecked(True)
        self.allowed_min = QSpinBox()
        self.allowed_min.setRange(1, 9999)
        self.allowed_min.setValue(58)
        self.allowed_max = QSpinBox()
        self.allowed_max.setRange(1, 9999)
        self.allowed_max.setValue(62)
        for w in (self.policy_exact, self.policy_flexible):
            w.toggled.connect(self._refresh)
        for w in (self.allowed_min, self.allowed_max):
            w.valueChanged.connect(self._refresh)

        flexible_row = QHBoxLayout()
        flexible_row.addWidget(QLabel("允许集数区间："))
        flexible_row.addWidget(self.allowed_min)
        flexible_row.addWidget(QLabel("–"))
        flexible_row.addWidget(self.allowed_max)
        flexible_row.addStretch(1)

        count_card = Card("目标集数 / 时长", parent=self)
        # 用 LabeledField 风格
        dfield = LabeledField("目标时长", suffix="秒")
        dfield.attach(self.duration_spin)
        count_card.body_layout.addWidget(dfield)
        nfield = LabeledField("目标集数 N")
        nfield.attach(self.count_spin)
        count_card.body_layout.addWidget(nfield)
        count_card.body_layout.addSpacing(4)
        count_card.body_layout.addWidget(self.policy_exact)
        count_card.body_layout.addWidget(self.policy_flexible)
        count_card.body_layout.addLayout(flexible_row)
        self._count_card = count_card

        # ---- 范围来源 ----
        self.range_manual = QRadioButton("手动上下限")
        self.range_percent = QRadioButton("按目标时长百分比浮动")
        self.range_percent.setChecked(True)
        self.min_spin = QDoubleSpinBox()
        self.min_spin.setRange(0.1, 3600.0)
        self.min_spin.setDecimals(3)
        self.min_spin.setValue(96.0)
        self.max_spin = QDoubleSpinBox()
        self.max_spin.setRange(0.1, 3600.0)
        self.max_spin.setDecimals(3)
        self.max_spin.setValue(144.0)
        self.tolerance_spin = QDoubleSpinBox()
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
        self.strategy_combo = QComboBox()
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
        settings = self.collect_settings()

        by_count = settings.split_mode == SplitMode.TARGET_EPISODE_COUNT
        self.duration_spin.setEnabled(not by_count)
        self.count_spin.setEnabled(by_count)
        self.policy_exact.setEnabled(by_count)
        self.policy_flexible.setEnabled(by_count)
        self.allowed_min.setEnabled(by_count and settings.count_policy == CountPolicy.FLEXIBLE)
        self.allowed_max.setEnabled(by_count and settings.count_policy == CountPolicy.FLEXIBLE)
        manual = self.range_manual.isChecked()
        self.min_spin.setEnabled(manual)
        self.max_spin.setEnabled(manual)
        self.tolerance_spin.setEnabled(not manual)

        lines: list[str] = []
        total = self.state.total_seconds
        if total is None:
            lines.append("尚未导入源片，无法检查可行性。")
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("就绪")
            self._feas_pill.set_tone("muted")
            return

        problems = settings.validate()
        if problems:
            lines.append("【参数不完整】")
            lines.extend(f"  · {text}" for text in problems)
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("参数不完整")
            self._feas_pill.set_tone("danger")
            return

        report = check_feasibility(settings, total)

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
