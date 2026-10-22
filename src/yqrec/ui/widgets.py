"""几个只有几十行、但必须自绘的小控件。

放这里而不是塞进 `main_window.py`：它们**没有业务逻辑**，只表达"长什么样"，
跟主窗的状态机完全解耦，单测也能单独抓。

- `StatusDot`：乐器标定状态点（8×8，成功/待办/未标定三态）；
- `VLine`：1px 竖分隔（试听胶囊里图标组与「停止」之间）；
- `EditPanel`：手动点选浮层（自己在顶部画一个指向触发按钮的小三角）。

颜色一律取 `theme` 令牌 —— 这三个控件都要跟着日/夜主题重绘。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QWidget

from . import theme

DOT_PX = 8
"""状态点直径（px）。"""

ARROW_HEIGHT = 9
"""浮层顶部留给指向箭头的带高（px）：箭头画在面板内，永远不会被窗口边界裁掉。"""

ARROW_HALF_WIDTH = 7.0
"""箭头半宽（px）。"""


class StatusDot(QWidget):
    """乐器标定状态点：绿=已标定且有基线 / 琥珀=已标定但没基线 / 灰=未标定。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusDot")
        self.setFixedSize(DOT_PX, DOT_PX)
        self._kind = "none"
        self.setToolTip("该乐器参数未标定，只能用当前基线")

    def set_kind(self, kind: str) -> None:
        """`kind` = `ok`（已标定+已学基线）/ `warn`（已标定未学基线）/ `none`（未标定）。"""
        if kind not in ("ok", "warn", "none"):
            kind = "none"
        if kind == self._kind:
            return
        self._kind = kind
        self.update()

    def kind(self) -> str:
        return self._kind

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002 - Qt 签名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        token = {"ok": "success", "warn": "warn", "none": "text-tertiary"}[self._kind]
        painter.setBrush(theme.color(token))
        painter.drawEllipse(QRectF(0, 0, DOT_PX, DOT_PX))
        painter.end()


class VLine(QFrame):
    """1px 竖分隔（高 14）：胶囊里分组用，比加 margin 更精确。"""

    def __init__(self, parent: QWidget | None = None, *, height: int = 14) -> None:
        super().__init__(parent)
        self.setObjectName("VLine")
        self.setFixedSize(1, height)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class EditPanel(QFrame):
    """手动点选浮层容器：**自绘背景 + 顶部指向触发按钮的小三角**（合成为一个路径）。

    **为什么不交给 QSS**：QSS 只能画矩形边框，三角要么盖在面板上（看起来像贴纸），
    要么画到面板外（被窗口边界裁掉）。所以背景与边框都由 `paintEvent` 画，
    三角形与圆角矩形用 `united()` 合成一条路径 —— 两者之间自然没有接缝。
    QSS 只负责颜色（`QFrame#EditPanel` 设成透明无边框）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EditPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.arrow_x = 72.0
        """箭头尖端相对面板左边的 x；`<0` 表示不画。"""

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QPainterPath()
        body.addRoundedRect(
            QRectF(0.5, ARROW_HEIGHT + 0.5, self.width() - 1.0, self.height() - ARROW_HEIGHT - 1.0),
            8,
            8,
        )
        path = body
        if self.arrow_x >= 0:
            tip_x = max(
                ARROW_HALF_WIDTH + 1.0, min(self.arrow_x, self.width() - ARROW_HALF_WIDTH - 1.0)
            )
            arrow = QPainterPath()
            arrow.moveTo(QPointF(tip_x, 0.5))
            arrow.lineTo(QPointF(tip_x + ARROW_HALF_WIDTH, ARROW_HEIGHT + 1.0))
            arrow.lineTo(QPointF(tip_x - ARROW_HALF_WIDTH, ARROW_HEIGHT + 1.0))
            arrow.closeSubpath()
            path = body.united(arrow)
        painter.setBrush(theme.color("bg-elevated"))
        painter.setPen(QPen(theme.color("border-default"), 1))
        painter.drawPath(path)
        painter.end()


def panel_shadow(owner: QWidget, *, dark: bool):
    """浮层投影：唯一允许用投影的地方（它真的浮在界面之上）。"""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect

    effect = QGraphicsDropShadowEffect(owner)
    effect.setBlurRadius(24)
    effect.setOffset(0, 8)
    effect.setColor(QColor(0, 0, 0, 72 if dark else 36))
    return effect
