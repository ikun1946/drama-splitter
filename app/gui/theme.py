"""GUI 主题：设计令牌与 QSS 样式表。

设计目标
--------
- 现代、信息层级清晰：标题、卡片、徽章、按钮状态（默认 / hover / pressed / disabled / primary / danger）有视觉区分
- 配色克制：单一主色（浅色 #3b6cb8 / 深色 #4aa6ff），辅色用于阻断 / 警告 / 提示 / 成功 / 进行中
- 不依赖任何外部资源：图标用 QStyle.StandardPixmap，颜色用 token 常量

双套令牌
--------
``TOKENS["light"]`` 与 ``TOKENS["dark"]`` 是同一个键集合的两份取值。QSS 由
:func:`build_stylesheet` 按 mode 渲染，因此换主题不需要改任何页面代码。

新增颜色键时必须**同时**补进两套字典 —— :func:`_validate_tokens` 会在导入时
校验两套键集合一致，缺一个直接抛错，避免出现「深色下某处漏色」这种难查的问题。

字体族回退链（重要）
-------------------
每条由 QSS 设置 ``font-size`` / ``font-weight`` 的规则都必须显式带 ``font-family``。
Qt 的 QSS 一旦覆盖字号，会连带把字体族重置成默认值，于是 "⚙" / "🤖" / "🎬"
这类符号落到没有该字形的默认字体上，渲染成豆腐块。实测：只写 font-size 的
QLabel 显示豆腐块，同时写 font-family 的正常。全局 ``QWidget`` 规则里的
family 救不了它们。

调用方
------
``apply_theme(app)`` 注入浅色；``apply_theme(app, "dark")`` 注入深色。
多次调用幂等（同一 mode 下字符串相同）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------------------
# 设计令牌
# ---------------------------------------------------------------------------

# 与颜色无关的令牌：两套主题共用
RADIUS_SM = 4
RADIUS_MD = 8
RADIUS_LG = 12

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

FONT_FAMILY = (
    "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', sans-serif"
)
FONT_SYMBOL = "'Segoe UI Symbol', 'Segoe UI Emoji', 'Microsoft YaHei UI'"
FONT_MONO = "Consolas, 'Cascadia Mono', 'Source Code Pro', monospace"

FONT_SIZE_TITLE = 14
FONT_SIZE_BODY = 12
FONT_SIZE_CAPTION = 11
FONT_SIZE_MONO = 12

# 配色：两套主题按键集合完全一致
#
# light 是既有的浅色方案（原始提交值，未改动任何色值）。
# dark 按方案 §31–33 取深色底 + 单强调色 #4AA6FF，并补齐浅色有而方案未列出的角色色
# （info / neutral / 各 pill 底色等）——数量必须与 light 一一对应。
TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "PRIMARY": "#3b6cb8",
        "PRIMARY_HOVER": "#4a7bc6",
        "PRIMARY_PRESSED": "#2f5aa0",
        "PRIMARY_SOFT": "#e8f0fc",
        "DANGER": "#b3261e",
        "DANGER_HOVER": "#c4332a",
        "DANGER_SOFT": "#fbe6e4",
        "WARN": "#9a6700",
        "WARN_SOFT": "#fbf2dd",
        "INFO": "#3d6b35",
        "SUCCESS": "#2e7d32",
        "SUCCESS_SOFT": "#e7f4ea",
        "NEUTRAL": "#5f6368",
        "BG": "#f5f6fa",
        "BG_ALT": "#ffffff",
        "BG_HOVER": "#eef1f7",
        "BG_SELECTED": "#e8f0fc",
        "BG_LOG": "#fafbfd",
        "BORDER": "#d9dce4",
        "BORDER_STRONG": "#b8bdc8",
        "TEXT": "#20232a",
        "TEXT_MUTED": "#5f6368",
        "TEXT_DISABLED": "#a0a3ac",
        "TEXT_INVERSE": "#ffffff",
        # 时间轴（自绘控件取色）
        "TL_TRACK": "#e4e7ef",
        "TL_SEGMENT": "#f8f9fc",
        "TL_SEGMENT_ALT": "#eef1f7",
        # 视频画面区：亮色主题下也保持深底（视频画布的通行做法），
        # 但与界面其余部分一样走令牌，避免再出现内联硬编码
        "VIDEO_BG": "#0e0f13",
        "VIDEO_BORDER": "#1a1c22",
        "VIDEO_TEXT": "#bbbbbb",
        "HUD_BG": "#1a1c22",
        "HUD_TEXT": "#cfd2d8",
    },
    "dark": {
        "PRIMARY": "#4aa6ff",
        "PRIMARY_HOVER": "#6bb7ff",
        "PRIMARY_PRESSED": "#3189dd",
        "PRIMARY_SOFT": "#1c2c3d",
        "DANGER": "#f4685c",
        "DANGER_HOVER": "#ff8074",
        "DANGER_SOFT": "#3a2222",
        "WARN": "#e0b355",
        "WARN_SOFT": "#3a3320",
        "INFO": "#7fc47a",
        "SUCCESS": "#6cc47a",
        "SUCCESS_SOFT": "#1f3325",
        "NEUTRAL": "#8a8f98",
        "BG": "#181a1d",
        "BG_ALT": "#202226",
        "BG_HOVER": "#2a2d32",
        "BG_SELECTED": "#2a3644",
        "BG_LOG": "#1b1d21",
        "BORDER": "#33373d",
        "BORDER_STRONG": "#4a4f57",
        "TEXT": "#e6e8ec",
        "TEXT_MUTED": "#9aa0a8",
        "TEXT_DISABLED": "#5c6169",
        "TEXT_INVERSE": "#0f1113",
        # 时间轴（自绘控件取色）
        "TL_TRACK": "#2a2d32",
        "TL_SEGMENT": "#23262b",
        "TL_SEGMENT_ALT": "#1e2126",
        # 视频画面区（深色底下与界面同族，但仍是独立令牌）
        "VIDEO_BG": "#0e0f13",
        "VIDEO_BORDER": "#26292f",
        "VIDEO_TEXT": "#bbbbbb",
        "HUD_BG": "#1a1c22",
        "HUD_TEXT": "#cfd2d8",
    },
}


def _validate_tokens() -> None:
    """两套主题的键集合必须一致，否则深色下会漏染色。"""
    light_keys = set(TOKENS["light"])
    dark_keys = set(TOKENS["dark"])
    if light_keys != dark_keys:
        missing_in_dark = sorted(light_keys - dark_keys)
        missing_in_light = sorted(dark_keys - light_keys)
        raise RuntimeError(
            "主题令牌两套不一致："
            f"dark 缺少 {missing_in_dark}；light 缺少 {missing_in_light}"
        )


_validate_tokens()

DEFAULT_MODE = "light"
MODES = tuple(TOKENS)


def tokens(mode: str = DEFAULT_MODE) -> dict[str, str]:
    """取一套颜色令牌。mode 非法时退回默认，不抛异常（避免 UI 起不来）。"""
    if mode not in TOKENS:
        mode = DEFAULT_MODE
    return TOKENS[mode]


# 兼容旧引用：默认浅色的扁平常量。
# 内部一律走 TOKENS，新代码请直接使用 tokens(mode)[...]。
COLOR_PRIMARY = TOKENS[DEFAULT_MODE]["PRIMARY"]
COLOR_DANGER = TOKENS[DEFAULT_MODE]["DANGER"]
COLOR_WARN = TOKENS[DEFAULT_MODE]["WARN"]
COLOR_INFO = TOKENS[DEFAULT_MODE]["INFO"]
COLOR_SUCCESS = TOKENS[DEFAULT_MODE]["SUCCESS"]
COLOR_BG = TOKENS[DEFAULT_MODE]["BG"]
COLOR_BG_ALT = TOKENS[DEFAULT_MODE]["BG_ALT"]
COLOR_BORDER = TOKENS[DEFAULT_MODE]["BORDER"]
COLOR_TEXT = TOKENS[DEFAULT_MODE]["TEXT"]
COLOR_TEXT_MUTED = TOKENS[DEFAULT_MODE]["TEXT_MUTED"]


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------

_RAW_TEMPLATE = """
/* ===== 全局 ===== */
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    color: {TEXT};
}}

QMainWindow, QDialog {{
    background: {BG};
}}

/* ===== 工具栏 / 状态栏 ===== */
QStatusBar {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QStatusBar::item {{
    border: none;
}}

/* ===== 标签 ===== */
/*
 * 注意：以下每条设置 font-size / font-weight 的规则都显式带上 font-family。
 * Qt 的 QSS 在覆盖 font-size 时会重置字体族为默认值，符号字符（⚙ 🤖 🎬）
 * 会因此落到无字形的字体上显示为豆腐块。全局规则里的 family 救不了它们。
 */
QLabel {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT};
    background: transparent;
}}

QLabel[role="title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
}}

QLabel[role="subtitle"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {TEXT_MUTED};
}}

QLabel[role="mono"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MONO}pt;
    color: {TEXT};
}}

QLabel[role="muted"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

/* 卡片标题 */
QLabel[role="card-title-text"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
}}

/* 状态徽章 */
QLabel[role="pill"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
}}

/* ===== 按钮 ===== */
QPushButton {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px 14px;
    min-height: 18px;
}}

QPushButton:hover {{
    background: {BG_HOVER};
    border-color: {BORDER_STRONG};
}}

QPushButton:pressed {{
    background: {BG_SELECTED};
}}

QPushButton:disabled {{
    color: {TEXT_DISABLED};
    background: {BG};
    border-color: {BORDER};
}}

QPushButton[role="primary"] {{
    background: {PRIMARY};
    color: {TEXT_INVERSE};
    border-color: {PRIMARY};
    font-weight: 600;
}}

QPushButton[role="primary"]:hover {{
    background: {PRIMARY_HOVER};
    border-color: {PRIMARY_HOVER};
}}

QPushButton[role="primary"]:pressed {{
    background: {PRIMARY_PRESSED};
    border-color: {PRIMARY_PRESSED};
}}

QPushButton[role="primary"]:disabled {{
    background: {BG_HOVER};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}

QPushButton[role="danger"] {{
    color: {DANGER};
    border-color: {DANGER};
}}

/* 必须有这一条：属性选择器 [role="danger"] 排在通用 :disabled 之后，
   不给它单独的 disabled 规则，置灰的"删除"看起来仍然可以点 */
QPushButton[role="danger"]:disabled {{
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}

QPushButton[role="danger"]:hover {{
    background: {DANGER};
    color: {TEXT_INVERSE};
}}

/* ===== 输入控件 ===== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
    min-height: 22px;
    selection-background-color: {BG_SELECTED};
    selection-color: {TEXT};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {PRIMARY};
}}

QLineEdit:read-only {{
    background: {BG};
    color: {TEXT_MUTED};
}}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}

QComboBox::drop-down {{
    border: none;
    width: 18px;
}}

QComboBox QAbstractItemView {{
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {BG_SELECTED};
    selection-color: {TEXT};
    outline: none;
}}

/* ===== 复选 / 单选 ===== */
QCheckBox, QRadioButton {{
    spacing: 6px;
    color: {TEXT};
}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {TEXT_DISABLED};
}}

/* ===== 分组框 ===== */
QGroupBox {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}

/* ===== 列表 ===== */
QListWidget {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
    outline: none;
}}

QListWidget::item {{
    padding: 8px 12px;
    border-radius: {RADIUS_SM}px;
    color: {TEXT};
}}

QListWidget::item:hover {{
    background: {BG_HOVER};
}}

QListWidget::item:selected {{
    background: {BG_SELECTED};
    color: {PRIMARY_PRESSED};
}}

/* ===== 表格 ===== */
QTableWidget {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    gridline-color: {BORDER};
    selection-background-color: {BG_SELECTED};
    selection-color: {TEXT};
}}

QHeaderView::section {{
    background: {BG};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}

QTableWidget::item {{
    padding: 4px 6px;
}}

/* ===== 进度条 ===== */
QProgressBar {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    text-align: center;
    color: {TEXT};
    min-height: 16px;
}}

QProgressBar::chunk {{
    background: {PRIMARY};
    border-radius: {RADIUS_SM_INNER}px;
}}

/* ===== 文本面板 ===== */
QTextEdit, QPlainTextEdit {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 8px;
    color: {TEXT};
    selection-background-color: {BG_SELECTED};
    selection-color: {TEXT};
}}

QTextEdit[role="log"], QPlainTextEdit[role="log"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MONO}pt;
    background: {BG_LOG};
}}

QTextEdit:read-only, QPlainTextEdit:read-only {{
    background: {BG_ALT};
}}

/* ===== 卡片（QFrame 派生） ===== */
QFrame[role="card"] {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
}}

QFrame[role="card-title"] {{
    /* font-family 必须显式写出：QSS 设了 padding / font-* 任一属性后，
       Qt 会把控件字体重置为默认值，中文因此落到 SimSun 这类高行距字体上，
       卡片标题被撑高十几像素，多张卡片叠起来会把下半页顶出可视区。 */
    font-family: {FONT_FAMILY};
    background: {BG};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-top-left-radius: {RADIUS_MD}px;
    border-top-right-radius: {RADIUS_MD}px;
    padding: 6px 12px;
}}

QFrame[role="card-body"] {{
    background: {BG_ALT};
    border: none;
    padding: 12px;
}}

QLabel[role="card-title-text"] {{
    font-weight: 600;
    color: {TEXT};
}}

/* ===== 状态徽章 ===== */
QLabel[role="pill"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    border-radius: {RADIUS_PILL}px;
    padding: 2px 8px;
    font-size: {FONT_SIZE_CAPTION}pt;
    font-weight: 600;
}}

QLabel[role="pill"][tone="primary"] {{
    background: {PRIMARY_SOFT};
    color: {PRIMARY_PRESSED};
}}

QLabel[role="pill"][tone="success"] {{
    background: {SUCCESS_SOFT};
    color: {SUCCESS};
}}

QLabel[role="pill"][tone="warn"] {{
    background: {WARN_SOFT};
    color: {WARN};
}}

QLabel[role="pill"][tone="danger"] {{
    background: {DANGER_SOFT};
    color: {DANGER};
}}

QLabel[role="pill"][tone="muted"] {{
    background: {BG_HOVER};
    color: {TEXT_MUTED};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {NEUTRAL};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-width: 24px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    height: 0;
    width: 0;
}}

/* ===== 分割器 ===== */
QSplitter::handle {{
    background: {BORDER};
}}

QSplitter::handle:horizontal {{
    width: 1px;
}}

QSplitter::handle:vertical {{
    height: 1px;
}}

/* ===== 滚动容器 ===== */
/* viewport 默认用调色板 Base（浅色下是白），深色主题里会顶出一块块白底。
   全部改透明，让窗口背景透出来；滚动条另按主题着色。 */
QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QAbstractScrollArea::corner {{
    background: transparent;
}}

/* ===== 工具提示 ===== */
QToolTip {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {TEXT};
    color: {TEXT_INVERSE};
    border: 1px solid {TEXT};
    padding: 4px 8px;
    border-radius: {RADIUS_SM}px;
    font-size: {FONT_SIZE_CAPTION}pt;
}}

/* ===== 右栏检查器 ===== */
QFrame[role="inspector"] {{
    background: {BG};
    border-left: 1px solid {BORDER};
}}

QFrame[role="inspector-tabs"] {{
    background: {BG_ALT};
    border: none;
    border-bottom: 1px solid {BORDER};
}}

QPushButton[role="inspector-tab"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 6px 4px;
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QPushButton[role="inspector-tab"]:hover {{
    color: {TEXT};
    background: {BG_HOVER};
}}

QPushButton[role="inspector-tab"]:checked {{
    color: {PRIMARY};
    border-bottom: 2px solid {PRIMARY};
    font-weight: 600;
}}

QFrame[role="issue-row"] {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
}}

QFrame[role="issue-bar"] {{
    border: none;
    border-radius: 1px;
}}

QFrame[role="issue-bar"][tone="danger"] {{
    background: {DANGER};
}}

QFrame[role="issue-bar"][tone="warn"] {{
    background: {WARN};
}}

QFrame[role="issue-bar"][tone="primary"] {{
    background: {PRIMARY};
}}

QLabel[role="issue-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT};
    font-weight: 600;
}}

QLabel[role="issue-meta"] {{
    font-family: {FONT_MONO};
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QLabel[role="issue-detail"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QLabel[role="issue-hint"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {PRIMARY};
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QPushButton[role="ghost"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: transparent;
    border: none;
    color: {PRIMARY};
    padding: 2px 6px;
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QPushButton[role="ghost"]:hover {{
    color: {PRIMARY_HOVER};
    background: {PRIMARY_SOFT};
    border-radius: {RADIUS_SM}px;
}}

/* 预览控制条紧凑档：10 个按钮单行，默认 padding 6px 14px 时总宽
   超过中栏可用宽度，控制条会横向滚动（见 MediaPreview 的 h_scrollable） */
QPushButton[role="mini"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    padding: 4px 8px;
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QPushButton[role="primary"][compact="true"] {{
    padding: 4px 8px;
}}

/* ===== 顶栏 / 状态条 / 底栏（原先散在各控件里的硬编码颜色收拢于此） ===== */
QFrame[role="app-bar"] {{
    background: {BG_ALT};
    border-bottom: 1px solid {BORDER};
}}

QLabel[role="app-bar-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-weight: 600;
    color: {TEXT};
    padding: 0 16px;
}}

QFrame[role="status-strip"] {{
    background: {BG};
    border-bottom: 1px solid {BORDER};
}}

QLabel[role="status-text"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT_MUTED};
    padding: 0 16px;
}}

QFrame[role="footer-bar"] {{
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
}}

QLabel[role="hint"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {WARN};
    padding: 0 16px;
}}

/* ===== 拖放导入区文字（inline 样式改角色，深浅主题各取令牌） ===== */
QLabel[role="drop-icon"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: 48pt;
    background: transparent;
}}

QLabel[role="drop-icon"][compact="true"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: 20pt;
}}

QLabel[role="drop-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: 14pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="drop-title"][compact="true"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: 10pt;
    font-weight: 400;
    color: {TEXT_MUTED};
}}

QLabel[role="drop-hint"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: 10pt;
    color: {TEXT_MUTED};
    background: transparent;
}}

/* ===== 底栏（单页工作台唯一的状态行） ===== */
QFrame[role="bottom-bar"] {{
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
}}

QLabel[role="bottom-caption"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {TEXT_MUTED};
}}

QLabel[role="bottom-hint"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {WARN};
}}

QProgressBar[role="bottom-progress"] {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    min-height: 6px;
    max-height: 6px;
}}

QProgressBar[role="bottom-progress"]::chunk {{
    background: {PRIMARY};
    border-radius: {RADIUS_SM_INNER}px;
}}

/* ===== 菜单栏 / 菜单 ===== */
QMenuBar {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    color: {TEXT};
    border-bottom: 1px solid {BORDER};
    padding: 2px 6px;
}}

QMenuBar::item {{
    background: transparent;
    padding: 4px 10px;
    border-radius: {RADIUS_SM}px;
}}

QMenuBar::item:selected {{
    background: {BG_HOVER};
}}

QMenu {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: {RADIUS_SM}px;
}}

QMenu::item:selected {{
    background: {PRIMARY_SOFT};
    color: {PRIMARY_PRESSED};
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

/* ===== 视频画面区（原内联硬编码，改角色后深浅主题都可控） ===== */
QLabel[role="video-canvas"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {VIDEO_BG};
    color: {VIDEO_TEXT};
    border: 1px solid {VIDEO_BORDER};
}}

QLabel[role="mono-hud"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MONO}pt;
    color: {HUD_TEXT};
    background: {HUD_BG};
    padding: 4px 10px;
}}
"""


def build_stylesheet(mode: str = DEFAULT_MODE) -> str:
    """按主题模式渲染全局样式表。"""
    palette = dict(tokens(mode))
    # 派生量：不在 TOKENS 里，避免两套字典要多写派生键
    palette["RADIUS_SM"] = RADIUS_SM
    palette["RADIUS_MD"] = RADIUS_MD
    palette["RADIUS_SM_INNER"] = RADIUS_SM - 1
    palette["RADIUS_PILL"] = RADIUS_SM + 2
    palette["FONT_FAMILY"] = FONT_FAMILY
    palette["FONT_SYMBOL"] = FONT_SYMBOL
    palette["FONT_MONO"] = FONT_MONO
    palette["FONT_SIZE_TITLE"] = FONT_SIZE_TITLE
    palette["FONT_SIZE_BODY"] = FONT_SIZE_BODY
    palette["FONT_SIZE_CAPTION"] = FONT_SIZE_CAPTION
    palette["FONT_SIZE_MONO"] = FONT_SIZE_MONO
    return _RAW_TEMPLATE.format(**palette)


# 兼容旧引用：默认浅色样式表
STYLESHEET = build_stylesheet(DEFAULT_MODE)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def apply_theme(app: QApplication | None = None, mode: str = DEFAULT_MODE) -> None:
    """把全局样式表注入到 QApplication。

    mode 取 ``"light"`` / ``"dark"``；非法值退回默认浅色，不抛异常。
    同一 mode 下多次调用幂等（字符串相同，不做无谓的 setStyleSheet）。
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    sheet = build_stylesheet(mode)
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
    global _CURRENT_MODE
    _CURRENT_MODE = mode if mode in MODES else DEFAULT_MODE


# 运行时当前模式。apply_theme 是唯一写入口，供自绘控件（DropZone 等）
# 在 paintEvent 里取当前主题的令牌——避免每处都自己保存一份 mode。
_CURRENT_MODE = DEFAULT_MODE


def current_mode() -> str:
    """返回 apply_theme 最近一次应用的主题模式。"""
    return _CURRENT_MODE


def is_dark(mode: str = DEFAULT_MODE) -> bool:
    """给需要按主题分叉的绘制代码用（如时间轴自绘背景）。"""
    return mode == "dark"


def qcolor(mode: str, key: str):
    """取 QColor，供自绘控件使用。

    延迟导入 QColor，避免纯逻辑测试在没有 QtGui 的环境下 import 失败。
    """
    from PySide6.QtGui import QColor

    return QColor(tokens(mode)[key])
