"""全屏半透明框选：拖拽出屏幕区域，Esc 取消。

坐标一律用**虚拟桌面绝对坐标**（多屏可能是负值），与 `ScreenTarget.rect` 一致，
因此选择结果可以直接交给 `capture/gdi_backend` / `dxgi_backend`。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme

MIN_SIZE = 8
"""小于该宽高的拖拽视为误触（取消）。"""

CORNER_LENGTH = 10
"""四角 L 形标记的臂长（像素）：细描边下靠它标出真实边界。"""


def virtual_desktop_rect() -> QRect:
    """所有屏幕的并集矩形（虚拟桌面坐标）。"""
    rect = QRect()
    for screen in QGuiApplication.screens():
        rect = rect.united(screen.geometry())
    return rect


class RegionPicker(QWidget):
    """覆盖整个虚拟桌面的框选层。"""

    region_selected = Signal(object)  # (left, top, width, height) 绝对屏幕坐标
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._cursor: QPoint | None = None
        """光标位置（画十字辅助线用；与是否正在拖拽无关）。"""
        self._base = QPoint(0, 0)
        self._aspect: float | None = None  # 16/9 锁长宽比；None 自由

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> None:
        """铺满虚拟桌面并置顶显示。"""
        rect = virtual_desktop_rect()
        self._base = rect.topLeft()
        self.setGeometry(rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    # -- 绘制 -------------------------------------------------------------- #

    def _selection(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def _constrain_aspect(self, origin: QPoint, current: QPoint) -> QPoint:
        """把 current 调整到使 origin→current 的矩形满足 `self._aspect` 长宽比。"""
        if self._aspect is None or self._aspect <= 0:
            return current
        dx = current.x() - origin.x()
        dy = current.y() - origin.y()
        raw_w = abs(dx)
        raw_h = abs(dy)
        if raw_w < 2 and raw_h < 2:
            return current
        if raw_w / max(1.0, raw_h) > self._aspect:
            new_h = raw_w / self._aspect
            cy = origin.y() + new_h * (1 if dy >= 0 else -1)
            return QPoint(current.x(), int(cy))
        new_w = max(1.0, raw_h) * self._aspect
        cx = origin.x() + new_w * (1 if dx >= 0 else -1)
        return QPoint(int(cx), current.y())

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002 - Qt 签名
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.overlay("scrim"))
        self._paint_crosshair(painter)

        selection = self._selection()
        if selection.isValid() and selection.width() > 0:
            painter.setBrush(theme.overlay("select-fill"))
            painter.setPen(QPen(theme.overlay("select-edge"), 1))
            painter.drawRect(selection)
            self._paint_corners(painter, selection)
            self._paint_size_label(painter, selection)
        else:
            painter.setPen(theme.overlay("select-label-text"))
            painter.setFont(QFont("Microsoft YaHei", 14))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "按住鼠标左键拖出要识别的区域（Esc 取消）",
            )
        painter.end()

    def _paint_crosshair(self, painter: QPainter) -> None:
        """从光标到屏幕四边的 1px 虚线：拖大区域时能对齐边界。"""
        if self._cursor is None:
            return
        pen = QPen(theme.overlay("crosshair"), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y = self._cursor.x(), self._cursor.y()
        painter.drawLine(self.rect().left(), y, self.rect().right(), y)
        painter.drawLine(x, self.rect().top(), x, self.rect().bottom())

    def _paint_corners(self, painter: QPainter, selection: QRect) -> None:
        """四角 L 形标记：1px 细描边下也能一眼看出选区边界。"""
        pen = QPen(theme.overlay("select-edge"), 2)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        left, top = selection.left(), selection.top()
        right, bottom = selection.right(), selection.bottom()
        arm = CORNER_LENGTH
        for x, y, dx, dy in (
            (left, top, 1, 1),
            (right, top, -1, 1),
            (left, bottom, 1, -1),
            (right, bottom, -1, -1),
        ):
            painter.drawLine(x, y, x + dx * arm, y)
            painter.drawLine(x, y, x, y + dy * arm)

    def _paint_size_label(self, painter: QPainter, selection: QRect) -> None:
        """尺寸标签：先铺圆角底再写字（亮画面上裸文字会糊掉）。"""
        text = (
            f"{selection.width()}x{selection.height()}  "
            f"@ {selection.left() + self._base.x()},{selection.top() + self._base.y()}"
        )
        painter.setFont(QFont("Consolas", 11))
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 16
        top = max(0, selection.top() - 26)
        box = QRectF(selection.left(), top, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.overlay("select-label-bg"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(theme.overlay("select-label-text"))
        painter.drawText(
            box.adjusted(8, 0, -8, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )

    # -- 交互 -------------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self._cursor = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        self._cursor = event.position().toPoint()
        if self._origin is not None:
            self._current = self._cursor
            if self._aspect is not None:
                self._current = self._constrain_aspect(self._origin, self._current)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._current = event.position().toPoint()
        if self._aspect is not None and self._origin is not None:
            self._current = self._constrain_aspect(self._origin, self._current)
        selection = self._selection()
        self._origin = None
        self._current = None
        if selection.width() < MIN_SIZE or selection.height() < MIN_SIZE:
            self.cancelled.emit()
            self.close()
            return
        self.region_selected.emit(
            (
                selection.left() + self._base.x(),
                selection.top() + self._base.y(),
                selection.width(),
                selection.height(),
            )
        )
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        super().keyPressEvent(event)
