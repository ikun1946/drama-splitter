"""状态徽章（StatusPill）。

圆角小标签，用于显示「成功 / 失败 / 警告 / 阻断 / 进行中 / 默认」。
颜色由 QSS 中的 ``QLabel[role="pill"][tone="..."]`` 控制。

用法::

    pill = StatusPill("成功", tone="success")
    pill.set_text("失败")  # 运行时切换
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget


class StatusPill(QLabel):
    """圆角小徽章。可用 tone 切换颜色（primary/success/warn/danger/muted）。"""

    _TONE_DEFAULT = "primary"

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