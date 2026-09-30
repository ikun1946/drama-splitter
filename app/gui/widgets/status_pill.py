"""彩色标签胶囊（StatusPill）。

圆角小标签，是界面里**彩色点缀的主要载体**：可行/警告/无解、未锁定/已锁定/已确认、
复核等级、日志 ×N 折叠计数都走它。颜色由 QSS 中 ``QLabel[role="pill"][tone="..."]`` 控制。

为什么只有这一个胶囊
------------------
设计稿上曾同时出现 ``role="pill"`` 与 ``role="chip"`` 两套规则，字号、圆角、内边距
完全一致——那是同一个东西的两份写法。留着就会变成两套真相：改了一处忘了另一处，
界面上一半胶囊变圆了另一半没变。这里只保留 ``pill``，组件名仍是 ``StatusPill``
（改名要动全部调用方，收益不抵风险）。

用法::

    pill = StatusPill("已锁定", tone="primary")
    pill.set_text("未锁定")
    pill.set_tone("neutral")
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

__all__ = ["StatusPill"]


class StatusPill(QLabel):
    """圆角小徽章。用 tone 切换配色档位。"""

    _TONE_DEFAULT = "primary"

    #: 可用档位。与 QSS 里的 ``[role="pill"][tone=...]`` 规则一一对应，
    #: 少写一个就会出现"传了 tone 但没变色"且没有任何报错的情况。
    TONES = ("primary", "success", "warn", "danger", "muted")

    def __init__(
        self,
        text: str = "",
        tone: str = _TONE_DEFAULT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "pill")
        self._apply_tone(tone)

    def set_text(self, text: str) -> None:
        self.setText(text)

    def set_tone(self, tone: str) -> None:
        """切换配色档位。"""
        self._apply_tone(tone)

    def _apply_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        # dynamic property 在已显示的 widget 上要重新 polish 才生效
        self.style().unpolish(self)
        self.style().polish(self)
