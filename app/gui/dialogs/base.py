"""对话框外壳与自动化约定。

为什么要有外壳
--------------
设置 / 模型 / 导出原先各自是一个"页"（占 stack 位或走右侧抽屉）。它们并不是
工作流的一步——用户只在需要时打开、用完关掉。改成对话框后：

- 工作区只剩真正的阶段内容（导入 → 分析 → 审核）；
- 抽屉的滑入动画、浮层遮挡、截图拼接 hack 一并消失；
- 三个对话框共享同一套标题 / 内容 / 底部按钮版式。

自动化约定（重要，实测踩过）
---------------------------
生产代码只在菜单或按钮处理器里 ``exec()``；测试、冒烟与截图工具**只允许**
``build_*_dialog() + show()``。实例级 ``exec()`` 在离屏环境会永久阻塞——
``tools/gui_smoke.py`` 只拦截 QMessageBox 的静态方法，拦不到实例，曾经挂了
25 分钟才定位。因此凡是要用户做决策的地方（如导出前置检查），必须留一个
可覆写的方法，测试覆写它而不是走弹窗。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DialogShell"]


class DialogShell(QDialog):
    """标题栏 + 内容区 + 底部按钮行。"""

    #: 最小尺寸的下限。内容控件自身的最小宽度之和约 700，再小就会被压扁。
    MIN_WIDTH = 720
    MIN_HEIGHT = 520

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        width: int = 900,
        height: int = 680,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # 按屏幕可用区收缩：小屏笔记本（1366x768）上，960x720 的请求高度会超过
        # 工作区，窗口底部按钮点不到、又因为固定最小尺寸而拖不动。这里把请求尺寸
        # 与最小尺寸都压到屏幕能容纳的范围内——宁可内容滚动，也不让窗口"顶出屏幕"。
        max_w, max_h = self._screen_limits()
        min_w = min(self.MIN_WIDTH, max_w)
        min_h = min(self.MIN_HEIGHT, max_h)
        self.setMinimumSize(min_w, min_h)
        self.resize(min(width, max_w), min(height, max_h))
        self.setSizeGripEnabled(True)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        self.footer_hint = QLabel("")
        self.footer_hint.setProperty("role", "subtitle")
        # 过长时折行而不是被裁掉：底部提示是"这条对话框的规矩"的唯一出处，
        # 裁一半等于没说（厂商管理里就吃过这个亏）
        self.footer_hint.setWordWrap(True)

        self._footer = QHBoxLayout()
        self._footer.setContentsMargins(16, 10, 16, 12)
        self._footer.setSpacing(8)
        self._footer.addWidget(self.footer_hint, 1)

    @staticmethod
    def _screen_limits() -> tuple[int, int]:
        """当前屏幕可用区的 (宽, 高)，留一点边距避免贴着任务栏/边缘。

        拿不到屏幕时（离屏测试、无 GUI 会话）退回一个保守的常用分辨率，
        不让对话框因取不到屏幕而变得不可用。
        """
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            return (1366, 720)
        avail = screen.availableGeometry()
        # 留 40px 纵向余量给标题栏／任务栏，别让窗口顶到屏幕边缘
        return (max(640, avail.width() - 40), max(480, avail.height() - 60))

    # ---- 装配 ----------------------------------------------------------

    def add_body(self, widget: QWidget, stretch: int = 1) -> None:
        self._outer.addWidget(widget, stretch)

    def add_footer(self, widget: QWidget) -> None:
        self._footer.addWidget(widget)

    def install_footer(self) -> None:
        """把底部按钮行挂上。必须在所有 ``add_body`` 之后调用。"""
        holder = QFrame()
        holder.setProperty("role", "footer-bar")
        holder.setLayout(self._footer)
        self._outer.addWidget(holder)

    def add_close_button(self, text: str = "关闭") -> QPushButton:
        button = QPushButton(text)
        # QSS 的 min-height 不进 minimumSizeHint：不显式给高度时按钮会被压扁
        button.setMinimumHeight(32)
        button.clicked.connect(self.close)
        self.add_footer(button)
        return button