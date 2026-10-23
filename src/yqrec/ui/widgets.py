"""几个只有几十行、但必须自绘的小控件。

放这里而不是塞进 `main_window.py`：它们**没有业务逻辑**，只表达"长什么样"，
跟主窗的状态机完全解耦，单测也能单独抓。

- `StatusDot`：乐器标定状态点（8×8，成功/待办/未标定三态）；
- `VLine`：1px 竖分隔（试听胶囊里图标组与「停止」之间）；
- `EditPanel`：手动点选浮层（自己在顶部画一个指向触发按钮的小三角）；
- `PlaybackProgress`：试听进度条（细轨 + 已播填充 + 可拖把手），只发信号、不做业务。

颜色一律取 `theme` 令牌 —— 这几个控件都要跟着日/夜主题重绘。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget

from . import theme

DOT_PX = 8
"""状态点直径（px）。"""

ARROW_HEIGHT = 9
"""浮层顶部留给指向箭头的带高（px）：箭头画在面板内，永远不会被窗口边界裁掉。"""

ARROW_HALF_WIDTH = 7.0
"""箭头半宽（px）。"""

PROGRESS_HEIGHT = 18
"""试听进度条控件高度（px）：比 4px 轨道高得多，纯粹为了**好抓**。"""

PROGRESS_TRACK_PX = 4
"""轨道粗细（px）。"""

PROGRESS_HANDLE_R = 4.5
"""把手半径（px）。轨道两端各留这么多空白，0% / 100% 才都能真点到。"""


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


def format_time(ms: float) -> str:
    """毫秒 → `m:ss`（进度条 tooltip 用；一小时以上的谱面分钟数继续长）。"""
    total = max(0.0, float(ms)) / 1000.0
    return f"{int(total // 60)}:{int(total % 60):02d}"


class PlaybackProgress(QWidget):
    """试听进度条：细轨 + accent 已播填充 + 可拖把手（自绘，走 theme 令牌）。

    为什么不用 `QSlider`：滑块默认 18px 高、要额外接管 hide/show 的样式表细节，
    而这个条子只有 4px 轨道 + 一个点，QSS 画不出"悬停才放大"的把手；
    自绘还顺手解决了「点击轨道任意位置立刻跳过去」（QSlider 默认只支持拖动把手）。

    只发信号，**不碰播放器**（业务全在 `MainWindow`）：
    - `scrub_started()`：按下左键（调用方此时该暂停播放，别让 tick 跟自己抢位置）；
    - `scrubbed(ratio)`：拖动中（含按下瞬间），`0.0~1.0`；
    - `seek_released(ratio)`：松手，最终落点。

    不支持键盘焦点（`NoFocus`）：试听时焦点要留在谱面文本框里，高亮才跟得上。
    """

    scrub_started = Signal()
    scrubbed = Signal(float)
    seek_released = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PlayProgress")
        self.setFixedHeight(PROGRESS_HEIGHT)
        self.setMinimumWidth(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fraction = 0.0
        self._total_ms = 0.0
        self._head_ms = 0.0
        self._dragging = False
        self._hover = False
        self.setToolTip("拖动跳到任意位置")

    # -- 数据 -------------------------------------------------------------- #

    def set_progress(self, head_ms: float, total_ms: float) -> None:
        """更新播放头与总长（ms）。拖动中不更新显示，避免跟手打架。"""
        self._total_ms = max(0.0, float(total_ms))
        self._head_ms = max(0.0, float(head_ms))
        if not self._dragging:
            self._fraction = (
                0.0 if self._total_ms <= 0 else min(1.0, self._head_ms / self._total_ms)
            )
        self._sync_tooltip()
        self.update()

    def reset(self) -> None:
        """退出试听模式时清空（避免下次进来先闪一下上次的进度）。"""
        self._fraction = 0.0
        self._head_ms = 0.0
        self._total_ms = 0.0
        self._dragging = False
        self.setToolTip("拖动跳到任意位置")
        self.update()

    def fraction(self) -> float:
        return self._fraction

    def time_of(self, ratio: float) -> float:
        """比例 → 毫秒（调用方换算和弦下标用同一个口径，别各算各的）。"""
        return min(1.0, max(0.0, float(ratio))) * self._total_ms

    def refresh_style(self) -> None:
        """主题切换：颜色是绘制时现取的，`update()` 就够（保留接口给主窗统一调用）。"""
        self.update()

    # -- 几何 -------------------------------------------------------------- #

    def _ratio_at(self, x: float) -> float:
        span = max(1.0, float(self.width()) - 2 * PROGRESS_HANDLE_R)
        return min(1.0, max(0.0, (float(x) - PROGRESS_HANDLE_R) / span))

    def _x_of(self, ratio: float) -> float:
        span = max(1.0, float(self.width()) - 2 * PROGRESS_HANDLE_R)
        return PROGRESS_HANDLE_R + min(1.0, max(0.0, ratio)) * span

    def _sync_tooltip(self) -> None:
        if self._total_ms <= 0:
            self.setToolTip("拖动跳到任意位置")
        else:
            self.setToolTip(
                f"{format_time(self._head_ms)} / {format_time(self._total_ms)} — 拖动跳到任意位置"
            )

    # -- 交互 -------------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if event.button() != Qt.MouseButton.LeftButton or self._total_ms <= 0:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self._hover = True
        self.scrub_started.emit()  # 先让调用方暂停，再报位置
        self._emit(self._ratio_at(event.position().x()))
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if self._dragging:
            self._emit(self._ratio_at(event.position().x()))
            event.accept()
            return
        hover = True
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self._emit(self._ratio_at(event.position().x()))
        self.seek_released.emit(self._fraction)
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if self._hover and not self._dragging:
            self._hover = False
            self.update()
        super().leaveEvent(event)

    def _emit(self, ratio: float) -> None:
        self._fraction = ratio
        self._head_ms = ratio * self._total_ms
        self._sync_tooltip()
        self.update()
        self.scrubbed.emit(ratio)

    # -- 绘制 -------------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002 - Qt 签名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        width = float(self.width())
        center_y = self.height() / 2.0
        track_w = max(1.0, width - 2 * PROGRESS_HANDLE_R)
        top = center_y - PROGRESS_TRACK_PX / 2.0
        radius = PROGRESS_TRACK_PX / 2.0

        # 轨道
        painter.setBrush(theme.color("border-default"))
        painter.drawRoundedRect(
            QRectF(PROGRESS_HANDLE_R, top, track_w, PROGRESS_TRACK_PX), radius, radius
        )

        # 已播部分：宽度至少一个圆头，否则 1% 时看着像空白
        if self._fraction > 0 and self._total_ms > 0:
            head_x = self._x_of(self._fraction)
            filled = max(PROGRESS_TRACK_PX, head_x - PROGRESS_HANDLE_R)
            painter.setBrush(theme.color("accent" if self.isEnabled() else "text-disabled"))
            painter.drawRoundedRect(
                QRectF(PROGRESS_HANDLE_R, top, filled, PROGRESS_TRACK_PX), radius, radius
            )
        else:
            head_x = PROGRESS_HANDLE_R

        # 把手：悬停/拖动时放大一点点，暗示"能抓"
        grow = 1.0 if (self._hover or self._dragging) else 0.0
        radius_handle = PROGRESS_HANDLE_R + grow
        if self._total_ms <= 0:  # 没有总长 = 不可用，不给主色，免得看起来"能点"
            handle = theme.color("border-strong")
        elif self._dragging or self._hover:
            handle = theme.color("accent-hover")
        else:
            handle = theme.color("accent")
        painter.setPen(QPen(theme.color("bg-surface"), 2))  # 与 PlayBar 底色同色描边，压住轨道
        painter.setBrush(handle)
        painter.drawEllipse(QPointF(head_x, center_y), radius_handle, radius_handle)
        painter.end()


def panel_shadow(owner: QWidget, *, dark: bool):
    """浮层投影：唯一允许用投影的地方（它真的浮在界面之上）。"""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect

    effect = QGraphicsDropShadowEffect(owner)
    effect.setBlurRadius(24)
    effect.setOffset(0, 8)
    effect.setColor(QColor(0, 0, 0, 72 if dark else 36))
    return effect
