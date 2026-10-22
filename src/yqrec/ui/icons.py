"""运行时绘制的矢量图标：不依赖外部图片，打包零配置、任意 DPI 都清晰。

三组图标：
- `pin_icon(active)` —— 窗口置顶图钉；
- `media_icon(kind)` —— 试听控制条（快退/播放/暂停/快进）；
- `theme_toggle_icon()` —— 日/夜切换（日间画月亮，夜间画太阳）。

**颜色不再写死在模块级常量里**：全部走 `theme.media_colors()` / `theme.color()`，
否则日间主题下会变成"浅色图标画在白底上"（设计包 `04` §3.10 明确点出的坑）。
形状按 24×24 网格设计：钉帽（圆角横条）+ 钉身（梯形）+ 钉针（圆角细条），
一眼可辨为「图钉 = 钉在最上层」。
"""

from __future__ import annotations

from math import cos, radians, sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

_UNITS = 24.0
"""设计网格尺寸（绘制时统一缩放到目标像素）。"""

_SUPERSAMPLE = 4
"""超采样倍数：先按 4 倍画再交给 Qt 缩，边缘更干净。"""

_GLYPH_STROKE = 1.5
"""统一线宽（网格单位）：图钉/试听/日月三组图标共用一个粗细，视觉上才是一套。"""

_ROTATION_DEG = 45.0
"""整体斜置 45°：直上直下的图钉在 18px 下像字母 T，斜置后一眼是「别针/图钉」。"""

_SHAPE_SCALE = 0.86
"""斜置后外接框变大，缩一点留出视觉边距。"""


def _pin_path() -> QPainterPath:
    """24×24 网格上的图钉轮廓：宽钉帽 + 短钉身 + 细长钉针（18px 下仍能认出是图钉）。"""
    path = QPainterPath()
    path.addRoundedRect(QRectF(5.2, 2.4, 13.6, 5.2), 2.6, 2.6)  # 钉帽

    body = QPainterPath()
    body.moveTo(8.4, 7.6)
    body.lineTo(15.6, 7.6)
    body.lineTo(13.4, 13.0)
    body.lineTo(10.6, 13.0)
    body.closeSubpath()
    path.addPath(body)  # 钉身（收窄的锥体）

    path.addRoundedRect(QRectF(10.7, 12.6, 2.6, 9.0), 1.3, 1.3)  # 钉针
    return path


def _blank_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    """建一张透明画布并把画笔坐标系缩放到 24×24 网格（4× 超采样）。"""
    pixels = max(8, int(size)) * _SUPERSAMPLE
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(pixels / _UNITS, pixels / _UNITS)
    return pixmap, painter


def pin_icon(
    active: bool, *, size: int = 20, colors: tuple[str, str] | None = None
) -> QIcon:
    """窗口置顶图钉（`active=True` 亮起为 `accent-fg`）。

    `colors=(激活色, 普通色)` 由调用方按当前主题传入（默认取主题令牌）。
    """
    on, off = colors or (theme.token("accent-fg"), theme.token("text-secondary"))
    pixmap, painter = _blank_pixmap(size)
    try:
        painter.translate(_UNITS / 2.0, _UNITS / 2.0)
        painter.rotate(_ROTATION_DEG)
        painter.scale(_SHAPE_SCALE, _SHAPE_SCALE)
        painter.translate(-_UNITS / 2.0, -_UNITS / 2.0)
        painter.fillPath(_pin_path(), QColor(on if active else off))
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(_SUPERSAMPLE)
    return QIcon(pixmap)


# --------------------------------------------------------------------------- #
# 试听控制条（M2q.9）：快退 / 播放 / 暂停 / 快进
# --------------------------------------------------------------------------- #

_MEDIA_STROKE = _GLYPH_STROKE
"""描边宽度（网格单位）：给实心多边形加一圈同色描边，角会自然变圆。"""


def _play_path() -> QPainterPath:
    """播放：向右的实心三角（略微右移 0.6，视觉重心才居中）。"""
    path = QPainterPath()
    path.moveTo(8.6, 4.6)
    path.lineTo(19.4, 12.0)
    path.lineTo(8.6, 19.4)
    path.closeSubpath()
    return path


def _pause_path() -> QPainterPath:
    """暂停：两根圆角竖条（间距 4.6，与三角的视觉重量接近）。"""
    path = QPainterPath()
    path.addRoundedRect(QRectF(7.4, 4.8, 3.4, 14.4), 1.2, 1.2)
    path.addRoundedRect(QRectF(13.2, 4.8, 3.4, 14.4), 1.2, 1.2)
    return path


def _rewind_path() -> QPainterPath:
    """快退：两个向左的实心三角（⏪ 样式，与 iconfont 的 skip-backward 同构）。"""
    path = QPainterPath()
    path.moveTo(11.0, 4.8)
    path.lineTo(3.4, 12.0)
    path.lineTo(11.0, 19.2)
    path.closeSubpath()
    path.moveTo(19.6, 4.8)
    path.lineTo(12.0, 12.0)
    path.lineTo(19.6, 19.2)
    path.closeSubpath()
    return path


def _forward_path() -> QPainterPath:
    """快进：`_rewind_path()` 关于中线镜像。"""
    mirror = QPainterPath()
    mirror.moveTo(_UNITS - 11.0, 4.8)
    mirror.lineTo(_UNITS - 3.4, 12.0)
    mirror.lineTo(_UNITS - 11.0, 19.2)
    mirror.closeSubpath()
    mirror.moveTo(_UNITS - 19.6, 4.8)
    mirror.lineTo(_UNITS - 12.0, 12.0)
    mirror.lineTo(_UNITS - 19.6, 19.2)
    mirror.closeSubpath()
    return mirror


_MEDIA_PATHS = {
    "play": _play_path,
    "pause": _pause_path,
    "rewind": _rewind_path,
    "forward": _forward_path,
}


def _media_pixmap(kind: str, color: str, size: int) -> QPixmap:
    pixmap, painter = _blank_pixmap(size)
    try:
        pen = QPen(QColor(color), _MEDIA_STROKE)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QColor(color))
        painter.drawPath(_MEDIA_PATHS[kind]())
    finally:
        painter.end()
    return pixmap


def media_icon(
    kind: str,
    *,
    size: int = 18,
    enabled: bool = True,
    colors: tuple[str, str] | None = None,
) -> QIcon:
    """试听控制条图标：`kind` = `play` / `pause` / `rewind` / `forward`。

    自带 `Disabled` 版本（灰一档），这样按钮 `setEnabled(False)` 时不用换图标，
    Qt 会自动切到禁用配色 —— 和 `pin_icon` 一样是运行时绘制，无外部资源。
    `colors=(可用色, 禁用色)` 默认取当前主题，切主题后要重新生成（见 `refresh_theme`）。
    """
    if kind not in _MEDIA_PATHS:
        raise ValueError(f"未知图标: {kind}")
    on, off = colors or theme.media_colors()
    icon = QIcon()
    color = on if enabled else off
    icon.addPixmap(_media_pixmap(kind, color, size), QIcon.Mode.Normal)
    icon.addPixmap(_media_pixmap(kind, off, size), QIcon.Mode.Disabled)
    return icon


# --------------------------------------------------------------------------- #
# 日/夜切换：日间画月亮（点它去夜间），夜间画太阳
# --------------------------------------------------------------------------- #
def _sun_path() -> tuple[QPainterPath, list[tuple[QPointF, QPointF]]]:
    """太阳：中心圆 + 8 条短射线（间隔 45°），射线与圆留 2px 呼吸。"""
    path = QPainterPath()
    path.addEllipse(QPointF(12.0, 12.0), 4.2, 4.2)
    rays: list[tuple[QPointF, QPointF]] = []
    for index in range(8):
        angle = index * 45.0
        dx, dy = cos(radians(angle)), sin(radians(angle))
        rays.append(
            (
                QPointF(12.0 + dx * 6.4, 12.0 + dy * 6.4),
                QPointF(12.0 + dx * 9.6, 12.0 + dy * 9.6),
            )
        )
    return path, rays


def _moon_path() -> QPainterPath:
    """月亮：大圆减去向右上偏移的小圆（substracted 得到新月）。"""
    outer = QPainterPath()
    outer.addEllipse(QPointF(11.4, 12.0), 7.4, 7.4)
    inner = QPainterPath()
    inner.addEllipse(QPointF(15.6, 9.0), 6.4, 6.4)
    return outer.subtracted(inner)


def theme_toggle_icon(*, size: int = 16) -> QIcon:
    """日/夜切换图标：当前是夜间 → 太阳（指向日间）；当前是日间 → 月亮。"""
    color = theme.token("text-secondary")
    pixmap, painter = _blank_pixmap(size)
    try:
        pen = QPen(QColor(color), _GLYPH_STROKE)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if theme.is_dark():
            body, rays = _sun_path()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(body)
            for start, end in rays:
                painter.drawLine(start, end)
        else:
            painter.setBrush(QColor(color))
            painter.drawPath(_moon_path())
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(_SUPERSAMPLE)
    return QIcon(pixmap)
