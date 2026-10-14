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
#
# 圆角 6/10/16 配合间距 4/8/12/16/24 与字号 11/13/15/18，构成一套一致的尺度。
# **输入框与按钮的圆角必须 ≤ 高度/2**，否则 Qt 在四个角上会露出底色。
RADIUS_SM = 6        # 输入框、进度条
RADIUS_MD = 10       # 按钮、列表项、浮层
RADIUS_LG = 16       # 卡片、分组块
RADIUS_PILL = 999    # 胶囊标签（日志级别点、状态徽章）

#: 8px 间距网格。原先各处随手写 10 / 14 / 18，模块间对不齐。
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

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

# 字号阶：11 / 13 / 15 / 18。原先只有 11/12/14 三档，级差太小，
# 除少数几处外整屏看不出层次。
FONT_SIZE_DISPLAY = 18
FONT_SIZE_TITLE = 15
FONT_SIZE_BODY = 13
FONT_SIZE_CAPTION = 11
FONT_SIZE_MONO = 11

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
        "NEUTRAL": "#5f6672",
        "NEUTRAL_SOFT": "#eef0f4",
        "BG": "#f4f5f9",
        "BG_ALT": "#ffffff",
        "BG_HOVER": "#eef1f7",
        "BG_SELECTED": "#e8f0fc",
        "BG_LOG": "#fafbfd",
        "BORDER": "#e2e5ec",
        "BORDER_STRONG": "#c6cbd6",
        # 伪阴影：Qt 的 QSS **没有 box-shadow**，纵深只能靠"底边用更深的颜色"
        # 伪造。这一条边是整套层次体系的唯一来源。
        "SHADOW_EDGE": "#c9cdd8",
        "TEXT": "#1c1f26",
        "TEXT_MUTED": "#5f6672",
        "TEXT_DISABLED": "#a2a8b4",
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
        "NEUTRAL_SOFT": "#2a2d32",
        "BG": "#181a1d",
        "BG_ALT": "#202226",
        "BG_HOVER": "#2a2d32",
        "BG_SELECTED": "#2a3644",
        "BG_LOG": "#1b1d21",
        "BORDER": "#33373d",
        "BORDER_STRONG": "#4a4f57",
        # 深色下的"伪阴影"要更深而不是更浅——亮边会被读成高光。
        "SHADOW_EDGE": "#0f1113",
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

/* 后端徽章（本地 / 云端）——只描述现状，不可点，所以没有 hover/pressed */
QLabel[role="badge"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {PRIMARY_PRESSED};
    background: {PRIMARY_SOFT};
    border-radius: 9px;
    padding: 1px 9px;
}}
QLabel[role="badge"][tone="warn"] {{
    color: {WARN};
    background: {WARN_SOFT};
}}

/* 状态结论行：一句话说清"此刻到底跑什么" */
QLabel[tone="ok"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {SUCCESS};
}}
QLabel[tone="warn"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {WARN};
    font-weight: 600;
}}

/* 下拉里的分组标题与提示项（禁用态由代码设 item flag，这里只管字色） */
QComboBox QAbstractItemView:disabled {{
    color: {TEXT_DISABLED};
}}

/* ===== 按钮 ===== */
/* 三档：primary（每屏唯一主动作）/ secondary（默认）/ ghost（行内小动作）。
   圆角 10 = RADIUS_MD，控件高约 36（min-height 20 + padding 8 + 边框 2）。
   水平内边距取 12 而不是 16：左栏 320px 里「切点操作」是两列并排的按钮，
   每边 4px 就是每行 8px —— 曾经用 16 时两行合计 331px，超出视口 310 而被静默裁切。 */
QPushButton {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 8px 12px;
    min-height: 20px;
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
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
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
/* 圆角 6 = RADIUS_SM（输入框高度约 34，圆角已接近高度一半，再大就露底） */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    min-height: 20px;
    selection-background-color: {BG_SELECTED};
    selection-color: {TEXT};
}}

/* 聚焦环：QSS 没有 box-shadow，聚焦只能靠边框加粗。
   padding 同步减 1px，否则控件在聚焦时会胖 1px、布局跟着抖。 */
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 2px solid {PRIMARY};
    padding: 5px 9px;
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
    /* 浮层层次 L3：描边用强一档，底边再加深一档 */
    border: 1px solid {BORDER_STRONG};
    border-bottom: 2px solid {SHADOW_EDGE};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
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
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    font-weight: 600;
}}

QGroupBox::title {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
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
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
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
/* 层次 L1：白底 + 1px 常规边框 + **2px 更深的底边**。
   Qt 的 QSS 不支持 box-shadow，这一行边框就是全部的"浮起"——
   给 QFrame 挂 QGraphicsDropShadowEffect 会把它里面的输入框、按钮裁掉。 */
QFrame[role="card"] {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-bottom: 2px solid {SHADOW_EDGE};
    border-radius: {RADIUS_LG}px;
}}

QFrame[role="card-title"] {{
    /* font-family 必须显式写出：QSS 设了 padding / font-* 任一属性后，
       Qt 会把控件字体重置为默认值，中文因此落到 SimSun 这类高行距字体上，
       卡片标题被撑高十几像素，多张卡片叠起来会把下半页顶出可视区。 */
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    background: {BG_ALT};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-top-left-radius: {RADIUS_LG}px;
    border-top-right-radius: {RADIUS_LG}px;
    padding: {SPACE_MD}px {SPACE_LG}px;
}}

QFrame[role="card-body"] {{
    background: {BG_ALT};
    border: none;
    padding: {SPACE_LG}px;
}}

/* 卡片标题左侧的语义色条：彩色点缀只出现在这种小面积元素上，
   大面积色块在 QSS 里最容易失控（圆角处露底、深浅两套难同步）。 */
QFrame[role="accent-bar"] {{
    border: none;
    border-radius: 2px;
    min-width: 3px;
    max-width: 3px;
}}

QFrame[role="accent-bar"][tone="primary"] {{ background: {PRIMARY}; }}
QFrame[role="accent-bar"][tone="success"] {{ background: {SUCCESS}; }}
QFrame[role="accent-bar"][tone="warn"] {{ background: {WARN}; }}
QFrame[role="accent-bar"][tone="danger"] {{ background: {DANGER}; }}
QFrame[role="accent-bar"][tone="none"] {{ background: transparent; }}

QLabel[role="card-title-text"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
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
    background: {NEUTRAL_SOFT};
    color: {TEXT_MUTED};
}}

/* ===== 空态 ===== */
/* 原先所有空态都是一行居中的小字（"暂无问题。"），右栏 95% 面积就那么一句话。 */
QFrame[role="empty-state"] {{
    background: transparent;
    border: none;
}}

QFrame[role="empty-icon"] {{
    background: {PRIMARY_SOFT};
    border: none;
    border-radius: {RADIUS_PILL}px;
}}

QLabel[role="empty-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="empty-detail"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    color: {TEXT_MUTED};
    background: transparent;
}}

/* ===== 集列表行 ===== */
/* 原先是 QTableWidget，四列被挤到出横向滚动条。改用行卡片后每行自成一格。 */
QFrame[role="episode-row"] {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
}}

QFrame[role="episode-row"][selected="true"] {{
    border: 1px solid {PRIMARY};
    background: {PRIMARY_SOFT};
}}

QLabel[role="episode-no"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    font-weight: 600;
    color: {TEXT_MUTED};
    background: {NEUTRAL_SOFT};
    border-radius: {RADIUS_SM}px;
}}

QFrame[role="episode-row"][selected="true"] QLabel[role="episode-no"] {{
    background: {PRIMARY};
    color: {TEXT_INVERSE};
}}

QLabel[role="episode-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    font-weight: 500;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="episode-meta"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {TEXT_MUTED};
    background: transparent;
}}

/* ===== 结构化日志条目 ===== */
/* 原先是一整块等宽 QTextEdit，"[00:52:23] [INFO] 正文" 逐行铺开。
   现在拆成：彩色级别点 + 等宽时间戳 + 自动换行正文。 */
QFrame[role="log-row"] {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_SM}px;
}}

QFrame[role="log-row"]:hover {{
    background: {BG_LOG};
}}

QFrame[role="log-dot"] {{
    border: none;
    border-radius: {RADIUS_PILL}px;
    min-width: 6px; max-width: 6px;
    min-height: 6px; max-height: 6px;
}}

QFrame[role="log-dot"][tone="info"] {{ background: {NEUTRAL}; }}
QFrame[role="log-dot"][tone="success"] {{ background: {SUCCESS}; }}
QFrame[role="log-dot"][tone="warn"] {{ background: {WARN}; }}
QFrame[role="log-dot"][tone="error"] {{ background: {DANGER}; }}

QLabel[role="log-time"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {TEXT_DISABLED};
    background: transparent;
}}

QLabel[role="log-msg"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    color: {TEXT};
    background: transparent;
}}

/* 折叠提示：说明"后面几条与这条重复"，而不是把重复内容再抄一遍 */
QLabel[role="log-foldmark"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {TEXT_DISABLED};
    background: transparent;
    border-left: 2px solid {BORDER};
    padding-left: 10px;
}}

/* ===== 分区小标题（工作台三栏顶部） ===== */
/* 不用 letter-spacing：Qt 的 QSS 不支持它，写了不会报错也不会生效。 */
QLabel[role="section-label"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    font-weight: 600;
    color: {TEXT_MUTED};
    background: transparent;
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

QFrame[role="log-toolbar"] {{
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
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {PRIMARY};
    border-bottom: 2px solid {PRIMARY};
    font-weight: 600;
}}

QFrame[role="issue-row"] {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-bottom: 2px solid {SHADOW_EDGE};
    border-radius: {RADIUS_MD}px;
}}

QFrame[role="issue-bar"] {{
    border: none;
    border-radius: 2px;
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
/* ===== 拖放导入区 ===== */
/* drop-zone 整个区域是自绘的（虚线框、悬停高亮都在 DropZone.paintEvent 里），
   这里只给它一个无边框的容器底色，免得默认 QFrame 的边框切进圆角里。 */
QFrame[role="drop-zone"] {{
    background: {BG_ALT};
    border: none;
    border-radius: {RADIUS_LG}px;
}}

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

/* 预览下方的「当前时间」：这一行是"现在在哪"，比旁边的总时长/帧号更该被看见 */
QLabel[role="timecode-now"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
}}
"""


def build_stylesheet(mode: str = DEFAULT_MODE) -> str:
    """按主题模式渲染全局样式表。"""
    palette = dict(tokens(mode))
    # 派生量：不在 TOKENS 里，避免两套字典要多写派生键
    palette["RADIUS_SM"] = RADIUS_SM
    palette["RADIUS_MD"] = RADIUS_MD
    palette["RADIUS_LG"] = RADIUS_LG
    palette["RADIUS_PILL"] = RADIUS_PILL
    palette["RADIUS_SM_INNER"] = RADIUS_SM - 1
    palette["SPACE_XS"] = SPACE_XS
    palette["SPACE_SM"] = SPACE_SM
    palette["SPACE_MD"] = SPACE_MD
    palette["SPACE_LG"] = SPACE_LG
    palette["SPACE_XL"] = SPACE_XL
    palette["FONT_FAMILY"] = FONT_FAMILY
    palette["FONT_SYMBOL"] = FONT_SYMBOL
    palette["FONT_MONO"] = FONT_MONO
    palette["FONT_SIZE_DISPLAY"] = FONT_SIZE_DISPLAY
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
