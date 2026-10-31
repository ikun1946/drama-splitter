"""设置：三个决策组（分集策略 / 范围与例外 / 校验结果），参数一次说完。

两种模式下"用户输入哪个、哪个是算出来的"是相反的：

- 按目标时长：时长可编辑，**集数只读并实时反算**（N = round(T/D)，夹到可行区间）
- 按目标集数：集数可编辑，**时长只读并实时反算**（D = T/N）

派生值只作展示与确认，不反向影响内核：``SplitSettings.derive()`` 在该模式下
本来就用另一个字段（模式B 用 ``Fraction(T, N)``，模式A 用用户输入的目标时长）。

联动一律用「置灰」，不用「隐藏」
--------------------------------
原先「允许集数区间」在非弹性模式下整行 ``setVisible(False)``：用户切到「严格
集数」后再切回来，会以为这个参数根本不存在（它确实有时不生效，但"不生效"
和"不存在"是两回事，置灰 + 一句原因才说得清）。同一原则用于集长范围的
浮动比例 / 上下限：不可用时置灰，值保留，切回即恢复。
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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

#: 卡片内表单的行距；QFormLayout 的默认行距比卡片标题下的间距大，显式收小。
_FORM_MARGINS = (12, 10, 12, 10)
_FORM_SPACING = 8


def _form_layout() -> QFormLayout:
    """卡片正文用的两列表单。

    ``Card`` 原生支持 ``body_layout`` 参数，直接传一个 QFormLayout 即可——
    旧写法是先建 QVBoxLayout 再"把 body 的 layout 摘下来塞进 QFormLayout"，
    摘下来的那个布局被放进临时 QWidget 再 ``deleteLater()``，纯属绕路。
    """
    form = QFormLayout()
    form.setContentsMargins(*_FORM_MARGINS)
    form.setSpacing(_FORM_SPACING)
    form.setLabelAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    )
    # 窗口不够宽时把标签折到字段上方（而不是让整张卡横向撑开）：
    # 三列并排时每列只有 400px 上下，单选的长标题（"严格集数：必须恰好 N 集…"）
    # 就是最小宽度的大头。折行只在真放不下时发生，宽屏上仍是标准两列。
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return form


def _radio_box(*radios: QRadioButton, parent: QWidget) -> QWidget:
    """把若干单选竖排成一组（表单里占"一行的值"那一格）。"""
    box = QWidget(parent)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for radio in radios:
        layout.addWidget(radio)
    return box


class SettingsForm(QWidget):
    def __init__(self, state: ProjectState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        # 反算写回派生控件时会再次触发 _refresh，用它挡住递归
        self._syncing = False
        # 上一次性刷新时「允许集数区间」是否生效：只在"刚切进弹性集数"那一次
        # 做自洽兜底，之后用户怎么改都不再插手
        self._was_flexible = False
        # 本表单由设置对话框承载（对话框正文套滚动容器）。显式给出能容纳双栏的
        # 最小宽度，窄窗口下横向滚动兜底，而不是把两栏压成"一列 90px"。
        self.setMinimumWidth(640)

        # ---- 决策①：分集方式 --------------------------------------------
        self.radio_by_duration = QRadioButton("按目标时长（系统求集数）")
        self.radio_by_count = QRadioButton("按目标集数")
        self.radio_recommend = QRadioButton("推荐方案（自动比较三种策略）")
        self.radio_recommend.setToolTip(
            "分析时把剧情 / 悬念 / 时长三种策略各解一遍，取总代价最低者；"
            "此时「策略」由系统决定，手选无效"
        )
        self.radio_by_duration.setChecked(True)
        for w in (self.radio_by_duration, self.radio_by_count, self.radio_recommend):
            w.toggled.connect(self._refresh)

        # 目标数值：同一个框在两种模式下含义不同（手里的输入 vs 系统算出来的值）
        self.duration_spin = NoWheelDoubleSpinBox(self)
        self.duration_spin.setRange(1.0, 3600.0)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setValue(120.0)
        self.duration_spin.setSuffix(" 秒")
        self.duration_spin.valueChanged.connect(self._refresh)

        self.count_spin = NoWheelSpinBox(self)
        self.count_spin.setRange(1, 9999)
        self.count_spin.setValue(60)
        self.count_spin.setSuffix(" 集")
        self.count_spin.valueChanged.connect(self._refresh)

        self._duration_field = LabeledField("目标时长", parent=self)
        self._duration_field.attach(self.duration_spin)
        self._count_field = LabeledField("目标集数 N", parent=self)
        self._count_field.attach(self.count_spin)

        # 决策②：严格 / 弹性集数
        self.policy_exact = QRadioButton("严格集数：必须恰好 N 集，无解就明确报错")
        self.policy_flexible = QRadioButton("弹性集数：在允许区间内比较方案")
        self.policy_exact.setChecked(True)
        for w in (self.policy_exact, self.policy_flexible):
            w.toggled.connect(self._refresh)

        # 策略（剧情 / 悬念 / 时长优先）：推荐方案会自己跑三遍，那时置灰
        self.strategy_combo = NoWheelComboBox(self)
        self.strategy_combo.addItem("剧情优先", Strategy.STORY.value)
        self.strategy_combo.addItem("悬念优先", Strategy.SUSPENSE.value)
        self.strategy_combo.addItem("时长优先", Strategy.DURATION.value)
        self.strategy_combo.currentIndexChanged.connect(self._refresh)
        self._strategy_field = LabeledField("策略", parent=self)
        self._strategy_field.attach(self.strategy_combo)

        policy_card = Card(
            "分集策略", parent=self, body_layout=_form_layout(), accent="primary"
        )
        form = policy_card.body_layout
        form.addRow(
            "分集方式",
            _radio_box(
                self.radio_by_duration,
                self.radio_by_count,
                self.radio_recommend,
                parent=policy_card,
            ),
        )
        # 两个目标值各占一行：竖排才不会把 "严格/弹性" 那一行的宽度撑爆，
        # 也让"填哪个、另一个由系统算"在置灰状态下一眼可辨。
        form.addRow(self._duration_field)
        form.addRow(self._count_field)
        form.addRow(
            "集数策略",
            _radio_box(self.policy_exact, self.policy_flexible, parent=policy_card),
        )
        form.addRow(self._strategy_field)
        self._policy_card = policy_card

        # ---- 决策③：集长范围来源 ----------------------------------------
        self.range_manual = QRadioButton("手动上下限")
        self.range_percent = QRadioButton("按目标时长百分比浮动")
        self.range_percent.setToolTip(
            "集长上下限 = 目标时长 × (1 ± 浮动比例)，随目标时长一起走"
        )
        self.range_percent.setChecked(True)

        self.tolerance_spin = NoWheelDoubleSpinBox(self)
        self.tolerance_spin.setRange(0.0, 99.0)
        self.tolerance_spin.setDecimals(2)
        self.tolerance_spin.setValue(20.0)
        self.tolerance_spin.setSuffix(" %")

        self.min_spin = NoWheelDoubleSpinBox(self)
        self.min_spin.setRange(0.1, 3600.0)
        self.min_spin.setDecimals(3)
        self.min_spin.setValue(96.0)
        self.min_spin.setSuffix(" 秒")

        self.max_spin = NoWheelDoubleSpinBox(self)
        self.max_spin.setRange(0.1, 3600.0)
        self.max_spin.setDecimals(3)
        self.max_spin.setValue(144.0)
        self.max_spin.setSuffix(" 秒")

        for w in (self.range_manual, self.range_percent):
            w.toggled.connect(self._refresh)
        for w in (self.min_spin, self.max_spin, self.tolerance_spin):
            w.valueChanged.connect(self._refresh)

        # 决策④：允许集数区间（始终可见，不可用时置灰 + 一句原因）
        self.allowed_min = NoWheelSpinBox(self)
        self.allowed_min.setRange(1, 9999)
        self.allowed_min.setValue(58)
        self.allowed_max = NoWheelSpinBox(self)
        self.allowed_max.setRange(1, 9999)
        self.allowed_max.setValue(62)
        for w in (self.allowed_min, self.allowed_max):
            w.valueChanged.connect(self._refresh)

        allowed_row = QWidget(self)
        allowed_layout = QHBoxLayout(allowed_row)
        allowed_layout.setContentsMargins(0, 0, 0, 0)
        allowed_layout.setSpacing(6)
        allowed_layout.addWidget(self.allowed_min)
        dash = QLabel("–", allowed_row)
        allowed_layout.addWidget(dash)
        allowed_layout.addWidget(self.allowed_max)
        allowed_layout.addWidget(QLabel("集", allowed_row))
        allowed_layout.addStretch(1)
        self._allowed_row = allowed_row
        # 旧名保留：外部按这名字找"允许集数区间那一行"
        self._flexible_row = allowed_row

        self._allowed_hint = QLabel("", self)
        self._allowed_hint.setProperty("role", "muted")
        self._allowed_hint.setWordWrap(True)

        self.allow_exception = QCheckBox("允许单集例外（启用后须逐集记录原因）", self)

        range_card = Card("范围与例外", parent=self, body_layout=_form_layout())
        range_form = range_card.body_layout
        range_form.addRow(
            "集长范围",
            _radio_box(self.range_percent, self.range_manual, parent=range_card),
        )
        range_form.addRow("浮动比例 p", self.tolerance_spin)
        range_form.addRow("最短集长 L", self.min_spin)
        range_form.addRow("最长集长 U", self.max_spin)
        range_form.addRow("允许集数区间", allowed_row)
        range_form.addRow(self._allowed_hint)
        range_form.addRow(self.allow_exception)
        self._range_card = range_card

        # ---- 校验结果（原右栏可行性卡） ----------------------------------
        self._feas_pill = StatusPill("可行", tone="success")
        feasibility_card = Card("校验结果", parent=self, accent="primary")
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.setSpacing(8)
        pill_row.addWidget(self._feas_pill)
        pill_row.addStretch(1)
        feasibility_card.body_layout.insertLayout(0, pill_row)
        self.feasibility = QTextEdit(feasibility_card)
        self.feasibility.setReadOnly(True)
        # 结论正文自己滚动：卡片高度不能被它那串"可行集数范围 / 与用户区间取交集"
        # 撑开——那是这一屏第三长的文本，而它上面已经有两张参数卡了。
        self.feasibility.setMinimumHeight(150)
        feasibility_card.body_layout.addWidget(self.feasibility, 1)
        # 结论卡的色条跟着判定结果走：这是这一屏最该被一眼看到的东西
        self._feas_card = feasibility_card
        self._feasibility_card = feasibility_card

        # ---- 装配 -------------------------------------------------------
        # 三张卡横向并排：卡片高度 = 最高那张（约 400px），整屏一次看完。
        # 竖排会到 850px、两列叠放也要 620px，720 屏上都得滚动才能看到结论。
        # 宽屏对话框里三列更省纵向空间，也正对应"三个决策组"。
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        outer.addWidget(policy_card, 4)
        outer.addWidget(range_card, 4)
        outer.addWidget(feasibility_card, 3)

    def collect_settings(self):
        settings = self.state.settings

        if self.radio_by_duration.isChecked():
            settings.split_mode = SplitMode.TARGET_DURATION
        elif self.radio_by_count.isChecked():
            settings.split_mode = SplitMode.TARGET_EPISODE_COUNT
        else:
            settings.split_mode = SplitMode.RECOMMEND

        # 目标时长 / 集数照常写回：推荐模式下它们是"用户上一次手填的值"，
        # 留着才能在切回「按目标时长」时恢复。自动判定不读它们
        # （check_feasibility 对模式C 无条件走 auto_pending）。
        settings.target_duration_seconds = Fraction(str(self.duration_spin.value()))
        settings.target_episode_count = self.count_spin.value()
        settings.count_policy = (
            CountPolicy.EXACT if self.policy_exact.isChecked() else CountPolicy.FLEXIBLE
        )
        # 区间值即使在不生效的模式下也照写：置灰不等于丢值，切回弹性即沿用
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

        # 允许集数区间只在「按目标集数 + 弹性集数」下参与计算：置灰 + 说明，
        # 不隐藏。它一直有定义（值始终写回配置），只是此时不生效。
        flexible = by_count and settings.count_policy == CountPolicy.FLEXIBLE
        for widget in (self.allowed_min, self.allowed_max):
            widget.setEnabled(flexible)
        self._sync_allowed_hint(settings, flexible)
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
            self._set_feas_accent("primary")
            return

        problems = settings.validate()
        if problems:
            self._set_derived_help(by_count, "参数不完整，暂不反算")
            lines.append("【参数不完整】")
            lines.extend(f"  · {text}" for text in problems)
            self.feasibility.setPlainText("\n".join(lines))
            self._feas_pill.set_text("参数不完整")
            self._feas_pill.set_tone("danger")
            self._set_feas_accent("danger")
            return

        report = check_feasibility(settings, total)
        self._apply_derived(settings, total, report, by_count)

        lines.append(f"源片总时长：{format_timecode(total)}")
        if report.auto_pending:
            # 模式C 还没有派生参数，也就没有可信的可行集数区间——要等自动判定
            # 定下集数后才有 L/U。这里绝不能打印默认的 "0 – 0 集"。
            lines.append(
                "每集时长与集数：分析后自动判定（依据素材自身的内容节奏）"
            )
        elif report.derived is not None:
            lines.append(
                f"目标时长 D：{format_timecode(report.derived.target_duration)}"
                "（内部全精度，界面显示才取整）"
            )
            lines.append(
                f"集长范围：{format_seconds_brief(report.derived.min_duration)}"
                f" – {format_seconds_brief(report.derived.max_duration)}"
            )
        if report.auto_pending:
            pass                       # 集数区间要等判定，不在这里编
        elif report.min_episodes > report.max_episodes:
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
            self._set_feas_accent("danger")
        else:
            # 可行但可能有警告
            has_warn = any(iss.level == "warn" for iss in report.issues)
            if has_warn:
                lines.append("结论：参数可行但存在警告，可继续。")
                self._feas_pill.set_text("可行（警告）")
                self._feas_pill.set_tone("warn")
                self._set_feas_accent("warn")
            else:
                lines.append("结论：参数可行，可以开始分析。")
                self._feas_pill.set_text("可行")
                self._feas_pill.set_tone("success")
                self._set_feas_accent("success")

        self.feasibility.setPlainText("\n".join(lines))

    # ---- 联动（置灰 + 原因） --------------------------------------------

    def _sync_allowed_hint(self, settings, flexible: bool) -> None:
        """「允许集数区间」当前为什么不可用：一句话写在它下面，不留白。"""
        if settings.split_mode == SplitMode.RECOMMEND:
            text = "推荐方案由系统判定集数，此区间暂不参与计算（值会保留）。"
        elif not flexible:
            text = (
                "当前为严格集数，此区间暂不参与计算；"
                "切到「弹性集数」后生效（值会保留）。"
            )
        else:
            text = "求解器只在这些集数里择优；切换模式时值不会被清掉。"
        if self._allowed_hint.text() != text:
            self._allowed_hint.setText(text)

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

        控件保持 enabled 以便正常读出数值；**不再切 ``NoButtons``** ——
        Qt 6.12 的 QStyleSheetStyle 在「QSpinBox/QDoubleSpinBox + NoButtons +
        主题 QSS 的 padding」组合下会算错文本绘制区：数字被裁成左上角一小块，
        严重时完全不可见（离屏实测抓手：可见深色像素 48 → 0）。用户看到的就是
        「预计集数没显示 / 数字被遮挡」。

        而本主题 QSS 早已把 up/down 按钮画成透明（看不到箭头），两种符号模式
        外观一致；「不能改」由 ``setReadOnly`` 真正保证（只读时点按钮也不改值）。
        """
        recommend = self.radio_recommend.isChecked()
        # 模式C 里两个都是**判定结果**，一个都不能改
        duration_ro = by_count or recommend
        count_ro = (not by_count) or recommend

        self.duration_spin.setReadOnly(duration_ro)
        self.count_spin.setReadOnly(count_ro)
        if recommend:
            self._duration_field.set_label("每集时长（分析后自动判定）")
            self._count_field.set_label("集数（分析后自动判定）")
        else:
            self._duration_field.set_label("预计目标时长（自动）" if by_count else "目标时长")
            self._count_field.set_label("目标集数 N" if by_count else "预计集数（自动）")

        # 推荐方案：分析时由系统把三种策略各解一遍再取代价最低者，此时手选
        # 「策略」既不会决定最终取向、又让人以为"我选的生效了"，索性置灰说清。
        self.strategy_combo.setEnabled(not recommend)
        self._strategy_field.set_help(
            "推荐方案会自动比较三种策略，取总代价最低者" if recommend else ""
        )

    def _set_feas_accent(self, tone: str | None) -> None:
        """结论卡的语义色条，与右下角状态块同步。

        结论只有四种，色条让"能不能开工"在一眼之内就有答案，不必读完那几行字。
        """
        self._feas_card.set_accent(tone)

    def _set_derived_help(self, by_count: bool, text: str) -> None:
        field = self._duration_field if by_count else self._count_field
        field.set_help(text)

    def _apply_derived(self, settings, total, report, by_count: bool) -> None:
        """把派生目标值写回只读控件：模式A 反算集数，模式B 反算时长。"""
        if settings.split_mode == SplitMode.RECOMMEND:
            # 模式C：时长与集数都交给分析后的自动判定，这里既不反算也不显示
            # 旧值——显示一个"看着像用户设的"数字，正是这次要消除的误导。
            decided = settings.decided_episode_count
            if decided:
                self._set_value(self.count_spin, decided)
                self._set_value(
                    self.duration_spin, float(settings.decided_target_seconds or 0)
                )
                self._count_field.set_help(f"上次分析判定为 {decided} 集（只读）")
                self._duration_field.set_help(
                    f"上次分析判定为 {float(settings.decided_target_seconds or 0):.1f} 秒（只读）"
                )
            else:
                self._count_field.set_help("分析后自动判定")
                self._duration_field.set_help("分析后自动判定")
            return

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