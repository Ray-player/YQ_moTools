"""预览控件：等比缩放绘制实时帧 + 键位圈叠加 + **键位编辑模式**（拖拽/新增/删除）。

坐标约定：帧像素坐标 ↔ 控件坐标的换算集中在 `_image_rect()` 与 `to_frame()`，
叠加层与编辑层共用同一套映射，保证"看到哪就点到哪"。

两种叠加来源：
- 正常态：`set_grid(grid)` + `set_press(flags)`（识别出的 21 圈 + 按下高亮）；
- 编辑态：`enter_edit(points)` 后由本控件自己维护 `_edit_points`，支持拖拽/新增/删除，
  退出时 `edit_points()` 交回调用方落盘。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..types import (
    DEFAULT_RING_POINTS,
    DEFAULT_SQUARE_POINTS,
    DEFAULT_SQUARE_SIDE,
    KeyGrid,
)
from ..vision.pointsampler import base_points
from . import theme

PADDING = 8
"""预览区四周留白（像素）。"""

MARKER_SCALE = 1.6
"""圆点绘制半径相对 `KeyPoint.r` 的放大系数（原始半径画出来太小看不见）。"""

HIT_RADIUS = 14.0
"""编辑态命中半径（控件像素）：距点位该值以内视为点中该圈。"""

EDIT_MODE_DRAG = "drag"
EDIT_MODE_ADD = "add"
EDIT_MODE_DELETE = "delete"

_CALM_DOT_MIN_PX = 1.0
"""常态采样点最小半径：比原来的 1.8 更小，21 键 × 8 点才不糊成一片。"""

_STRONG_DOT_MIN_PX = 1.8
"""提亮态（学基线 / 悬停该键）采样点最小半径。"""


def _dot_radius(true_radius: float, strong: bool) -> float:
    """采样点半径：常态 0.07R，提亮 0.09R（都与真实键半径挂钩，缩放窗口不变形）。"""
    if strong:
        return max(_STRONG_DOT_MIN_PX, true_radius * 0.09)
    return max(_CALM_DOT_MIN_PX, true_radius * 0.07)


class PreviewWidget(QWidget):
    """实时预览 + 键位叠加 + 键位编辑。

    信号：
        points_changed: 编辑态点位发生增/删/拖后发一次（无参数）
        edit_hint: 需要提示给用户的一句话（如"已达上限 N 个"）
        selection_changed: 选中的点位序号（-1 = 未选中）
    """

    points_changed = Signal()
    edit_hint = Signal(str)
    selection_changed = Signal(int)
    background_clicked = Signal()
    """编辑态「拖动」模式下点了空白处：主窗据此等同「保存退出」（修"浮层关不掉"）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 窄长条布局（M2a.2）：预览降到键位状态下方，最小尺寸放宽以便窗口收窄
        # 最小高 150（原 180）：三段各加了 28px 区块头，放宽预览下限让整窗仍能缩到 700 高
        self.setMinimumSize(320, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._grid: KeyGrid | None = None
        self._press: list[bool] = []
        self._hint = ""
        # 学习基线期间的采样区域提示（蓝色半透明）
        self._overlay: list[tuple[float, float, float]] | None = None
        self._overlay_ring: float = 0.0
        self._overlay_side: float = 1.0
        """>0 表示画圆环（值为内径比例），0 表示画方块。"""
        self._overlay_dy: float = 0.0
        """采样区域的纵向偏移（帧像素）。"""
        self._overlay_points: bool = False
        """True = 点采样可视化（学基线时的参考圆，点本身由 `_paint_grid` 常显）。"""
        self._overlay_ring_points: int = DEFAULT_RING_POINTS
        self._overlay_square_points: int = DEFAULT_SQUARE_POINTS

        # 常显的采样口径：检测圈一出现就把基准点画成小红点（滑条改动即时同步）
        self._sample_inner: float = 0.0
        self._sample_side: float = DEFAULT_SQUARE_SIDE
        self._sample_ring_points: int = DEFAULT_RING_POINTS
        self._sample_square_points: int = DEFAULT_SQUARE_POINTS
        self._show_sample_points: bool = True

        # -- 编辑态 --
        self._editing = False
        self._edit_points: list[tuple[float, float]] = []
        self._edit_mode = EDIT_MODE_DRAG
        self._selected = -1
        self._drag_index = -1
        self._edit_limit = 1

        # -- 视觉态 --
        self._hover_key = -1
        """鼠标悬停的键位下标（-1 = 没悬停）：只有它和学基线时才提亮采样点。"""
        self._stopped = False
        """预览已停止（画面是最后一帧）：右上角标一个「已停止」。"""

    # -- 外部 API ---------------------------------------------------------- #

    def refresh_theme(self) -> None:
        """主题切换后重画（叠加层颜色不跟主题，但留白底与 header 胶囊跟）。"""
        self.update()

    def set_stopped(self, stopped: bool) -> None:
        if self._stopped == stopped:
            return
        self._stopped = bool(stopped)
        self.update()

    def set_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_grid(self, grid: KeyGrid | None) -> None:
        self._grid = grid
        self.update()

    def set_press(self, flags: list[bool] | None) -> None:
        self._press = list(flags or [])
        self.update()

    def set_sampling(
        self,
        *,
        inner_ratio: float = 0.0,
        square_side: float = DEFAULT_SQUARE_SIDE,
        ring_points: int = DEFAULT_RING_POINTS,
        square_points: int = DEFAULT_SQUARE_POINTS,
        show_points: bool = True,
    ) -> None:
        """设置当前采样口径 —— 检测圈显示时就把**基准点**画成小红点。

        `inner_ratio > 0` 画内环圆周上的点（默认 0.80R），否则画方块四边的点
        （半边长 = `square_side/2`，默认 0.55R）。拖「半径」滑条时主窗会立刻调这里，
        所以点的位置与真正采样的位置始终一致。
        """
        self._sample_inner = max(0.0, float(inner_ratio))
        self._sample_side = max(0.05, float(square_side))
        self._sample_ring_points = max(1, int(ring_points))
        self._sample_square_points = max(1, int(square_points))
        self._show_sample_points = bool(show_points)
        self.update()

    def has_frame(self) -> bool:
        return self._image is not None

    def frame_image(self) -> QImage | None:
        return self._image

    def frame_size(self) -> tuple[int, int]:
        if self._image is None:
            return (0, 0)
        return (self._image.width(), self._image.height())

    def show_sample_overlay(
        self,
        grid: KeyGrid | None,
        *,
        inner_ratio: float,
        square_side: float = DEFAULT_SQUARE_SIDE,
        offset_y: float = 0.0,
        point_mode: str = "",
        ring_points: int = 16,
        square_points: int = 8,
    ) -> None:
        """学习基线期间把"到底采了哪一块"标出来（蓝色半透明）。

        `inner_ratio > 0` 画圆环（环形采样），否则画边长 `square_side·R` 的方块。
        `point_mode="points"` 时改画分段点采样的基准点（小圆点）。
        `offset_y` 是**帧像素**偏移（1920 基准的换算由调用方做）。
        """
        if grid is None:
            self.clear_sample_overlay()
            return
        self._overlay = [(p.u, p.v, p.r) for p in grid.points]
        self._overlay_ring = max(0.0, float(inner_ratio))
        self._overlay_side = max(0.05, float(square_side))
        self._overlay_dy = float(offset_y)
        self._overlay_points = point_mode == "points"
        self._overlay_ring_points = max(1, int(ring_points))
        self._overlay_square_points = max(1, int(square_points))
        self.update()

    def clear_sample_overlay(self) -> None:
        if self._overlay is None:
            return
        self._overlay = None
        self.update()

    @property
    def has_sample_overlay(self) -> bool:
        return self._overlay is not None

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.update()

    def to_frame(self, x: float, y: float) -> tuple[float, float] | None:
        """控件坐标 → 帧像素坐标（超出画面返回 None）。"""
        if self._image is None:
            return None
        rect = self._image_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        fx = (x - rect.left()) / rect.width() * self._image.width()
        fy = (y - rect.top()) / rect.height() * self._image.height()
        if not (0.0 <= fx <= self._image.width() and 0.0 <= fy <= self._image.height()):
            return None
        return (fx, fy)

    # -- 编辑态 API -------------------------------------------------------- #

    @property
    def editing(self) -> bool:
        return self._editing

    @property
    def edit_mode(self) -> str:
        return self._edit_mode

    def enter_edit(
        self, points: Sequence[tuple[float, float]] = (), *, limit: int
    ) -> None:
        """进入编辑态：`points` 是帧像素坐标（可来自已检测的圈或预填候选）。

        `limit` 是检测圈上限（= 当前乐器的 `key_count`），由调用方传入。
        """
        self._edit_limit = max(1, int(limit))
        self._editing = True
        self._edit_points = [(float(x), float(y)) for x, y in points]
        self._edit_mode = EDIT_MODE_DRAG
        self._selected = -1
        self._drag_index = -1
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.selection_changed.emit(-1)
        self.update()

    def exit_edit(self) -> None:
        self._editing = False
        self._edit_points = []
        self._edit_mode = EDIT_MODE_DRAG
        self._selected = -1
        self._drag_index = -1
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.selection_changed.emit(-1)
        self.update()

    def set_edit_mode(self, mode: str) -> None:
        if mode not in (EDIT_MODE_DRAG, EDIT_MODE_ADD, EDIT_MODE_DELETE):
            mode = EDIT_MODE_DRAG
        self._edit_mode = mode
        if mode == EDIT_MODE_ADD:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == EDIT_MODE_DELETE:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def edit_points(self) -> list[tuple[float, float]]:
        return list(self._edit_points)

    # -- 绘制 -------------------------------------------------------------- #

    def _image_rect(self) -> QRectF:
        """等比缩放后的绘制区域（居中、留白）。"""
        area = QRectF(self.rect()).adjusted(PADDING, PADDING, -PADDING, -PADDING)
        if self._image is None or self._image.width() <= 0 or self._image.height() <= 0:
            return area
        scale = min(area.width() / self._image.width(), area.height() / self._image.height())
        width = self._image.width() * scale
        height = self._image.height() * scale
        left = area.left() + (area.width() - width) / 2.0
        top = area.top() + (area.height() - height) / 2.0
        return QRectF(left, top, width, height)

    def _to_widget(self, rect: QRectF, fx: float, fy: float) -> QPointF:
        width, height = self.frame_size()
        if width <= 0 or height <= 0:
            return QPointF(rect.center())
        return QPointF(
            rect.left() + fx / width * rect.width(), rect.top() + fy / height * rect.height()
        )

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002 - Qt 签名
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color("bg-input"))
        if self._image is None:
            painter.setPen(theme.color("text-tertiary"))
            painter.setFont(QFont("Microsoft YaHei", 11))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "尚未开始预览\n先点「框选区域」选择检测范围"
            )
            return

        rect = self._image_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, self._image)

        if self._editing:
            self._paint_edit(painter, rect)
        elif self._grid is not None:
            self._paint_grid(painter, rect)
        if self._overlay is not None:
            self._paint_sample_overlay(painter, rect)
        self._paint_header(painter, rect)
        self._paint_stopped_badge(painter, rect)

    def _paint_sample_overlay(self, painter: QPainter, rect: QRectF) -> None:
        """蓝色半透明色块：标出每个键实际参与判定的像素范围。"""
        if self._overlay is None:
            return
        height, width = self.frame_size()
        if width <= 0 or height <= 0:
            return
        fill = QColor(70, 150, 255, 70)
        edge = QColor(110, 180, 255, 200)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        dy = self._overlay_dy / height
        for u, v, r in self._overlay:
            center = QPointF(
                rect.left() + u * rect.width(), rect.top() + (v + dy) * rect.height()
            )
            radius = max(2.0, r * rect.width())
            if self._overlay_points:
                # 小红点已由 `_paint_grid` 常显；这里只加一条极淡的参考圆，
                # 让"学基线中"和"平时"在视觉上仍然可区分。
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if self._overlay_ring > 0:
                    painter.setPen(QPen(theme.overlay("guide"), 1.0))
                    painter.drawEllipse(
                        center, radius * self._overlay_ring, radius * self._overlay_ring
                    )
                continue
            if self._overlay_ring > 0:
                path = QPainterPath()
                path.setFillRule(Qt.FillRule.OddEvenFill)
                path.addEllipse(center, radius, radius)
                path.addEllipse(
                    center, radius * self._overlay_ring, radius * self._overlay_ring
                )
                painter.fillPath(path, fill)
                painter.setPen(QPen(edge, 1.0))
                painter.drawEllipse(center, radius, radius)
                painter.drawEllipse(
                    center, radius * self._overlay_ring, radius * self._overlay_ring
                )
            else:
                half = radius * self._overlay_side / 2.0
                box = QRectF(
                    center.x() - half,
                    center.y() - half,
                    half * 2,
                    half * 2,
                )
                painter.fillRect(box, fill)
                painter.setPen(QPen(edge, 1.0))
                painter.drawRect(box)

    def _paint_grid(self, painter: QPainter, rect: QRectF) -> None:
        assert self._grid is not None
        painter.setFont(QFont("Consolas", 10))
        learning = self._overlay is not None
        for index, point in enumerate(self._grid.points):
            center = QPointF(
                rect.left() + point.u * rect.width(), rect.top() + point.v * rect.height()
            )
            radius = max(5.0, point.r * rect.width() * MARKER_SCALE)
            true_radius = max(2.0, point.r * rect.width())
            pressed = index < len(self._press) and self._press[index]
            # 采样点常显是调参刚需：平时只降存在感，学基线/悬停该键时才提亮
            strong = learning or index == self._hover_key or pressed
            if self._show_sample_points:
                # 采样基准点画在**真实半径**上（圈是按 MARKER_SCALE 放大画的）
                dot = _dot_radius(true_radius, strong)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.overlay("dot-strong" if strong else "dot"))
                for px, py in base_points(
                    center.x(),
                    center.y(),
                    true_radius,
                    inner_ratio=self._sample_inner,
                    square_side=self._sample_side,
                    ring_points=self._sample_ring_points,
                    square_points=self._sample_square_points,
                ):
                    painter.drawEllipse(QPointF(px, py), dot, dot)
            if pressed:
                # 半透明填充：能透过圈看清原画面（用户要求再把绿底调淡一档），描边略加粗
                painter.setBrush(theme.overlay("press-fill"))
                painter.setPen(QPen(theme.overlay("press-edge"), 1.6))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(theme.overlay("ring-strong" if strong else "ring"), 1.0))
            painter.drawEllipse(center, radius, radius)
            painter.setPen(
                theme.overlay("ring-strong") if strong else QColor(220, 230, 240, 204)
            )
            painter.drawText(
                QRectF(center.x() - 10, center.y() - 7, 20, 14),
                Qt.AlignmentFlag.AlignCenter,
                str(index + 1),
            )

    def _paint_edit(self, painter: QPainter, rect: QRectF) -> None:
        painter.setFont(QFont("Consolas", 10))
        for index, (fx, fy) in enumerate(self._edit_points):
            center = self._to_widget(rect, fx, fy)
            if index == self._selected:
                painter.setBrush(theme.overlay("edit-sel-fill"))
                painter.setPen(QPen(theme.overlay("edit-sel-edge"), 2.0))
            else:
                painter.setBrush(theme.overlay("edit-fill"))
                painter.setPen(QPen(theme.overlay("edit-edge"), 1.6))
            painter.drawEllipse(center, 9.0, 9.0)
            painter.setPen(
                theme.overlay("edit-sel-edge")
                if index == self._selected
                else QColor(220, 235, 245)
            )
            painter.drawText(
                QRectF(center.x() + 10, center.y() - 8, 26, 14),
                Qt.AlignmentFlag.AlignLeft,
                str(index + 1),
            )

    def _header_text(self) -> str:
        """header 一行字：尺寸/缩放 + 编辑态进度 + 临时提示。"""
        assert self._image is not None
        rect = self._image_rect()
        scale = rect.width() / max(1, self._image.width())
        text = f"{self._image.width()}×{self._image.height()} · ×{scale:.2f}"
        if self._editing:
            text = f"{text} · 编辑 {len(self._edit_points)}/{self._edit_limit}"
        if self._hint:
            text = f"{text} · {self._hint}"
        return text

    def _paint_header(self, painter: QPainter, rect: QRectF) -> None:
        """左上角 header：深色胶囊 + 等宽小字。

        游戏画面亮度不可控 —— 雪地/花海场景里裸白字会直接消失，
        所以必须有半透明底（也是所有游戏 HUD 的通用做法）。
        """
        text = self._header_text()
        painter.setFont(QFont("Consolas", 9))
        metrics = painter.fontMetrics()
        width = min(rect.width() - 8, metrics.horizontalAdvance(text) + 16)
        box = QRectF(rect.left() + 6, rect.top() + 6, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.overlay("chip-bg"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(theme.overlay("chip-text"))
        painter.drawText(
            box.adjusted(8, 0, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(box.width()) - 14),
        )

    def _paint_stopped_badge(self, painter: QPainter, rect: QRectF) -> None:
        """右上角标「已停止」（只在预览停止后出现，避免被当成实时画面）。"""
        if not self._stopped:
            return
        painter.setFont(QFont("Microsoft YaHei", 9))
        text = "已停止"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 16
        box = QRectF(rect.right() - width - 6, rect.top() + 6, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.overlay("chip-bg"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(theme.color("text-tertiary"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # -- 交互 -------------------------------------------------------------- #

    def _key_at(self, x: float, y: float) -> int:
        """鼠标点落在哪个检测圈里（用于提亮采样点；只在非编辑态用）。"""
        if self._grid is None:
            return -1
        rect = self._image_rect()
        if rect.width() <= 0:
            return -1
        for index, point in enumerate(self._grid.points):
            center = QPointF(
                rect.left() + point.u * rect.width(), rect.top() + point.v * rect.height()
            )
            radius = max(6.0, point.r * rect.width() * MARKER_SCALE)
            if (center.x() - x) ** 2 + (center.y() - y) ** 2 <= radius * radius:
                return index
        return -1

    # -- 交互（仅编辑态响应） ---------------------------------------------- #

    def _hit_test(self, x: float, y: float) -> int:
        rect = self._image_rect()
        if rect.width() <= 0:
            return -1
        best = -1
        best_distance = HIT_RADIUS
        for index, (fx, fy) in enumerate(self._edit_points):
            center = self._to_widget(rect, fx, fy)
            distance = ((center.x() - x) ** 2 + (center.y() - y) ** 2) ** 0.5
            if distance <= best_distance:
                best, best_distance = index, distance
        return best

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if not self._editing or event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        hit = self._hit_test(pos.x(), pos.y())

        if self._edit_mode == EDIT_MODE_DELETE:
            if hit < 0:
                self.edit_hint.emit("请先点击要删除的检测圈")
                return
            self._edit_points.pop(hit)
            self._selected = -1
            self.selection_changed.emit(-1)
            self.points_changed.emit()
            self.update()
            return

        if self._edit_mode == EDIT_MODE_ADD:
            frame_point = self.to_frame(pos.x(), pos.y())
            if frame_point is None:
                self.edit_hint.emit("请点画面内部新增检测圈")
                return
            if len(self._edit_points) >= self._edit_limit:
                self.edit_hint.emit(
                    f"检测圈已达上限 {self._edit_limit} 个，无法继续新增"
                )
                return
            self._edit_points.append(frame_point)
            self._selected = len(self._edit_points) - 1
            self.selection_changed.emit(self._selected)
            # 保持新增模式：用户可连续点击添加多个圈（与删除模式一致）
            self.points_changed.emit()
            self.update()
            return

        # 拖拽模式：点中则选中并可拖动
        if hit >= 0:
            self._selected = hit
            self._drag_index = hit
            self.selection_changed.emit(hit)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            return
        # 点空白 = 结束编辑（主窗按"保存退出"处理，圈数不够只会提示）
        self.background_clicked.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if not self._editing:
            # 非编辑态：只有"鼠标停在这个键上"才提亮它的采样点（否则 21×8 个点太抢眼）
            pos = event.position()
            hover = self._key_at(pos.x(), pos.y())
            if hover != self._hover_key:
                self._hover_key = hover
                self.update()
            return
        if self._drag_index < 0:
            return
        pos = event.position()
        frame_point = self.to_frame(pos.x(), pos.y())
        if frame_point is None:
            return
        self._edit_points[self._drag_index] = frame_point
        self.update()  # Qt 会自动合并重绘请求（相当于限频）

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if self._hover_key >= 0:
            self._hover_key = -1
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if not self._editing or event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_index >= 0:
            self._drag_index = -1
            self.setCursor(
                Qt.CursorShape.CrossCursor
                if self._edit_mode == EDIT_MODE_ADD
                else Qt.CursorShape.ArrowCursor
            )
            self.points_changed.emit()
            self.update()
