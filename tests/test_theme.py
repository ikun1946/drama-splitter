"""主题与样式的守卫测试。

这些用例守的不是"好看"，而是**几种一旦发生就很难查的退化**：

1. 浅深两套令牌键不一致 → 某个颜色在深色主题下漏掉，表现为"那块是白的"
2. 代码里用了 ``role="x"`` 但 QSS 没定义 → 那个控件完全不受样式管
3. QSS 规则写了 ``font-size`` 却没写 ``font-family`` → **中文变豆腐块**（本项目踩过）
4. QSS 写了 ``box-shadow`` 之类 Qt 不支持的属性 → 看着写了、实际没反应，
   还会让人以为"层次已经做过了"
5. 大圆角配小高度 → 四个角露出底色
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.gui.theme import (
    FONT_SIZE_BODY,
    FONT_SIZE_CAPTION,
    FONT_SIZE_DISPLAY,
    FONT_SIZE_TITLE,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_SM,
    TOKENS,
    build_stylesheet,
)

GUI_ROOT = Path(__file__).resolve().parents[1] / "app" / "gui"


@pytest.fixture(scope="module")
def qapp():
    """一个 QApplication。

    ``WA_DontShowOnScreen`` 那条不是这里的重点，但同一进程里后面还有别的 GUI
    测试文件：真 show() 出来的窗口一旦成为"最后一个可见窗口"并在回收时关闭，
    Qt 会置上退出挂起态，后续文件的 ``qapp.exec()`` 立刻返回。留个隐患不如先堵。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app

#: Qt 的 QSS 解析器会静默忽略的常见 CSS 属性。写了等于没写，还会误导后来人。
UNSUPPORTED_PROPS = ("box-shadow:", "text-shadow:", "filter:", "letter-spacing:")


def _strip_comments(sheet: str) -> str:
    return re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)


def _rules(sheet: str) -> list[tuple[str, str]]:
    """把 QSS 拆成 ``(选择器, 声明块)`` 列表。"""
    return [
        (match.group(1).strip(), match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", sheet)
    ]


class TestTokenParity:
    def test_light_and_dark_have_the_same_keys(self):
        assert set(TOKENS["light"]) == set(TOKENS["dark"])

    def test_shadow_edge_exists_in_both(self):
        """伪阴影是整套层次体系的唯一来源，少一套主题整屏就塌回"完全平"。"""
        for mode in ("light", "dark"):
            assert TOKENS[mode]["SHADOW_EDGE"]
        # 深色下必须**更深**：亮边会被读成高光而不是阴影
        assert _luminance(TOKENS["dark"]["SHADOW_EDGE"]) < _luminance(
            TOKENS["dark"]["BORDER"]
        )
        assert _luminance(TOKENS["light"]["SHADOW_EDGE"]) < _luminance(
            TOKENS["light"]["BORDER"]
        )

    def test_no_placeholder_colors(self):
        for mode in ("light", "dark"):
            for key, value in TOKENS[mode].items():
                assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{mode}.{key}={value}"


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class TestTypeAndRadiusScale:
    def test_font_sizes_are_strictly_increasing(self):
        sizes = [FONT_SIZE_CAPTION, FONT_SIZE_BODY, FONT_SIZE_TITLE, FONT_SIZE_DISPLAY]
        assert sizes == sorted(sizes)
        assert len(set(sizes)) == 4, "字号阶必须真的分出层次，不能两档同值"
        # 级差要够大，1pt 的差距在界面里看不出来
        assert min(b - a for a, b in zip(sizes, sizes[1:])) >= 2

    def test_radius_order(self):
        assert RADIUS_SM < RADIUS_MD < RADIUS_LG

    def test_both_modes_render(self):
        for mode in ("light", "dark"):
            sheet = build_stylesheet(mode)
            assert len(sheet) > 5000
            assert "{" in sheet and "}" in sheet


class TestStylesheetHygiene:
    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_no_unsupported_css_properties(self, mode):
        """Qt 的 QSS 没有 box-shadow —— 写了不会报错，但层次根本没做出来。"""
        sheet = _strip_comments(build_stylesheet(mode))
        for prop in UNSUPPORTED_PROPS:
            assert prop not in sheet, f"{prop} 在 Qt QSS 里不生效，别自我欺骗"

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_font_size_rules_always_carry_font_family(self, mode):
        """豆腐块守卫（本项目已踩过）。

        QSS 一旦覆盖 font-size / font-weight，Qt 会把字体族一并重置为默认值，
        中文因此落到没有字形的字体上。**全局规则里的 font-family 救不了它**，
        每条规则都得自己写。
        """
        sheet = _strip_comments(build_stylesheet(mode))
        offenders = [
            selector
            for selector, body in _rules(sheet)
            if ("font-size" in body or "font-weight" in body)
            and "font-family" not in body
        ]
        assert not offenders, f"这些规则会出豆腐块：{offenders}"

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_no_unrendered_placeholders(self, mode):
        """模板里每个 ``{占位符}`` 都必须在渲染时被替换掉。

        漏掉一个不会报错，只会在界面上留下字面量 ``{PRIMARY}``。
        注意不能笼统地查 ``{``：渲染后的 QSS 里 ``{`` / ``}`` 是 CSS 语法本身，
        模板里也是靠 ``{{`` 转义的。只认"大写词 + 尖括号"这种占位符形状。
        """
        leftovers = re.findall(r"\{[A-Z][A-Z_0-9]*\}", _strip_comments(build_stylesheet(mode)))
        assert not leftovers, f"有占位符没被替换：{sorted(set(leftovers))}"


class TestRoleClosure:
    def test_every_role_used_in_code_is_defined_in_qss(self):
        used: set[str] = set()
        pattern = re.compile(r'setProperty\(\s*["\']role["\']\s*,\s*["\']([a-z0-9\-]+)["\']')
        for path in GUI_ROOT.rglob("*.py"):
            if path.name == "theme.py":
                continue
            used.update(pattern.findall(path.read_text(encoding="utf-8")))

        defined = set(re.findall(r'\[role="([a-z0-9\-]+)"\]', build_stylesheet("light")))
        missing = sorted(used - defined)
        assert not missing, f"代码里用了但 QSS 没定义：{missing}"
        assert used, "守卫本身失效：一条 role 都没扫到"


class TestProviderControlSizing:
    def test_scrollable_keeps_scroll_area_children_unconstrained(self, qapp):
        """滚动容器清零子控件宽度时必须跳过列表/表的视口。

        清零视口最小宽度会让 ``QListWidget`` 把条目撑到最长厂商名，把整页顶宽；
        正确做法是让列表自己按可用宽度省略，条目全文留在 tooltip 里。
        """
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from app.gui.widgets import scrollable

        inner = QWidget()
        layout = QVBoxLayout(inner)
        label = QLabel("很长的说明文字")
        label.setWordWrap(True)
        label.setMinimumWidth(600)
        layout.addWidget(label)
        area = scrollable(inner)
        assert inner.minimumWidth() == 0, "内容根节点必须能随滚动视口收缩"
        assert label.minimumWidth() == 0, "普通标签仍要清零，换行才会生效"
        area.close()
        area.deleteLater()


class TestElevationAndGeometry:
    def test_card_uses_bottom_shadow_edge(self):
        """卡片浮起只能来自底边；没有这一条，卡片就和背景同平。

        断言的是**解析后的颜色值**而不是 ``{SHADOW_EDGE}`` 字面量——渲染时
        占位符已经换成具体色值，在成品样式表里搜占位符当然搜不到。
        """
        for mode in ("light", "dark"):
            body = dict(_rules(_strip_comments(build_stylesheet(mode)))).get(
                'QFrame[role="card"]', ""
            )
            assert "border-bottom" in body, f"{mode} 卡片没有底边"
            assert TOKENS[mode]["SHADOW_EDGE"] in body, (
                f"{mode} 卡片的底边没用 SHADOW_EDGE，纵深不成立"
            )
            # 底边要比其余三边深，否则读出来是描边不是浮起
            match = re.search(r"border-bottom:\s*(\d+)px solid (\S+);", body)
            assert match and int(match.group(1)) >= 2, f"{mode} 底边至少 2px 才读得出来"
            assert match.group(2) == TOKENS[mode]["SHADOW_EDGE"]

    def test_card_radius_is_the_large_one(self):
        body = dict(_rules(_strip_comments(build_stylesheet("light")))).get(
            'QFrame[role="card"]', ""
        )
        assert f"border-radius: {RADIUS_LG}px" in body

    def test_input_radius_does_not_exceed_half_its_height(self):
        """大圆角配矮控件会在四角露出底色——这是选 6 而不是 16 的原因。"""
        body = dict(_rules(_strip_comments(build_stylesheet("light")))).get(
            "QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox", ""
        )
        radius = re.search(r"border-radius: (\d+)px", body)
        minimum = re.search(r"min-height: (\d+)px", body)
        assert radius and minimum
        # 控件实际高 = min-height + 上下 padding + 上下边框
        padding = re.search(r"padding: (\d+)px", body)
        height = int(minimum.group(1)) + 2 * int(padding.group(1)) + 2
        assert int(radius.group(1)) <= height / 2


class TestDropdownCaret:
    """下拉箭头必须看得见。

    QSS 里只要给子控件写了**任何**属性（哪怕 ``border: none``），Qt 的样式表引擎
    就接管它的绘制、替换掉原生箭头；而没有 ``image:`` 就什么也不画。结果整个应用
    的下拉框长得跟输入框一样，用户看不出"这里能展开"。这里用像素采样钉住。
    """

    @staticmethod
    def _darkest_in_dropdown(combo, x_from: int, x_to: int) -> int:
        """下拉区里的最深像素亮度。纯白 = 没画任何东西。"""
        image = combo.grab().toImage()
        darkest = 255
        for y in range(8, 26):
            for x in range(x_from, x_to):
                pixel = image.pixelColor(x, y)
                darkest = min(darkest, pixel.red(), pixel.green(), pixel.blue())
        return darkest

    def test_combo_draws_its_own_caret(self, qapp):
        from PySide6.QtCore import Qt

        from app.gui import theme
        from app.gui.widgets import NoWheelComboBox

        theme.apply_theme(qapp, "light")
        combo = NoWheelComboBox()
        combo.addItems(["本地 whisper base", "云端 deepseek-flash"])
        combo.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        combo.resize(300, 34)
        combo.show()
        qapp.processEvents()
        darkest = self._darkest_in_dropdown(combo, 272, 296)
        combo.close()
        assert darkest < 200, f"下拉区没有画出箭头（最深像素 {darkest}，纯白说明什么都没画）"

    def test_caret_fades_when_disabled(self, qapp):
        from PySide6.QtCore import Qt

        from app.gui import theme
        from app.gui.widgets import NoWheelComboBox

        theme.apply_theme(qapp, "light")
        combo = NoWheelComboBox()
        combo.addItems(["a", "b"])
        combo.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        combo.resize(300, 34)
        combo.show()
        qapp.processEvents()
        enabled = self._darkest_in_dropdown(combo, 272, 296)
        combo.setEnabled(False)
        qapp.processEvents()
        disabled = self._darkest_in_dropdown(combo, 272, 296)
        combo.close()
        assert disabled > enabled, "禁用态的箭头应当比常态浅"

    def test_no_native_image_asset_required(self):
        """箭头是自己画的，QSS 里不该出现指向图片资源的 url()。"""
        sheet = _strip_comments(build_stylesheet("light"))
        for line in sheet.splitlines():
            if "image:" in line:
                assert "url(" not in line, f"引入了外部图片资源：{line.strip()}"


class TestScrollAreaPolicy:
    def test_scrollable_disables_horizontal_scroll(self):
        """左栏 320px：横滚动条一出现，所有文字就被裁。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        from app.gui.widgets.scroll_area import scrollable

        area = scrollable(QLabel("一行很长很长的文字" * 20))
        assert (
            area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # h_scrollable（预览控制条）必须重新打开，否则那排按钮会被裁掉
        from app.gui.widgets.scroll_area import h_scrollable

        row = h_scrollable(QLabel("x" * 400))
        assert (
            row.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        assert row.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
