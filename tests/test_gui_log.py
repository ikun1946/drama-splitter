"""结构化日志面板与左栏布局的回归测试。

日志面板最容易出的三类问题都在这里钉住：
1. 折叠把**不同**的结论误并成一条（信息丢失，比不折叠严重得多）
2. 折叠后 ``toPlainText()`` 丢内容（人工复制与下游消费者会断）
3. 追加日志时把用户正在看的滚动位置顶走
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from app.gui.widgets.log_panel import MAX_ENTRIES, LogPanel, fold_key

REPEAT = "集内没有完整对白——只有画面没有叙事单元，多半不是预期切法。"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def panel(qapp):
    """一个不显示的日志面板。

    刻意**不 show()**：本文件与 test_gui_smoke 同进程。夹具窗口一旦成为"最后一个
    可见窗口"并在回收时关闭，Qt 会置上退出挂起态，后续文件里的 `qapp.exec()`
    随即**立刻返回** —— 播放用例于是报"一帧都没产出"，错误信息指向解码，
    实际是上一个文件关掉了事件循环。滚动条量程改由用例显式设定，不依赖窗口。
    """
    widget = LogPanel()
    widget.resize(320, 200)
    qapp.processEvents()
    yield widget
    widget.deleteLater()


def rows(panel: LogPanel) -> list[tuple[str, int]]:
    return [(row._msg.text(), row.count()) for row in panel._rows]


def qapp_process(widget) -> None:
    """跑一次事件循环，让布局、信号与滚动条量程落地。"""
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


class TestFolding:
    def test_episode_only_differences_fold(self, panel):
        panel.append("整集复核（阶段4）：")
        for index in (1, 2, 3, 4):
            panel.append(f"第0{index}集[留意] {REPEAT}")
        qapp_process(panel)

        assert panel.count() == 5, "五条都在，只是渲染成两行"
        assert len(panel._rows) == 2
        assert panel._rows[1].count() == 4
        assert "×4" in panel._rows[1]._fold_btn.text()

    def test_different_messages_never_fold(self, panel):
        """误并是最严重的退化：会把不同结论说成同一条。"""
        for index in (1, 2, 3, 4):
            panel.append(f"第0{index}集[留意] 完全不同的第 {index} 种情况")
        qapp_process(panel)
        assert len(panel._rows) == 4
        assert all(row.count() == 1 for row in panel._rows)

    def test_different_levels_never_fold(self, panel):
        panel.append(REPEAT, "info")
        panel.append(REPEAT, "warn")
        qapp_process(panel)
        assert len(panel._rows) == 2, "级别不同就不该并成一条"

    def test_non_adjacent_identical_do_not_fold(self, panel):
        """不跨时间折叠：隔开的同文不该被并掉。"""
        panel.append("同样的结论")
        panel.append("别的内容")
        panel.append("同样的结论")
        qapp_process(panel)
        assert len(panel._rows) == 3

    def test_expand_shows_every_occurrence(self, panel):
        for index in (1, 2, 3):
            panel.append(f"第0{index}集[留意] {REPEAT}")
        qapp_process(panel)
        row = panel._rows[0]
        assert len(row._msg.text().splitlines()) == 1

        row._fold_btn.click()
        qapp_process(panel)
        assert row.is_expanded()
        assert len(row._msg.text().splitlines()) == 3, "点开必须真的看到全部"
        assert row._fold_btn.text() == "收起"

        row._fold_btn.click()
        qapp_process(panel)
        assert not row.is_expanded()
        assert len(row._msg.text().splitlines()) == 1

    def test_fold_key_only_normalises_episode_numbers(self):
        assert fold_key("第01集 ok") == fold_key("第2集 ok")
        assert fold_key("第01集 ok") != fold_key("第01集 失败")
        assert fold_key("完全不同的两句话") != fold_key("另一句完全不同的话")


class TestCompatibility:
    def test_public_api_unchanged(self, panel):
        """append / clear / toPlainText 三个入口与旧版一致。"""
        panel.append("第一条")
        panel.append("第二条", "warn")
        text = panel.toPlainText()
        assert "第一条" in text and "第二条" in text
        assert "[WARN]" in text, "级别前缀的旧格式要保留"
        assert text.startswith("["), "时间码前缀要保留"
        panel.clear()
        assert panel.toPlainText() == ""
        assert panel.count() == 0

    def test_folding_never_loses_text(self, panel):
        """折叠只该改屏幕上的排版，**不能**改能拿走的文本。

        早先的实现只输出折叠段的首条加一句计数，等于把第 02/03/04 集的结论
        从复制结果里抹掉了 —— 这比"不折叠"严重得多。
        """
        for index in (1, 2, 3, 4):
            panel.append(f"第0{index}集[留意] {REPEAT}")
        qapp_process(panel)
        assert len(panel._rows) == 1, "界面上折成一行"
        text = panel.toPlainText()
        for index in (1, 2, 3, 4):
            assert f"第0{index}集" in text, f"第0{index}集 从导出文本里消失了"
        assert "以上 4 条仅集号不同" in text

    def test_cap_collapses_the_oldest(self, panel, monkeypatch):
        """超过上限后最早的那些收起。

        这里把 ``MAX_ENTRIES`` 调小而不是真的灌 800 条：每次追加都要新建一个
        ``QWidget`` 并触发布局，800 条在离屏环境下要跑好几分钟——测试要验的是
        收起**逻辑**，不是构造速度。真实上限由 ``MAX_ENTRIES`` 常量守着。
        """
        from app.gui.widgets import log_panel as module

        monkeypatch.setattr(module, "MAX_ENTRIES", 20)
        for index in range(25):
            panel.append(f"第 {index} 行")
        qapp_process(panel)
        assert panel.count() == 20
        assert panel._notice._label.text() == "更早的 5 条日志已收起"
        assert "第 0 行" not in panel.toPlainText()
        assert "第 24 行" in panel.toPlainText()

    def test_cap_constant_is_sane(self):
        """上限本身也要有理由：太小会丢日志，太大逐行控件扛不住。"""
        assert 100 <= MAX_ENTRIES <= 2000


class TestScrollFollow:
    # 滚动条量程直接手工设定：真实窗口里由布局算出来，而本文件不显示窗口
    # （理由见 panel 夹具），不设量程就测不了"上滚"这个动作。
    RANGE = 1000

    def test_appends_do_not_yank_a_reader_back_to_bottom(self, panel):
        """用户手动上滚后，追加日志不许把位置顶走。"""
        bar = panel._area.verticalScrollBar()
        bar.setRange(0, self.RANGE)
        bar.setValue(self.RANGE)
        qapp_process(panel)
        assert panel.following(), "在底部时应当跟随"

        bar.setValue(self.RANGE // 3)
        qapp_process(panel)
        assert not panel.following(), "上滚后应暂停自动跟随"

        position = bar.value()
        panel.append("新来的一条")
        qapp_process(panel)
        assert bar.value() == position, "追加把正在看的位置顶走了"

    def test_returns_to_bottom_resumes_following(self, panel):
        bar = panel._area.verticalScrollBar()
        bar.setRange(0, self.RANGE)
        bar.setValue(self.RANGE // 3)
        qapp_process(panel)
        assert not panel.following()

        bar.setValue(self.RANGE)
        qapp_process(panel)
        assert panel.following(), "回到底部应恢复自动跟随"

        panel.append("又一条")
        qapp_process(panel)
        assert bar.value() == self.RANGE, "跟随时新内容应把位置带到底部"


class TestLayoutRegression:
    """左栏与集列表的裁剪回归。

    这里**刻意不建 MainWindow**：主窗口带着播放器和探针线程，多一个实例就会把
    同进程后续用例的解码时间抢光（实测会让 test_gui_smoke 的播放头用例失败）。
    三条断言要读的只是控件自身的滚动条策略与换行属性，直接构造被测控件即可。
    """

    def test_left_column_has_no_horizontal_scrollbar(self, qapp):
        from app.gui.panes.import_pane import ImportPane
        from app.gui.state import ProjectState
        from app.gui.widgets.scroll_area import scrollable

        area = scrollable(ImportPane(ProjectState()))
        assert area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def test_episode_table_stretches_and_never_scrolls_horizontally(self, qapp):
        from PySide6.QtWidgets import QHeaderView

        from app.gui.panes.review_pane import ReviewPane
        from app.gui.state import ProjectState

        table = ReviewPane(ProjectState()).episode_table
        assert (
            table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        for column in range(table.columnCount()):
            assert (
                table.horizontalHeader().sectionResizeMode(column)
                == QHeaderView.ResizeMode.Stretch
            )

    def test_format_hint_wraps(self, qapp):
        """「支持 .mp4 / …」漏了 wordWrap 就会被硬裁。"""
        from PySide6.QtWidgets import QLabel

        from app.gui.panes.import_pane import ImportPane
        from app.gui.state import ProjectState

        pane = ImportPane(ProjectState())
        hints = [label for label in pane.findChildren(QLabel) if "支持" in label.text()]
        assert hints, "找不到格式说明那一行"
        assert all(label.wordWrap() for label in hints)


class TestLeftColumnFitsItsWidth:
    """左栏装不下内容时，``scrollable()`` 关掉的横滚动条会**静默裁切**。

    这组用例把"看不全"变成会红的测试：任何一次文案变长、QSS 内边距调大、
    图标换成宽字形，都会当场在这里炸出来，而不是等用户截图反馈。

    判据用 ``minimumSizeHint`` 而**不是** ``sizeHint``：后者是一个不换行
    QLabel 的单行文本宽度（实测「支持 .mp4 / .mov / …」一行 285px），
    远超栏宽，但它会换行，根本不会裁切。真正决定"会不会被裁"的是最小宽度。
    """

    #: 左栏目标栏宽（``workbench.splitter.setSizes`` 的第一项）
    COLUMN = 360

    def _review_window(self, width: int):
        """构造一个审核态窗口（套真实主题——量的是样式化后的最小宽度）。"""
        from app.core.ffmpeg import check_encoders, locate_ffmpeg
        from app.core.plan import BoundaryPlan
        from app.core.probe import probe_media
        from app.gui import theme
        from app.gui.main_window import MainWindow

        theme.apply_theme(QApplication.instance(), "light")
        binaries = locate_ffmpeg()
        check_encoders(binaries)
        win = MainWindow()
        win.resize(width, 889)
        win.show()
        win.state.binaries = binaries
        media = probe_media(binaries, _ASSET)
        win.state.media = media
        win.state.plans = [
            BoundaryPlan.uniform(2, media.video_time_base, media.duration_ticks())
        ]
        win.workbench.refresh()
        QApplication.instance().processEvents()
        QApplication.instance().processEvents()
        return win

    def test_nothing_is_clipped_at_any_usable_width(self, qapp):
        """从最小窗口宽到 1600，左栏里的卡片都不该超出视口。"""
        from app.gui.widgets import Card

        for width in (1320, 1440, 1600):
            win = self._review_window(width)
            try:
                viewport = win.workbench._left_review.viewport().width()
                pane = win.workbench.review_pane
                clipped = [
                    card.title()
                    for card in pane.findChildren(Card)
                    if card.width() > viewport
                ]
                assert not clipped, (
                    f"窗口 {width}px 时左栏视口只有 {viewport}px，"
                    f"这些卡片被裁掉：{clipped}"
                )
            finally:
                win.close()
                qapp.processEvents()

    def test_left_column_keeps_its_width_at_the_minimum_window(self, qapp):
        """最小窗口宽下左栏不能被中栏挤扁。

        ``QSplitter.setSizes`` 在子控件最小宽度之和超标时会被**静默忽略**：
        窗口够小也照样被压到 170px，而横滚动条已关，内容于是被裁且不报错。
        """
        win = self._review_window(1320)
        try:
            assert win.workbench.splitter.sizes()[0] >= self.COLUMN - 20
        finally:
            win.close()
            qapp.processEvents()

    def test_every_ops_button_fits_two_across(self, qapp):
        """切点操作的两列行：两个按钮 + 间距必须放得下。

        QSS 里按钮水平内边距每调大 1px，每行就宽 2px；曾经用 16 时两行合计
        331px，把整张卡顶到 397 并在 310px 的视口里被裁掉半截。
        """
        from app.gui import theme
        from app.gui.panes.review_pane import ReviewPane
        from app.gui.state import ProjectState

        theme.apply_theme(qapp, "light")
        pane = ReviewPane(ProjectState())
        pane.resize(self.COLUMN, 700)
        qapp_process(pane)
        budget = self.COLUMN - 32 - 8     # 32 = 卡片内边距，8 = 网格水平间距
        for left, right in (("btn_lock", "btn_unlock"), ("btn_confirm", "btn_snap")):
            need = (
                getattr(pane, left).minimumSizeHint().width()
                + getattr(pane, right).minimumSizeHint().width()
            )
            assert need <= budget, (
                f"{left} + {right} 需要 {need}px，两列可用 {budget}px"
            )


#: 合成测试素材；缺失时这些用例应当跳过而不是误报"裁切"
_ASSET = str((Path(__file__).resolve().parents[1] / "testdata" / "cfr_120s_25fps.mp4"))


class TestProviderDialogFits:
    """「模型管理 → 云端模型」这一页的裁剪回归。

    同一类病：内容最小宽度超过滚动视口，而横滚动条已关 → 静默裁切。
    这里最隐蔽的是**按钮被切**：「保存并测试连接」少一半的时候，界面看上去
    仍然「正常」，用户只会以为要点两下。
    """

    def _dialog(self, qapp):
        from app.core.ffmpeg import check_encoders, locate_ffmpeg
        from app.gui import theme
        from app.gui.dialogs.model_dialog import ModelDialog
        from app.gui.state import ProjectState

        theme.apply_theme(qapp, "light")
        binaries = locate_ffmpeg()
        check_encoders(binaries)
        dialog = ModelDialog(ProjectState(), providers_tab=True)
        dialog.show()
        qapp.processEvents()
        qapp.processEvents()
        return dialog

    @staticmethod
    def _widest_area(dialog):
        return max(dialog.findChildren(QScrollArea), key=lambda a: a.viewport().width())

    def test_no_card_is_clipped(self, qapp):
        from app.gui.widgets import Card

        dialog = self._dialog(qapp)
        try:
            viewport = self._widest_area(dialog).viewport().width()
            clipped = [
                card.title()
                for card in dialog.providers.findChildren(Card)
                if card.width() > viewport
            ]
            assert not clipped, f"视口 {viewport}px，这些卡片被裁：{clipped}"
        finally:
            dialog.close()

    def test_primary_button_is_fully_visible(self, qapp):
        """「保存并测试连接」是这一页唯一的主动作，被切一半等于没有。"""
        from PySide6.QtWidgets import QPushButton

        dialog = self._dialog(qapp)
        try:
            area = self._widest_area(dialog)
            for button in dialog.providers.findChildren(QPushButton):
                if "保存并测试连接" not in button.text():
                    continue
                right = button.mapTo(area.viewport(), button.rect().topRight()).x()
                assert right <= area.viewport().width(), (
                    f"「{button.text()}」右边缘 {right} 超出视口 "
                    f"{area.viewport().width()}，按钮被切"
                )
                return
            raise AssertionError("没找到「保存并测试连接」按钮")
        finally:
            dialog.close()

    def test_dialog_cannot_be_shrunk_below_its_content(self, qapp):
        """最小宽度必须跟住初始宽度：QDialog 默认能缩到外壳的 720px。"""
        dialog = self._dialog(qapp)
        try:
            assert dialog.minimumWidth() >= dialog.width() - 1
        finally:
            dialog.close()

    def test_result_of_probing_appears_below_the_button(self, qapp):
        """点了「保存并测试连接」，结果必须出现在**看得到的地方**。

        这条钉的是一个很难用截图发现的毛病：结果原本写在长表单的最末尾，
        而按钮在首屏。用户点了按钮、什么也没发生——因为消息在折叠线以下。
        """
        dialog = self._dialog(qapp)
        try:
            area = self._widest_area(dialog)
            form = dialog.providers
            # 离线环境必定测不通，正好触发「要写失败原因」这条路径
            form._save_and_probe()
            qapp.processEvents()

            assert form.hint_label.text(), "点了按钮却没有任何结果文字"
            assert not form.hint_label.isHidden(), "结果文字设了但标签还藏着"
            top = form.hint_label.mapTo(area.viewport(), form.hint_label.rect().topLeft())
            assert 0 <= top.y() < area.viewport().height(), (
                f"结果写在 y={top.y()}，视口只有 {area.viewport().height()} 高，"
                f"用户看不到——这就是「点了没反应」"
            )
        finally:
            dialog.close()

    def test_hint_hides_itself_when_empty(self, qapp):
        """提示行不该常年占位（没有消息时留白）。"""
        dialog = self._dialog(qapp)
        try:
            form = dialog.providers
            form._set_hint("")
            assert form.hint_label.isHidden()
            form._set_hint("有消息了")
            assert not form.hint_label.isHidden()
        finally:
            dialog.close()

    def test_first_screen_has_only_connection_fields(self, qapp):
        """首屏只处理厂商连接；模型用途与清单都进模型设置/进阶设置。"""
        dialog = self._dialog(qapp)
        try:
            form = dialog.providers
            area = self._widest_area(dialog)
            assert not form.advanced_toggle.isChecked()
            assert form.models_host.isHidden(), "模型清单默认收起"
            for widget in (form.key_field, form.allow_upload, form.cloud_hint):
                top = widget.mapTo(area.viewport(), widget.rect().topLeft()).y()
                assert top < area.viewport().height(), (
                    f"{widget.objectName() or widget.__class__.__name__} 首屏被截断"
                )
            assert not hasattr(form, "judge_choice")
            assert not hasattr(form, "asr_choice")
        finally:
            dialog.close()

    def test_long_profile_names_are_elided_not_clipped(self, qapp):
        """厂商名超长时画省略号，且原始文案仍在 item 里（按 id 查找不受影响）。"""
        from app.gui.widgets import ElidingItemDelegate

        dialog = self._dialog(qapp)
        try:
            listing = dialog.providers.profile_list
            assert isinstance(listing.itemDelegate(), ElidingItemDelegate)
            assert listing.count() > 0, "厂商列表是空的"
            # 数据没被改：仍按 id 与全文工作
            assert listing.item(0).data(Qt.ItemDataRole.UserRole)
            assert listing.item(0).text(), "原始文案必须保留给 tooltip 与查找"
            # 绘制宽度被 sizeHint 限住，不至于把这一栏顶到最长条目的宽度
            assert listing.minimumSizeHint().width() <= 300
        finally:
            dialog.close()
