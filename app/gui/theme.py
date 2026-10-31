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
# 圆角 4/6/10/16 配合间距 4/8/12/16/24 与字号 11/13/15/18/32，构成一套一致的尺度。
# **输入框与按钮的圆角必须 ≤ 高度/2**，否则 Qt 在四个角上会露出底色。
RADIUS_XS = 4        # 细色条等"几乎只是切了个角"的小元素
RADIUS_SM = 6        # 输入框、进度条、滚动条把手
RADIUS_MD = 10       # 按钮、列表项、浮层
RADIUS_LG = 16       # 卡片、分组块
RADIUS_PILL = 999    # 胶囊标签（日志级别点、状态徽章）

#: 4px 间距网格（4/8/12/16/24）。
#: 6/10/14/18 这类值不在网格上，模板里保持原值并就地注释，不硬凑。
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

#: 卡片标题左侧语义色条的尺寸（原先 QSS 与 card.py 各写一遍字面量）。
ACCENT_BAR_W = 3
ACCENT_BAR_H = 15

FONT_FAMILY = (
    "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', sans-serif"
)
FONT_SYMBOL = "'Segoe UI Symbol', 'Segoe UI Emoji', 'Microsoft YaHei UI'"
FONT_MONO = "Consolas, 'Cascadia Mono', 'Source Code Pro', monospace"

# 字号阶：11 / 13 / 15 / 18 / 32。原先只有 11/12/14 三档，级差太小，
# 除少数几处外整屏看不出层次。HERO 只给空态大图标留位（原 48pt 越阶）。
FONT_SIZE_HERO = 32
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
        "INFO_SOFT": "#e9f2e4",
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
        "INFO_SOFT": "#24331f",
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

/* 后端徽章（本地 / 云端）——只描述现状，不可点，所以没有 hover/pressed */
/* 圆角原先写死 9px，实际含义是"高度的一半"。用胶囊令牌表达同一意图：
   Qt 会把超过半高的半径夹到半高，既不露角，也不会随标签换行而失配。 */
QLabel[role="badge"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    color: {PRIMARY_PRESSED};
    background: {PRIMARY_SOFT};
    border-radius: {RADIUS_PILL}px;
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

QTabBar::tab {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
    /* 6 / 14 不在 4px 网格上：14 是"16 减掉 2px 边框"的结果，标签页要靠它压窄才排得下 */
    padding: 6px 14px;
    margin-right: 2px;
}}

QTabBar::tab:hover {{
    background: {BG_HOVER};
    color: {TEXT};
}}

QTabBar::tab:selected {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    background: {BG_ALT};
    color: {TEXT};
    border-color: {BORDER};
    font-weight: 600;
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
    padding: {SPACE_SM}px {SPACE_MD}px;
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
    /* 6 / 10 不在 4px 网格上：输入框高度按 34 定（6×2 + 20 + 2），换成 8/12
       会把整屏表单撑高 4px；聚焦态还要各减 1px（见下一条），网格值减 1 更偏离。 */
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
    padding: {SPACE_MD}px {SPACE_SM}px {SPACE_SM}px {SPACE_SM}px;
    font-weight: 600;
}}

QGroupBox::title {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    subcontrol-origin: margin;
    subcontrol-position: top left;
    /* 0 贴边、6 压进边框：两者都不是 4px 网格上的值，保持原样 */
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}

/* ===== 列表 ===== */
QListWidget {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: {SPACE_XS}px;
    outline: none;
}}

QListWidget::item {{
    padding: {SPACE_SM}px {SPACE_MD}px;
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
    alternate-background-color: {BG_LOG};
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
    /* 卡片内边距统一 12：标题四边都走 SPACE_MD */
    padding: {SPACE_MD}px;
}}

/* 顶部留 0：标题自己已有 12px 下边距，body 再加 12 会把"标题↔正文"撑成 24。
   card.py 里 body 布局另设 16px 外边距，所以最终缩进是 12 + 16；
   这里只保证 QSS 这一层的声明统一为 12。 */
QFrame[role="card-body"] {{
    background: {BG_ALT};
    border: none;
    padding: 0 {SPACE_MD}px {SPACE_MD}px {SPACE_MD}px;
}}

/* 卡片标题左侧的语义色条：彩色点缀只出现在这种小面积元素上，
   大面积色块在 QSS 里最容易失控（圆角处露底、深浅两套难同步）。
   尺寸走 ACCENT_BAR_* 令牌；tone 配色由 _tone_rules() 统一生成，此处不再重写。 */
QFrame[role="accent-bar"] {{
    border: none;
    border-radius: {RADIUS_XS}px;
    min-width: {ACCENT_BAR_W}px;
    max-width: {ACCENT_BAR_W}px;
    min-height: {ACCENT_BAR_H}px;
    max-height: {ACCENT_BAR_H}px;
}}

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

/* pill 的 tone 配色不在这里逐条写死：与 accent-bar 共用 _tone_rules()
   里同一份 (tone → 令牌) 表，避免同一个语义色两处各写一遍、改一处漏一处。 */

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

/* ===== 云端模型首屏 ===== */
QLabel[role="provider-heading"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_DISPLAY}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="provider-step"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

/* ===== 导出首屏 ===== */
QLabel[role="export-heading"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_DISPLAY}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QFrame[role="export-metric"] {{
    background: {BG_LOG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
}}

QLabel[role="export-metric-value"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="export-metric-caption"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
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
    /* 5px 是"把手宽度 10 的一半"，改成 RADIUS_SM(6) 后 Qt 会夹到半宽，视觉完全一致 */
    border-radius: {RADIUS_SM}px;
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
    /* 5px 是"把手宽度 10 的一半"，改成 RADIUS_SM(6) 后 Qt 会夹到半宽，视觉完全一致 */
    border-radius: {RADIUS_SM}px;
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

/* 导出页正文不是滚动区，但仍要透明：否则深色主题下 QWidget 的默认
   白底会在两张卡片之间露出一条亮边，看起来像卡片边框断开。 */
QWidget#exportBody {{
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
    /* 0 是刻意的：选中态靠底部 2px 实线贴合分隔线，圆角会把那条线咬断。
       6px 垂直内边距不在网格上，它和 2px 下边框一起凑出标签高度。 */
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
    /* 原 2px：色条只有几像素宽，RADIUS_XS 会被 Qt 夹到半宽，等于原来的圆头 */
    border-radius: {RADIUS_XS}px;
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

/* 紧凑档（第二档的窄版）：控制条收成 5 键 + 「步进」菜单后仍需紧凑档，
   否则主栏会被按钮的最小宽度顶宽、把预览画面挤扁。
   role="mini" 是历史别名，与 compact="true" 共用同一组声明——两档视觉必须
   一致，否则同一屏里"紧凑按钮"会长出两种尺寸。 */
QPushButton[compact="true"], QPushButton[role="mini"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    padding: {SPACE_XS}px {SPACE_SM}px;
    font-size: {FONT_SIZE_CAPTION}pt;
}}

QPushButton[role="primary"][compact="true"] {{
    padding: {SPACE_XS}px {SPACE_SM}px;
}}

/* ===== 顶栏 / 底栏（原先散在各控件里的硬编码颜色收拢于此） ===== */
QFrame[role="app-bar"] {{
    background: {BG_ALT};
    border-bottom: 1px solid {BORDER};
}}

QLabel[role="app-bar-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-weight: 600;
    color: {TEXT};
    padding: 0 {SPACE_LG}px;
}}

QLabel[role="status-text"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    color: {TEXT_MUTED};
    padding: 0 {SPACE_LG}px;
}}

QFrame[role="footer-bar"] {{
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
}}

/* ===== 拖放导入区 ===== */
/* drop-zone 整个区域是自绘的（虚线框、悬停高亮都在 DropZone.paintEvent 里），
   这里只给它一个无边框的容器底色，免得默认 QFrame 的边框切进圆角里。 */
QFrame[role="drop-zone"] {{
    background: {BG_ALT};
    border: none;
    border-radius: {RADIUS_LG}px;
}}

/* 空态大图标：原来 48pt 完全在字阶之外，收进 HERO（32pt）仍是最醒目的一档 */
QLabel[role="drop-icon"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_HERO}pt;
    background: transparent;
}}

QLabel[role="drop-icon"][compact="true"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_DISPLAY}pt;
}}

QLabel[role="drop-title"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}pt;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}

QLabel[role="drop-title"][compact="true"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
    font-weight: 400;
    color: {TEXT_MUTED};
}}

QLabel[role="drop-hint"] {{
    font-family: {FONT_SYMBOL}, {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}pt;
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

/* 预览下方的「当前时间」：这一行是"现在在哪"，比旁边的总时长/帧号更该被看见 */
QLabel[role="timecode-now"] {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_TITLE}pt;
    font-weight: 600;
    color: {TEXT};
}}
"""


# ---------------------------------------------------------------------------
# tone 配色：pill 与 accent-bar 共用同一份数据
# ---------------------------------------------------------------------------
#: tone → (pill 底色, pill 文字色, 色条实心色)，都是令牌名。
#: 两处共用这张表，改一处不会漏另一处；INFO_SOFT 也因此有了用处。
_TONE_COLORS: dict[str, tuple[str, str, str]] = {
    "primary": ("PRIMARY_SOFT", "PRIMARY_PRESSED", "PRIMARY"),
    "success": ("SUCCESS_SOFT", "SUCCESS", "SUCCESS"),
    "warn": ("WARN_SOFT", "WARN", "WARN"),
    "danger": ("DANGER_SOFT", "DANGER", "DANGER"),
    "info": ("INFO_SOFT", "INFO", "INFO"),
    "muted": ("NEUTRAL_SOFT", "TEXT_MUTED", "NEUTRAL"),
}

#: pill 支持的 tone（muted 是"无可强调"档，色条上没有对应语义）
_PILL_TONES = ("primary", "success", "warn", "danger", "info", "muted")
#: accent-bar 支持的 tone（与 Card.ACCENTS 对齐，另加 info）
_ACCENT_TONES = ("primary", "success", "warn", "danger", "info")


def _tone_rules(palette: dict[str, str]) -> str:
    """由同一张 tone 表生成 pill / accent-bar 的配色规则。"""
    lines: list[str] = []
    for tone in _PILL_TONES:
        soft, text, _solid = _TONE_COLORS[tone]
        lines.append(
            f'QLabel[role="pill"][tone="{tone}"] '
            f"{{ background: {palette[soft]}; color: {palette[text]}; }}"
        )
    for tone in _ACCENT_TONES:
        _soft, _text, solid = _TONE_COLORS[tone]
        lines.append(
            f'QFrame[role="accent-bar"][tone="{tone}"] '
            f"{{ background: {palette[solid]}; }}"
        )
    lines.append(
        'QFrame[role="accent-bar"][tone="none"] { background: transparent; }'
    )
    return "\n".join(lines)


def build_stylesheet(mode: str = DEFAULT_MODE) -> str:
    """按主题模式渲染全局样式表。"""
    palette = dict(tokens(mode))
    # 派生量：不在 TOKENS 里，避免两套字典要多写派生键
    palette["RADIUS_XS"] = RADIUS_XS
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
    palette["ACCENT_BAR_W"] = ACCENT_BAR_W
    palette["ACCENT_BAR_H"] = ACCENT_BAR_H
    palette["FONT_FAMILY"] = FONT_FAMILY
    palette["FONT_SYMBOL"] = FONT_SYMBOL
    palette["FONT_MONO"] = FONT_MONO
    palette["FONT_SIZE_HERO"] = FONT_SIZE_HERO
    palette["FONT_SIZE_DISPLAY"] = FONT_SIZE_DISPLAY
    palette["FONT_SIZE_TITLE"] = FONT_SIZE_TITLE
    palette["FONT_SIZE_BODY"] = FONT_SIZE_BODY
    palette["FONT_SIZE_CAPTION"] = FONT_SIZE_CAPTION
    palette["FONT_SIZE_MONO"] = FONT_SIZE_MONO
    return _RAW_TEMPLATE.format(**palette) + "\n" + _tone_rules(palette)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def build_palette(mode: str = DEFAULT_MODE):
    """按令牌生成 QPalette，与样式表一起切换。

    为什么光有样式表不够
    -------------------
    QSS 只覆盖它显式写到的属性。没写 ``background`` 的容器——``QStackedWidget`` 的页、
    ``QScrollArea`` 的视口、``QTabWidget`` 的 pane、不设 role 的自绘 ``QFrame``——
    会退回**系统调色板**。系统是浅色时，深色主题下就冒出一块块白板（实测：日志面板
    亮度 239、模型列表行 217、导出的统计条 239，满分 255）。因此两套主题必须连调色板
    一起换，而不是只换字符串。
    """
    from PySide6.QtGui import QColor, QPalette

    t = tokens(mode)
    palette = QPalette()
    for role, key in (
        (QPalette.ColorRole.Window, "BG"),
        (QPalette.ColorRole.WindowText, "TEXT"),
        (QPalette.ColorRole.Base, "BG_ALT"),
        (QPalette.ColorRole.AlternateBase, "BG_LOG"),
        (QPalette.ColorRole.Text, "TEXT"),
        (QPalette.ColorRole.Button, "BG_ALT"),
        (QPalette.ColorRole.ButtonText, "TEXT"),
        (QPalette.ColorRole.ToolTipBase, "BG_ALT"),
        (QPalette.ColorRole.ToolTipText, "TEXT"),
        (QPalette.ColorRole.PlaceholderText, "TEXT_MUTED"),
        (QPalette.ColorRole.Highlight, "PRIMARY"),
    ):
        palette.setColor(role, QColor(t[key]))
    # 选中项上的文字要能在强调色上读：浅色主色偏深 → 白字；深色主色偏亮 → 深字
    palette.setColor(
        QPalette.ColorRole.HighlightedText,
        QColor("#ffffff" if mode != "dark" else t["BG"]),
    )
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["TEXT_MUTED"]))
    return palette


def apply_theme(app: QApplication | None = None, mode: str = DEFAULT_MODE) -> None:
    """把全局样式表与调色板注入到 QApplication。

    mode 取 ``"light"`` / ``"dark"``；非法值退回默认浅色，不抛异常。
    同一 mode 下多次调用幂等（字符串相同，不做无谓的 setStyleSheet）。
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    sheet = build_stylesheet(mode)
    # 顺序要紧：样式表在 polish 时会把当时的调色板**快照**到每个控件上。先设调色板
    # 再注入样式表，控件才拿到新主题的底色；反过来做，深色下会留下一批浅色快照
    # （实测右栏 QStackedWidget 页仍是 #f4f5f9）。
    #
    # 只在下真变了的时候设：setPalette 会向每个控件发 PaletteChange 并触发样式重新
    # 解析——真实窗口（233 个控件）约 0.08s 可接受，但同一个 QApplication 里累积
    # 上千个控件时会被放大到几十秒（测试套件里实测过）。
    palette = build_palette(mode)
    if app.palette() != palette:
        app.setPalette(palette)
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
