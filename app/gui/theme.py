"""GUI 主题：设计令牌与 QSS 样式表。

设计目标
--------
- 现代、信息层级清晰：标题、卡片、徽章、按钮状态（默认 / hover / pressed / disabled / primary / danger）有视觉区分
- 配色克制：主色 #3b6cb8（蓝灰），辅色用于阻断 / 警告 / 提示 / 成功 / 进行中
- 不依赖任何外部资源：图标用 QStyle.StandardPixmap，颜色用 token 常量

调用方
------
``apply_theme(QApplication.instance())`` 一次性注入；任何 QWidget 在创建时自动继承
（Qt 的 stylesheet 级联在 QApplication 上生效）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------------------
# 设计令牌
# ---------------------------------------------------------------------------

# 配色 —— 在这里集中改，全局生效
COLOR_PRIMARY = "#3b6cb8"
COLOR_PRIMARY_HOVER = "#4a7bc6"
COLOR_PRIMARY_PRESSED = "#2f5aa0"
COLOR_DANGER = "#b3261e"
COLOR_DANGER_HOVER = "#c4332a"
COLOR_WARN = "#9a6700"
COLOR_INFO = "#3d6b35"
COLOR_SUCCESS = "#2e7d32"
COLOR_NEUTRAL = "#5f6368"

COLOR_BG = "#f5f6fa"
COLOR_BG_ALT = "#ffffff"
COLOR_BG_HOVER = "#eef1f7"
COLOR_BG_SELECTED = "#e8f0fc"
COLOR_BORDER = "#d9dce4"
COLOR_BORDER_STRONG = "#b8bdc8"
COLOR_TEXT = "#20232a"
COLOR_TEXT_MUTED = "#5f6368"
COLOR_TEXT_DISABLED = "#a0a3ac"
COLOR_TEXT_INVERSE = "#ffffff"

# 圆角
RADIUS_SM = 4
RADIUS_MD = 8
RADIUS_LG = 12

# 间距
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

# 字体
#
# 必须显式把符号字体排进回退链，且**每个由 QSS 设置 font-size 的规则都要带上
# font-family**：Qt 的 QSS 一旦覆盖 font-size，会连带把字体族重置成默认值，
# 于是 "⚙" / "🤖" 这类符号落到没有该字形的默认字体上，渲染成豆腐块。
# 实测：只写 font-size 的 QLabel 显示豆腐块，同时写 font-family 的正常。
FONT_FAMILY = (
    "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', sans-serif"
)
FONT_SYMBOL = "'Segoe UI Symbol', 'Segoe UI Emoji', 'Microsoft YaHei UI'"
FONT_MONO = "Consolas, 'Cascadia Mono', 'Source Code Pro', monospace"

# 字号
FONT_SIZE_TITLE = 14
FONT_SIZE_BODY = 12
FONT_SIZE_CAPTION = 11
FONT_SIZE_MONO = 12


# ---------------------------------------------------------------------------
# 样式表
# ---------------------------------------------------------------------------

STYLESHEET = f"""
/* ===== 全局 ===== */
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    color: {COLOR_TEXT};
}}

QMainWindow, QDialog {{
    background: {COLOR_BG};
}}

/* ===== 工具栏 / 状态栏 ===== */
QStatusBar {{
    background: {COLOR_BG_ALT};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT_MUTED};
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
    color: {COLOR_TEXT};
    background: transparent;
}}

QLabel[role="title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {COLOR_TEXT};
}}

QLabel[role="subtitle"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {COLOR_TEXT_MUTED};
}}

QLabel[role="mono"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MONO}pt;
    color: {COLOR_TEXT};
}}

QLabel[role="muted"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {COLOR_TEXT_MUTED};
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
    background: {COLOR_BG_ALT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px 14px;
    min-height: 18px;
}}

QPushButton:hover {{
    background: {COLOR_BG_HOVER};
    border-color: {COLOR_BORDER_STRONG};
}}

QPushButton:pressed {{
    background: {COLOR_BG_SELECTED};
}}

QPushButton:disabled {{
    color: {COLOR_TEXT_DISABLED};
    background: {COLOR_BG};
    border-color: {COLOR_BORDER};
}}

QPushButton[role="primary"] {{
    background: {COLOR_PRIMARY};
    color: {COLOR_TEXT_INVERSE};
    border-color: {COLOR_PRIMARY};
    font-weight: 600;
}}

QPushButton[role="primary"]:hover {{
    background: {COLOR_PRIMARY_HOVER};
    border-color: {COLOR_PRIMARY_HOVER};
}}

QPushButton[role="primary"]:pressed {{
    background: {COLOR_PRIMARY_PRESSED};
    border-color: {COLOR_PRIMARY_PRESSED};
}}

QPushButton[role="primary"]:disabled {{
    background: {COLOR_BG_HOVER};
    color: {COLOR_TEXT_DISABLED};
    border-color: {COLOR_BORDER};
}}

QPushButton[role="danger"] {{
    color: {COLOR_DANGER};
    border-color: {COLOR_DANGER};
}}

QPushButton[role="danger"]:hover {{
    background: {COLOR_DANGER};
    color: {COLOR_TEXT_INVERSE};
}}

/* ===== 输入控件 ===== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {COLOR_BG_ALT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
    min-height: 22px;
    selection-background-color: {COLOR_BG_SELECTED};
    selection-color: {COLOR_TEXT};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {COLOR_PRIMARY};
}}

QLineEdit:read-only {{
    background: {COLOR_BG};
    color: {COLOR_TEXT_MUTED};
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
    background: {COLOR_BG_ALT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_BG_SELECTED};
    selection-color: {COLOR_TEXT};
    outline: none;
}}

/* ===== 复选 / 单选 ===== */
QCheckBox, QRadioButton {{
    spacing: 6px;
    color: {COLOR_TEXT};
}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {COLOR_TEXT_DISABLED};
}}

/* ===== 分组框 ===== */
QGroupBox {{
    background: {COLOR_BG_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {COLOR_TEXT_MUTED};
    font-weight: 600;
}}

/* ===== 列表 ===== */
QListWidget {{
    background: {COLOR_BG_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
    outline: none;
}}

QListWidget::item {{
    padding: 8px 12px;
    border-radius: {RADIUS_SM}px;
    color: {COLOR_TEXT};
}}

QListWidget::item:hover {{
    background: {COLOR_BG_HOVER};
}}

QListWidget::item:selected {{
    background: {COLOR_BG_SELECTED};
    color: {COLOR_PRIMARY_PRESSED};
}}

/* ===== 表格 ===== */
QTableWidget {{
    background: {COLOR_BG_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    gridline-color: {COLOR_BORDER};
    selection-background-color: {COLOR_BG_SELECTED};
    selection-color: {COLOR_TEXT};
}}

QHeaderView::section {{
    background: {COLOR_BG};
    color: {COLOR_TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    border-right: 1px solid {COLOR_BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}

QTableWidget::item {{
    padding: 4px 6px;
}}

/* ===== 进度条 ===== */
QProgressBar {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    text-align: center;
    color: {COLOR_TEXT};
    min-height: 16px;
}}

QProgressBar::chunk {{
    background: {COLOR_PRIMARY};
    border-radius: {RADIUS_SM - 1}px;
}}

/* ===== 文本面板 ===== */
QTextEdit, QPlainTextEdit {{
    background: {COLOR_BG_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 8px;
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_BG_SELECTED};
    selection-color: {COLOR_TEXT};
}}

QTextEdit[role="log"], QPlainTextEdit[role="log"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MONO}pt;
    background: #fafbfd;
}}

QTextEdit:read-only, QPlainTextEdit:read-only {{
    background: {COLOR_BG_ALT};
}}

/* ===== 卡片（QFrame 派生） ===== */
QFrame[role="card"] {{
    background: {COLOR_BG_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MD}px;
}}

QFrame[role="card-title"] {{
    background: {COLOR_BG};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    border-top-left-radius: {RADIUS_MD}px;
    border-top-right-radius: {RADIUS_MD}px;
    padding: 8px 12px;
}}

QFrame[role="card-body"] {{
    background: {COLOR_BG_ALT};
    border: none;
    padding: 12px;
}}

QLabel[role="card-title-text"] {{
    font-weight: 600;
    color: {COLOR_TEXT};
}}

/* ===== 状态徽章 ===== */
QLabel[role="pill"] {{
    border-radius: {RADIUS_SM + 2}px;
    padding: 2px 8px;
    font-size: {FONT_SIZE_CAPTION}pt;
    font-weight: 600;
}}

QLabel[role="pill"][tone="primary"] {{
    background: {COLOR_BG_SELECTED};
    color: {COLOR_PRIMARY_PRESSED};
}}

QLabel[role="pill"][tone="success"] {{
    background: #e7f4ea;
    color: {COLOR_SUCCESS};
}}

QLabel[role="pill"][tone="warn"] {{
    background: #fbf2dd;
    color: {COLOR_WARN};
}}

QLabel[role="pill"][tone="danger"] {{
    background: #fbe6e4;
    color: {COLOR_DANGER};
}}

QLabel[role="pill"][tone="muted"] {{
    background: {COLOR_BG_HOVER};
    color: {COLOR_TEXT_MUTED};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {COLOR_BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {COLOR_NEUTRAL};
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
    background: {COLOR_BORDER_STRONG};
    border-radius: 5px;
    min-width: 24px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    height: 0;
    width: 0;
}}

/* ===== 分割器 ===== */
QSplitter::handle {{
    background: {COLOR_BORDER};
}}

QSplitter::handle:horizontal {{
    width: 1px;
}}

QSplitter::handle:vertical {{
    height: 1px;
}}

/* ===== 工具提示 ===== */
QToolTip {{
    background: {COLOR_TEXT};
    color: {COLOR_TEXT_INVERSE};
    border: 1px solid {COLOR_TEXT};
    padding: 4px 8px;
    border-radius: {RADIUS_SM}px;
    font-size: {FONT_SIZE_CAPTION}pt;
}}
"""


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def apply_theme(app: QApplication | None = None) -> None:
    """把全局样式表注入到 QApplication。

    多次调用幂等（同一字符串）。
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    if app.styleSheet() != STYLESHEET:
        app.setStyleSheet(STYLESHEET)