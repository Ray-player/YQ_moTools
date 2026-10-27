"""介绍视频的画面渲染：纯 QPainter 绘制，1920×1080 / 60 fps。

设计基调
--------
* 底色 `#0A0D11`，左上主色光斑 + 右下绿色光斑，40px 细网格；
* 只用一个主色（accent 蓝）+ 一个状态色（success 绿），其余靠明度分层；
* 所有出现动画 = `ease_out_cubic` 位移 24~40px + 透明度，误差 < 0.5px；
* 场景之间交叉叠化（timeline.CROSS），不硬切。
"""

from __future__ import annotations

import math
import os
import pathlib

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

import timeline as TL

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHOTS = ROOT / "out" / "intro_video" / "shots"
W, H = 1920, 1080

# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #
BG = QColor("#0B0F14")
T1 = QColor("#EAF0F7")
T2 = QColor("#A3B3C5")
T3 = QColor("#6E7D8D")
ACC = QColor("#2A66F0")
ACC_L = QColor("#4B83F0")
ACC_T = QColor("#7FAAFF")
GREEN = QColor("#2ED47F")
AMBER = QColor("#F0A93B")
DANGER = QColor("#FF6B6B")

FAMILY = "Microsoft YaHei UI"
MONO = "Consolas"

_FONTS_READY = False


def init_fonts() -> None:
    """把系统字体显式注册到 Qt（offscreen 平台下不注册就只有豆腐块）。"""
    global FAMILY, MONO, _FONTS_READY
    if _FONTS_READY:
        return
    windir = pathlib.Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    loaded: list[str] = []
    for name in ("msyh.ttc", "msyhbd.ttc", "consola.ttf", "consolab.ttf"):
        path = windir / name
        if path.exists():
            fid = QFontDatabase.addApplicationFont(str(path))
            if fid >= 0:
                loaded.extend(QFontDatabase.applicationFontFamilies(fid))
    for candidate in ("Microsoft YaHei UI", "Microsoft YaHei"):
        if candidate in loaded:
            FAMILY = candidate
            break
    MONO = "Consolas" if "Consolas" in loaded else "Courier New"
    _FONTS_READY = True


def col(c: QColor, alpha: float = 1.0) -> QColor:
    """按 alpha（0~1）缩放颜色的**原有**透明度。"""
    out = QColor(c)
    a = alpha if alpha <= 1.0 else alpha / 255.0
    out.setAlphaF(max(0.0, min(1.0, out.alphaF() * a)))
    return out


def mix(a: QColor, b: QColor, k: float) -> QColor:
    k = max(0.0, min(1.0, k))
    return QColor(
        int(a.red() + (b.red() - a.red()) * k),
        int(a.green() + (b.green() - a.green()) * k),
        int(a.blue() + (b.blue() - a.blue()) * k),
    )


# --------------------------------------------------------------------------- #
# 缓动
# --------------------------------------------------------------------------- #
def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def prog(t: float, start: float, dur: float) -> float:
    if dur <= 0:
        return 1.0 if t >= start else 0.0
    return clamp01((t - start) / dur)


def ease_out(k: float) -> float:
    return 1.0 - (1.0 - k) ** 3


def ease_in_out(k: float) -> float:
    return 4 * k**3 if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2


def ease_out_back(k: float, s: float = 1.34) -> float:
    k -= 1.0
    return 1.0 + k * k * ((s + 1) * k + s)


def stage(t: float, start: float, dur: float, dy: float = 26.0) -> tuple[float, float]:
    """返回 (alpha, 垂直偏移)。"""
    k = prog(t, start, dur)
    e = ease_out(k)
    return e, (1.0 - e) * dy


# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #
def font(size: float, weight: QFont.Weight = QFont.Weight.Normal,
         *, spacing: float = 0.0, mono: bool = False) -> QFont:
    f = QFont(MONO if mono else FAMILY)
    f.setPixelSize(int(round(size)))
    f.setWeight(weight)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


# --------------------------------------------------------------------------- #
# 绘制原语
# --------------------------------------------------------------------------- #
def text(
    p: QPainter,
    x: float,
    y: float,
    s: str,
    size: float,
    color: QColor = T1,
    *,
    weight: QFont.Weight = QFont.Weight.Normal,
    alpha: float = 1.0,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    spacing: float = 0.0,
    mono: bool = False,
    box_w: float = 0.0,
) -> QRectF:
    if alpha <= 0.003:
        return QRectF(x, y, 0, 0)
    f = font(size, weight, spacing=spacing, mono=mono)
    p.setFont(f)
    p.setPen(col(color, alpha))
    fm = QFontMetricsF(f)
    w = box_w or (fm.horizontalAdvance(s) + 4)
    left = x
    if align & Qt.AlignmentFlag.AlignHCenter:
        left = x - w / 2
    elif align & Qt.AlignmentFlag.AlignRight:
        left = x - w
    rect = QRectF(left, y - fm.height() / 2, w, fm.height())
    p.drawText(rect, align, s)
    return rect


def text_w(s: str, size: float, *, weight: QFont.Weight = QFont.Weight.Normal,
           spacing: float = 0.0, mono: bool = False) -> float:
    return QFontMetricsF(font(size, weight, spacing=spacing, mono=mono)).horizontalAdvance(s)


def card(
    p: QPainter,
    rect: QRectF,
    radius: float,
    *,
    fill: QColor | None = None,
    stroke: QColor | None = None,
    stroke_w: float = 1.0,
    alpha: float = 1.0,
    shadow: float = 0.0,
) -> None:
    if alpha <= 0.003:
        return
    if shadow > 0:
        for i in range(3):
            grow = shadow * (i + 1) / 2.2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col(QColor("#000000"), alpha * 0.16 / (i + 1)))
            p.drawRoundedRect(rect.adjusted(-grow, -grow + 6, grow, grow + 10), radius + grow, radius + grow)
    p.setPen(QPen(col(stroke or QColor(255, 255, 255, 20), alpha), stroke_w) if stroke else Qt.PenStyle.NoPen)
    p.setBrush(col(fill or QColor(255, 255, 255, 12), alpha))
    p.drawRoundedRect(rect, radius, radius)


def pill(
    p: QPainter,
    x: float,
    y: float,
    label: str,
    *,
    size: float = 18,
    pad_x: float = 14,
    h: float = 34,
    fill: QColor | None = None,
    stroke: QColor | None = None,
    color: QColor = T2,
    alpha: float = 1.0,
    weight: QFont.Weight = QFont.Weight.Medium,
) -> float:
    w = text_w(label, size, weight=weight) + pad_x * 2
    rect = QRectF(x, y - h / 2, w, h)
    card(p, rect, h / 2, fill=fill or QColor(255, 255, 255, 10),
         stroke=stroke or QColor(255, 255, 255, 22), alpha=alpha)
    text(p, x + pad_x, y, label, size, color, weight=weight, alpha=alpha)
    return w


def dot(p: QPainter, cx: float, cy: float, r: float, color: QColor, alpha: float = 1.0) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col(color, alpha))
    p.drawEllipse(QPointF(cx, cy), r, r)


def glow(p: QPainter, cx: float, cy: float, r: float, color: QColor, alpha: float) -> None:
    if alpha <= 0.003:
        return
    g = QRadialGradient(QPointF(cx, cy), r)
    g.setColorAt(0.0, col(color, alpha))
    g.setColorAt(0.55, col(color, alpha * 0.28))
    g.setColorAt(1.0, col(color, 0.0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(QPointF(cx, cy), r, r)


def ring(p: QPainter, cx: float, cy: float, r: float, color: QColor, alpha: float,
         width: float = 2.0) -> None:
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(col(color, alpha), width))
    p.drawEllipse(QPointF(cx, cy), r, r)


def vgrad(p: QPainter, rect: QRectF, top: QColor, bottom: QColor, alpha: float = 1.0,
          radius: float = 0.0) -> None:
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0.0, col(top, alpha))
    g.setColorAt(1.0, col(bottom, alpha))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    if radius:
        p.drawRoundedRect(rect, radius, radius)
    else:
        p.drawRect(rect)


def hline(p: QPainter, x1: float, x2: float, y: float, color: QColor, alpha: float,
          width: float = 1.0) -> None:
    p.setPen(QPen(col(color, alpha), width))
    p.drawLine(QPointF(x1, y), QPointF(x2, y))


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #
class Assets:
    """一次性载入所有位图（缩放好，避免逐帧重采样）。"""

    def __init__(self) -> None:
        self.logo = QPixmap(str(ROOT / "assets" / "icons" / "mo_icon.png"))
        self.logo_round = self._round(self.logo, 260, 56)
        self.logo_small = self._round(self.logo, 132, 30)
        self.ui_dark = QPixmap(str(SHOTS / "ui_dark.png"))
        self.ui_light = QPixmap(str(SHOTS / "ui_light.png"))
        self.game = QPixmap(str(SHOTS / "_crop_a.png"))

    @staticmethod
    def _round(pix: QPixmap, size: int, radius: int) -> QPixmap:
        src = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        out = QPixmap(src.size())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(out.rect()), radius, radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, src)
        p.end()
        return out


ASSETS: Assets | None = None


def assets() -> Assets:
    global ASSETS
    if ASSETS is None:
        ASSETS = Assets()
    return ASSETS


# --------------------------------------------------------------------------- #
# 背景
# --------------------------------------------------------------------------- #
def draw_bg(p: QPainter, t: float, *, warm: float = 0.0) -> None:
    p.fillRect(QRectF(0, 0, W, H), BG)

    drift = math.sin(t * 0.16) * 60
    glow(p, 380 + drift, 210 + math.cos(t * 0.13) * 40, 920, ACC, 0.175 + warm * 0.05)
    glow(p, 1620 - drift * 0.6, 900 - math.cos(t * 0.11) * 40, 780, GREEN, 0.055 + warm * 0.02)

    # 细网格
    p.setPen(QPen(QColor(255, 255, 255, 5), 1))
    step = 48
    off = (t * 5.0) % step
    for x in range(-step, W + step, step):
        p.drawLine(QPointF(x + off * 0.35, 0), QPointF(x + off * 0.35, H))
    for y in range(-step, H + step, step):
        p.drawLine(QPointF(0, y + off * 0.55), QPointF(W, y + off * 0.55))

    # 斜向光带
    for index, (offset, width, alpha) in enumerate(((0.30, 380, 0.016), (0.62, 220, 0.011))):
        x = (offset * W + t * 26 * (1 + index)) % (W + 700) - 350
        g = QLinearGradient(QPointF(x, 0), QPointF(x + width, H))
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.5, QColor(160, 200, 255, int(255 * alpha)))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        band = QPainterPath()
        band.moveTo(x, 0)
        band.lineTo(x + width, 0)
        band.lineTo(x + width - 260, H)
        band.lineTo(x - 260, H)
        band.closeSubpath()
        p.drawPath(band)

    # 暗角
    vg = QRadialGradient(QPointF(W / 2, H / 2), W * 0.72)
    vg.setColorAt(0.55, QColor(0, 0, 0, 0))
    vg.setColorAt(1.0, QColor(0, 0, 0, 150))
    p.setBrush(vg)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(QRectF(0, 0, W, H))


def draw_chrome(p: QPainter, t: float, chapter: str) -> None:
    """左下品牌水印 + 右下章节标签（极轻，不抢画面）。"""
    a = clamp01((t - 3.0) / 1.2) * 0.7
    if a > 0:
        text(p, 78, H - 56, "YQ_moTools", 17, T3, alpha=a, weight=QFont.Weight.DemiBold, spacing=0.6)
        text(p, 78 + text_w("YQ_moTools", 17, weight=QFont.Weight.DemiBold, spacing=0.6) + 12,
             H - 56, "v0.1.6", 15, T3, alpha=a * 0.8, mono=True)
    if chapter and t > 1.0:
        text(p, W - 78, H - 56, chapter, 15, T3, alpha=0.55, align=
             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, spacing=2.4)
    # 右上角章节进度点（7 段）
    if t > 0.8:
        cur = chapter[:2]
        for index in range(7):
            on = f"{index + 1:02d}" == cur
            cx = W - 78 - (6 - index) * 17
            dot(p, cx, 62, 3.4 if on else 2.6, ACC_L if on else QColor(255, 255, 255, 90),
                (0.95 if on else 0.45) * clamp01((t - 0.8) / 0.8))


def chapter_label(t: float) -> str:
    for start, stop, label in (
        (TL.S_PROBLEM[0], TL.S_PROBLEM[1], "01 · 问题"),
        (TL.S_STEPS[0], TL.S_STEPS[1], "02 · 流程"),
        (TL.S_DETECT[0], TL.S_DETECT[1], "03 · 识别"),
        (TL.S_UI[0], TL.S_UI[1], "04 · 界面"),
        (TL.S_PLAY[0], TL.S_PLAY[1], "05 · 演奏"),
        (TL.S_EXPORT[0], TL.S_EXPORT[1], "06 · 导出"),
        (TL.S_OUTRO[0], TL.S_OUTRO[1], "07 · 开始"),
    ):
        if start <= t < stop:
            return label
    return ""


# --------------------------------------------------------------------------- #
# S0 开场
# --------------------------------------------------------------------------- #
def scene_intro(p: QPainter, t: float) -> None:
    a = assets()
    cx = W / 2

    logo_a = ease_out(prog(t, 0.25, 0.9))
    scale = 0.86 + 0.14 * ease_out_back(prog(t, 0.25, 1.0), 1.1)
    breathe = 1.0 + 0.012 * math.sin(t * 2.1)
    size = 238 * scale * breathe
    glow(p, cx, 398, 420, ACC, 0.20 * logo_a)
    if logo_a > 0:
        pix = a.logo_round
        p.setOpacity(logo_a)
        p.drawPixmap(
            QRectF(cx - size / 2, 398 - size / 2, size, size), pix,
            QRectF(pix.rect()),
        )
        p.setOpacity(1.0)

    # 标题
    al, dy = stage(t, 0.95, 0.85, 30)
    if al > 0:
        f = font(70, QFont.Weight.Bold, spacing=1.5)
        p.setFont(f)
        p.setPen(col(T1, al))
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(TL.BRAND)
        p.drawText(QRectF(cx - tw / 2, 600 + dy - fm.height() / 2, tw, fm.height()),
                   int(Qt.AlignmentFlag.AlignCenter), TL.BRAND)

    al2, dy2 = stage(t, 1.35, 0.8, 22)
    if al2 > 0:
        text(p, cx, 672 + dy2, TL.TAGLINE, 27, T2, alpha=al2 * 0.95,
             align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
             spacing=3.0, box_w=900)

    # 三个能力胶囊
    labels = TL.INTRO_CAPS
    widths = [text_w(s, 20) + 40 for s in labels]
    total = sum(widths) + 16 * (len(labels) - 1)
    x = cx - total / 2
    for index, label in enumerate(labels):
        ai, dyi = stage(t, 2.35 + index * 0.22, 0.7, 18)
        pill(p, x, 768 + dyi, label, size=20, h=44, pad_x=20, alpha=ai,
             fill=QColor(255, 255, 255, 10), stroke=QColor(255, 255, 255, 24), color=T1)
        x += widths[index] + 16

    # 底部细线生长
    lw = 240 * ease_in_out(prog(t, 2.9, 1.4))
    if lw > 1:
        g = QLinearGradient(QPointF(cx - lw, 0), QPointF(cx + lw, 0))
        g.setColorAt(0.0, col(ACC, 0.0))
        g.setColorAt(0.5, col(ACC_L, 0.55))
        g.setColorAt(1.0, col(ACC, 0.0))
        p.setPen(QPen(g, 1.2))
        p.drawLine(QPointF(cx - lw, 846), QPointF(cx + lw, 846))


# --------------------------------------------------------------------------- #
# S1 问题
# --------------------------------------------------------------------------- #
def scene_problem(p: QPainter, t: float) -> None:
    cx = W / 2

    al, dy = stage(t, 0.15, 0.8, 24)
    if al > 0:
        label = "问 题"
        lw = text_w(label, 16, spacing=6)
        text(p, cx - lw / 2, 328 + dy, label, 16, ACC_T, alpha=al * 0.9, spacing=6)

    al, dy = stage(t, 0.35, 0.9, 30)
    if al > 0:
        f = font(74, QFont.Weight.Bold, spacing=1.0)
        p.setFont(f)
        p.setPen(col(T1, al))
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(TL.PROBLEM_TITLE)
        p.drawText(QRectF(cx - tw / 2, 420 + dy - fm.height() / 2, tw, fm.height()),
                   int(Qt.AlignmentFlag.AlignCenter), TL.PROBLEM_TITLE)
        # 标题下方一抹红色强调
        uw = 120 * ease_in_out(prog(t, 0.9, 0.7))
        if uw > 1:
            p.setPen(QPen(col(DANGER, al * 0.55), 3))
            p.drawLine(QPointF(cx - uw / 2, 474), QPointF(cx + uw / 2, 474))

    line1 = TL.PROBLEM_SUB
    line2 = TL.PROBLEM_SUB2
    al, dy = stage(t, 1.35, 0.8, 20)
    if al > 0:
        text(p, cx, 536 + dy, line1, 25, T2, alpha=al * 0.9,
             align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, box_w=1500)
    al, dy = stage(t, 1.75, 0.8, 20)
    if al > 0:
        text(p, cx, 592 + dy, line2, 32, T1, alpha=al * 0.96,
             align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, box_w=1500,
             weight=QFont.Weight.DemiBold)

    # 三张否定卡
    labels = TL.PROBLEM_CARDS
    cw, ch, gap = 384, 116, 34
    total = cw * 3 + gap * 2
    x0 = cx - total / 2
    for index, label in enumerate(labels):
        ai, dyi = stage(t, 2.9 + index * 0.3, 0.75, 26)
        if ai <= 0:
            continue
        rect = QRectF(x0 + index * (cw + gap), 690 + dyi, cw, ch)
        card(p, rect, 16, fill=QColor(255, 255, 255, 8), stroke=QColor(255, 255, 255, 16),
             alpha=ai)
        # ✕ + 文案整体水平居中
        tw_lab = text_w(label, 27, weight=QFont.Weight.Medium)
        total_w = 18 + 20 + tw_lab
        mx = rect.center().x() - total_w / 2 + 18
        my = rect.center().y()
        p.setPen(QPen(col(DANGER, ai * 0.8), 2.4))
        p.drawLine(QPointF(mx - 9, my - 9), QPointF(mx + 9, my + 9))
        p.drawLine(QPointF(mx + 9, my - 9), QPointF(mx - 9, my + 9))
        text(p, mx + 20, my, label, 27, QColor("#C6D2DF"), alpha=ai * 0.95, weight=QFont.Weight.Medium)


# --------------------------------------------------------------------------- #
# S2 三步
# --------------------------------------------------------------------------- #
def scene_steps(p: QPainter, t: float) -> None:
    cx = W / 2

    al, dy = stage(t, 0.1, 0.8, 24)
    if al > 0:
        label = "流 程"
        lw = text_w(label, 16, spacing=6)
        text(p, cx - lw / 2, 208 + dy, label, 16, ACC_T, alpha=al * 0.9, spacing=6)

    al, dy = stage(t, 0.25, 0.85, 26)
    if al > 0:
        f = font(56, QFont.Weight.Bold, spacing=1.0)
        p.setFont(f)
        p.setPen(col(T1, al))
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(TL.STEPS_TITLE)
        p.drawText(QRectF(cx - tw / 2, 280 + dy - fm.height() / 2, tw, fm.height()),
                   int(Qt.AlignmentFlag.AlignCenter), TL.STEPS_TITLE)

    # 三个节点
    nodes = TL.STEPS
    node_x = (cx - 560, cx, cx + 560)
    cy = 566
    lit = []
    for index, (num, title, desc) in enumerate(nodes):
        start = 1.0 + index * 0.75
        ai, dyi = stage(t, start, 0.7, 22)
        on = prog(t, start + 0.1, 0.5)
        lit.append(on)
        cy_i = cy + dyi
        # 圆
        r = 62
        ring(p, node_x[index], cy_i, r, QColor(255, 255, 255, 40), ai * 0.9, 1.4)
        if on > 0:
            glow(p, node_x[index], cy_i, 120, ACC, 0.22 * on)
            ring(p, node_x[index], cy_i, r, ACC_L, ai, 2.2)
            # ripple
            rp = prog(t, start + 0.3, 1.2)
            if rp < 1:
                ring(p, node_x[index], cy_i, r + 46 * ease_out(rp), ACC_L, (1 - rp) * 0.35 * ai, 1.6)
            p.setBrush(col(ACC, ai * 0.20))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(node_x[index], cy_i), r - 1, r - 1)
        text(p, node_x[index], cy_i, num, 34, ACC_T if on > 0.3 else T2, alpha=ai,
             weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter, mono=True,
             box_w=140)
        # 文案
        text(p, node_x[index], cy_i + 108, title, 30, T1, alpha=ai,
             weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignHCenter, box_w=520)
        text(p, node_x[index], cy_i + 152, desc, 20, T2, alpha=ai * 0.95,
             align=Qt.AlignmentFlag.AlignHCenter, box_w=520)

    # 连接线（依次生长）
    for index in range(2):
        k = ease_in_out(prog(t, 1.75 + index * 0.75, 0.75))
        if k <= 0:
            continue
        x1 = node_x[index] + 78
        x2 = node_x[index + 1] - 78
        xe = x1 + (x2 - x1) * k
        g = QLinearGradient(QPointF(x1, 0), QPointF(x2, 0))
        g.setColorAt(0.0, col(ACC_L, 0.55))
        g.setColorAt(1.0, col(GREEN, 0.35))
        p.setPen(QPen(g, 2.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(x1, cy), QPointF(xe, cy))
        # 线上的流动光点
        if k > 0.6:
            for n in range(2):
                ft = ((t * 0.55 + n * 0.5 + index * 0.2) % 1.0)
                px = x1 + (x2 - x1) * ft * k
                dot(p, px, cy, 3.2, ACC_T, 0.85 * (1 - abs(ft - 0.5) * 1.4))

    # 数据流小标签
    words = TL.FLOW_WORDS
    y = 852
    widths = [text_w(w, 19) + 40 for w in words]
    arrows = 34
    total = sum(widths) + arrows * (len(words) - 1)
    x = cx - total / 2
    for index, word in enumerate(words):
        on = prog(t, 3.9 + index * 0.32, 0.55)
        ai = on
        rect = QRectF(x, y - 26, widths[index], 52)
        card(p, rect, 26, fill=QColor(255, 255, 255, 9), stroke=QColor(255, 255, 255, 16),
             alpha=ai)
        if on > 0.05:
            glow(p, rect.center().x(), rect.center().y(), 90, ACC, 0.12 * on)
        text(p, x + 20, y, word, 19, T1 if on > 0.5 else T2, alpha=max(ai, 0.12),
             mono=word.isascii() and "RGB" in word)
        x += widths[index]
        if index < len(words) - 1:
            ax = x + 6
            aa = prog(t, 3.9 + index * 0.32 + 0.2, 0.5)
            if aa > 0:
                p.setPen(QPen(col(T3, aa * 0.9), 1.6))
                p.drawLine(QPointF(ax, y), QPointF(ax + 18, y))
                p.drawLine(QPointF(ax + 12, y - 5), QPointF(ax + 18, y))
                p.drawLine(QPointF(ax + 12, y + 5), QPointF(ax + 18, y))
            x += arrows


# --------------------------------------------------------------------------- #
# S3 识别
# --------------------------------------------------------------------------- #
#: 采样圈位置 = 程序对 `tests/data/real/fwzs_1695x956.jpg` 跑 detect_keys_color
#: 检测出的真实圆心（相对裁剪图 1240×500），别再凭眼睛估：
#:   detect_keys_color → circle_map_note → 21 个圆心取行列均值
GAME_COLS = (0.1809, 0.2979, 0.4182, 0.5371, 0.6532, 0.7763, 0.8955)
GAME_ROWS = (0.3992, 0.6324, 0.8770)
GAME_R = 0.03844
"""键半径（相对裁剪图宽）：button_radius 54 @1920 → 47.7px / 1240。"""


def scene_detect(p: QPainter, t: float) -> None:
    a = assets()

    al, dy = stage(t, 0.1, 0.8, 22)
    if al > 0:
        text(p, 132, 150 + dy, "识 别", 16, ACC_T, alpha=al * 0.9, spacing=6)
        text(p, 132, 214 + dy, TL.DETECT_TITLE, 54, T1, alpha=al, weight=QFont.Weight.Bold)
        text(p, 132, 268 + dy, TL.DETECT_SUB, 23, T3, alpha=al * 1.0)

    # ---- 左：游戏画面 ----
    gw, gh = 800, 322
    gx, gy = 132.0, 372.0
    ga, gdy = stage(t, 0.45, 0.95, 40)
    if ga > 0:
        rect = QRectF(gx, gy + gdy, gw, gh)
        card(p, rect, 18, fill=QColor(20, 26, 34), stroke=QColor(255, 255, 255, 26), alpha=ga,
             shadow=18)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        p.save()
        p.setClipPath(path)
        p.setOpacity(ga)
        p.drawPixmap(rect.topLeft(), a.game.scaled(
            int(gw), int(gh), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        # 暗化，让叠加的采样点更清楚
        p.fillRect(rect, QColor(4, 8, 14, 96))
        p.restore()

        # 扫描亮线（循环）
        scan = (t * 0.42) % 1.0
        sy = rect.top() + rect.height() * scan
        sg = QLinearGradient(QPointF(rect.left(), sy - 40), QPointF(rect.left(), sy + 8))
        sg.setColorAt(0.0, QColor(90, 150, 255, 0))
        sg.setColorAt(0.8, QColor(120, 175, 255, 34))
        sg.setColorAt(1.0, QColor(190, 220, 255, 120))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(sg)
        p.save()
        p.setClipPath(path)
        p.drawRect(QRectF(rect.left(), sy - 40, rect.width(), 48))
        p.setPen(QPen(col(ACC_T, 0.55 * ga), 1.4))
        p.drawLine(QPointF(rect.left(), sy), QPointF(rect.right(), sy))
        p.restore()

        # 框选角标
        corner = 26
        p.setPen(QPen(col(ACC_T, 0.9 * ga), 2.4))
        for cxp, cyp, dx, dy2 in (
            (rect.left(), rect.top(), 1, 1),
            (rect.right(), rect.top(), -1, 1),
            (rect.left(), rect.bottom(), 1, -1),
            (rect.right(), rect.bottom(), -1, -1),
        ):
            p.drawLine(QPointF(cxp, cyp), QPointF(cxp + corner * dx, cyp))
            p.drawLine(QPointF(cxp, cyp), QPointF(cxp, cyp + corner * dy2))

        # 采样环 + 采样点（逐键出现）
        for index in range(21):
            start = 1.35 + index * 0.13
            on = prog(t, start, 0.4)
            if on <= 0:
                continue
            col_i, row_i = index % 7, index // 7
            kx = rect.left() + gw * GAME_COLS[col_i]
            ky = rect.top() + gh * GAME_ROWS[row_i]
            r = gw * GAME_R
            pulse = 0.55 + 0.45 * math.sin(t * 3.0 + index)
            ring(p, kx, ky, r, ACC_T, on * (0.45 + 0.35 * pulse) * ga, 1.6)
            for n in range(8):
                ang = math.pi * 2 * n / 8 + t * 0.6
                dot(p, kx + math.cos(ang) * r * 0.80, ky + math.sin(ang) * r * 0.80, 2.4,
                    GREEN, on * 0.85 * ga)

        # 标签（放卡片上方，别压住琴键）
        lab = "识别区域 · 1600 × 900 · 60 fps"
        wlab = text_w(lab, 15) + 24
        card(p, QRectF(rect.left(), rect.top() - 50, wlab, 32), 16,
             fill=QColor(255, 255, 255, 12), stroke=QColor(255, 255, 255, 30), alpha=ga * 0.95)
        text(p, rect.left() + 12, rect.top() - 34, lab, 15, ACC_T, alpha=ga * 0.95)

    # ---- 右：21 键灯板 ----
    bw, bh = 107, 86
    bxp, byp = 1000.0, 330.0
    gap_x, gap_y = 9, 9
    for index in range(21):
        r_i, c_i = divmod(index, 7)
        start = 0.9 + index * 0.045
        ai, ady = stage(t, start, 0.55, 18)
        if ai <= 0:
            continue
        rect = QRectF(bxp + c_i * (bw + gap_x), byp + ady + r_i * (bh + gap_y), bw, bh)
        delta = TL.KEY_DELTAS[index]
        hot = delta > 10.0
        jitter = (math.sin(t * 2.6 + index * 1.7) * 0.5 + 0.5) * 1.2
        shown = delta + jitter if hot else jitter * 0.8
        if hot:
            k = 0.55 + 0.45 * math.sin(t * 3.4 + index)
            card(p, rect, 12, fill=mix(QColor(46, 212, 127), QColor(20, 40, 32), 0.72),
                 stroke=mix(QColor(46, 212, 127), QColor(255, 255, 255), 0.15), alpha=ai,
                 stroke_w=1.4)
            glow(p, rect.center().x(), rect.center().y(), 110, GREEN, 0.16 * k * ai)
            text(p, rect.center().x(), rect.top() + 24, f"{TL.NOTE_ORDER[index]} · {TL.KEY_ORDER[index]}",
                 15, QColor("#0B1F15"), alpha=ai, weight=QFont.Weight.Bold,
                 align=Qt.AlignmentFlag.AlignCenter, box_w=bw)
            text(p, rect.center().x(), rect.top() + 52, f"Δ{shown:.0f}", 21, QColor("#06140C"),
                 alpha=ai, weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter,
                 box_w=bw, mono=True)
            text(p, rect.center().x(), rect.top() + 72, "PRESS", 11, QColor(6, 40, 24), alpha=ai * 0.75,
                 align=Qt.AlignmentFlag.AlignCenter, box_w=bw, spacing=2.0)
        else:
            card(p, rect, 12, fill=QColor(255, 255, 255, 10), stroke=QColor(255, 255, 255, 14),
                 alpha=ai)
            text(p, rect.center().x(), rect.top() + 26, f"{TL.NOTE_ORDER[index]} · {TL.KEY_ORDER[index]}",
                 15, T2, alpha=ai * 0.9, align=Qt.AlignmentFlag.AlignCenter, box_w=bw)
            text(p, rect.center().x(), rect.top() + 52, f"Δ{shown:.0f}", 21, T3,
                 alpha=ai * 0.85, align=Qt.AlignmentFlag.AlignCenter, box_w=bw, mono=True)
            text(p, rect.center().x(), rect.top() + 72, "IDLE", 11, T3, alpha=ai * 0.45,
                 align=Qt.AlignmentFlag.AlignCenter, box_w=bw, spacing=2.0)

    # ---- 底部：badge + 判据说明 ----
    bx = 132.0
    for index, badge in enumerate(TL.DETECT_BADGES):
        ai, ady = stage(t, 2.6 + index * 0.16, 0.6, 16)
        wb = pill(p, bx, 772 + ady, badge, size=18, alpha=ai,
                  fill=QColor(42, 102, 240, 34), stroke=QColor(75, 131, 240, 90), color=ACC_T)
        bx += wb + 12

    ai, ady = stage(t, 3.2, 0.8, 16)
    if ai > 0:
        text(p, 132, 848 + ady, TL.DETECT_NOTE, 21, T3, alpha=ai * 0.95)

    # 右侧纵向装饰：Δ 曲线
    _delta_curve(p, t)


def _delta_curve(p: QPainter, t: float) -> None:
    x0, x1, yc = 1000.0, 1810.0, 842.0
    amp = 44.0
    pts = []
    n = 120
    for i in range(n):
        k = i / (n - 1)
        x = x0 + (x1 - x0) * k
        phase = k * 9.0 - t * 1.6
        env = math.exp(-((k - 0.42) ** 2) / 0.06) * 1.0
        y = yc - amp * env * (0.65 + 0.35 * math.sin(phase * 3.1)) - 6 * math.sin(phase * 7)
        pts.append(QPointF(x, y))
    path = QPainterPath(pts[0])
    for pt in pts[1:]:
        path.lineTo(pt)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(col(ACC_L, 0.55), 1.8))
    p.drawPath(path)

    label = "Δ 判据量（逐键基线 → 阈值 20）"
    text(p, x0, yc + 54, label, 15, T3, alpha=0.7)
    # 阈值虚线
    p.setPen(QPen(col(AMBER, 0.45), 1.2, Qt.PenStyle.DashLine))
    p.drawLine(QPointF(x0, yc - 20), QPointF(x1, yc - 20))
    text(p, x1, yc - 20, "阈值", 14, AMBER, alpha=0.7,
         align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


# --------------------------------------------------------------------------- #
# S4 界面
# --------------------------------------------------------------------------- #
#: 截图（840×1200）内三块区域，比例坐标
UI_BLOCKS = (
    ((0.006, 0.120), (0.996, 0.384)),
    ((0.006, 0.424), (0.996, 0.686)),
    ((0.006, 0.730), (0.996, 0.952)),
)


def scene_ui(p: QPainter, t: float) -> None:
    a = assets()

    al, dy = stage(t, 0.1, 0.8, 22)
    if al > 0:
        text(p, 132, 168 + dy, "界 面", 16, ACC_T, alpha=al * 0.9, spacing=6)
        text(p, 132, 228 + dy, "认出来的音", 52, T1, alpha=al, weight=QFont.Weight.Bold)
        text(p, 132, 296 + dy, "直接落成谱", 52, T1, alpha=al, weight=QFont.Weight.Bold)

    # 左栏三条标注
    marks = (
        ("01", "键位状态", "每个键的 RGB 与 Δ 实时刷新，按下即刻反色"),
        ("02", "实时预览", "检测圈与采样点直接叠在画面上，看得见才敢信"),
        ("03", "录制内容", "tick 文本边弹边写；延音自动补 [毫秒]"),
    )
    focus_index = 0
    for index in range(len(marks)):
        if prog(t, 2.2 + index * 0.75 + 0.1, 0.55) > 0:
            focus_index = index
    for index, (num, title, desc) in enumerate(marks):
        start = 2.2 + index * 0.75
        ai, adyi = stage(t, start, 0.8, 22)
        if ai <= 0:
            continue
        # 只有"当前那条"满亮，其余降为淡底（避免三条一起抢焦点）
        active = prog(t, start + 0.1, 0.55) * (1.0 if index == focus_index else 0.34)
        y = 420 + index * 132 + adyi
        if active > 0.05:
            glow(p, 168, y + 4, 150, ACC, 0.10 * active)
            card(p, QRectF(120, y - 36, 640, 104), 14,
                 fill=QColor(42, 102, 240, 26), stroke=QColor(75, 131, 240, 80), alpha=active)
        text(p, 148, y - 4, num, 20, ACC_T if active > 0.4 else T3, alpha=ai,
             weight=QFont.Weight.Bold, mono=True)
        text(p, 200, y - 6, title, 28, T1, alpha=ai, weight=QFont.Weight.DemiBold)
        text(p, 200, y + 34, desc, 19, T3, alpha=ai * 0.95)

    al, dy = stage(t, 4.5, 0.8, 18)
    if al > 0:
        hline(p, 132, 700, 830 + dy, QColor(255, 255, 255, 22), al * 0.8)
        text(p, 132, 872 + dy, TL.UI_SUB, 21, T2, alpha=al * 0.95)

    # ---- 右侧：真实界面截图 ----
    pix = a.ui_dark
    scale = 0.88
    sh = 892.0
    sw = sh * pix.width() / pix.height()
    sx, sy = 1494.0, 104.0
    ap, adyp = stage(t, 0.35, 1.05, 60)
    if ap <= 0:
        return
    k = 0.94 + 0.06 * ease_out(prog(t, 0.35, 1.05))
    hh = sh * k
    ww = sw * k
    rect = QRectF(sx - ww / 2, sy + adyp + (sh - hh) / 2, ww, hh)
    card(p, rect.adjusted(-6, -6, 6, 6), 18, fill=QColor(16, 21, 28),
         stroke=QColor(255, 255, 255, 30), alpha=ap, shadow=26)
    p.setOpacity(ap)
    p.drawPixmap(rect, pix, QRectF(pix.rect()))
    p.setOpacity(1.0)

    # 三块区域高亮：框线逐条点亮，聚光只跟"最后一块"
    blocks: list[tuple[QRectF, float]] = []
    for index, (p0, p1) in enumerate(UI_BLOCKS):
        active = prog(t, 2.2 + index * 0.75 + 0.1, 0.55)
        if active <= 0.02:
            continue
        x1 = rect.left() + rect.width() * p0[0]
        y1 = rect.top() + rect.height() * p0[1]
        x2 = rect.left() + rect.width() * p1[0]
        y2 = rect.top() + rect.height() * p1[1]
        blocks.append((QRectF(x1, y1, x2 - x1, y2 - y1), active))

    if blocks:
        focus, focus_a = blocks[-1]
        mask = QPainterPath()
        mask.addRoundedRect(rect.adjusted(2, 2, -2, -2), 14, 14)
        hole = QPainterPath()
        hole.addRoundedRect(focus.adjusted(-3, -3, 3, 3), 12, 12)
        p.save()
        p.setClipPath(mask.subtracted(hole))
        p.fillRect(rect, QColor(4, 7, 11, int(255 * 0.32 * focus_a)))
        p.restore()

        for index, (r, active) in enumerate(blocks):
            p.setPen(QPen(col(ACC_L, active * 0.95), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 10, 10)
            if active > 0.9 and index == len(blocks) - 1:
                glow(p, r.center().x(), r.center().y(), r.width() * 0.55, ACC, 0.06 * active)
            bx, by = r.left() + 20, r.top() + 20
            dot(p, bx, by, 15, ACC, active)
            text(p, bx, by, f"{index + 1}", 15, QColor("#FFFFFF"), alpha=active,
                 weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter, box_w=30,
                 mono=True)

    # 顶部标签
    tag = "YQ_moTools · 深色主题"
    wt = text_w(tag, 15) + 30
    card(p, QRectF(sx - wt / 2, sy - 46, wt, 32), 16, fill=QColor(10, 14, 20),
         stroke=QColor(255, 255, 255, 26), alpha=ap * 0.95)
    text(p, sx - wt / 2 + 15, sy - 30, tag, 15, T2, alpha=ap * 0.95)


# --------------------------------------------------------------------------- #
# S5 试听 / 演奏
# --------------------------------------------------------------------------- #
def scene_play(p: QPainter, t: float) -> None:
    al, dy = stage(t, 0.1, 0.8, 22)
    if al > 0:
        text(p, 132, 156 + dy, "演 奏", 16, ACC_T, alpha=al * 0.9, spacing=6)
        text(p, 132, 222 + dy, TL.PLAY_TITLE, 54, T1, alpha=al, weight=QFont.Weight.Bold)
        text(p, 132, 276 + dy, TL.PLAY_SUB, 22, T3, alpha=al * 0.98)

    # ---- 播放器卡片 ----
    pa, pdy = stage(t, 0.55, 0.95, 40)
    if pa > 0:
        rect = QRectF(132, 352 + pdy, 900, 268)
        card(p, rect, 18, fill=QColor(255, 255, 255, 10), stroke=QColor(255, 255, 255, 22),
             alpha=pa, shadow=20)

        # 播放按钮
        bx, by = rect.left() + 56, rect.top() + 62
        glow(p, bx, by, 70, ACC, 0.22 * pa)
        dot(p, bx, by, 26, ACC, pa)
        # 三角形
        tri = QPainterPath()
        tri.moveTo(bx - 6, by - 11)
        tri.lineTo(bx + 12, by)
        tri.lineTo(bx - 6, by + 11)
        tri.closeSubpath()
        p.setBrush(col(QColor("#FFFFFF"), pa))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(tri)

        # 进度条
        px0, px1 = rect.left() + 108, rect.right() - 40
        py = rect.top() + 52
        frac = ease_in_out(prog(t, 1.4, 9.6)) * 0.62
        p.setPen(QPen(col(QColor(255, 255, 255, 30), pa), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(px0, py), QPointF(px1, py))
        if frac > 0:
            g = QLinearGradient(QPointF(px0, 0), QPointF(px1, 0))
            g.setColorAt(0.0, col(ACC, pa))
            g.setColorAt(1.0, col(GREEN, pa))
            p.setPen(QPen(g, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(px0, py), QPointF(px0 + (px1 - px0) * frac, py))
            hx = px0 + (px1 - px0) * frac
            glow(p, hx, py, 34, ACC_T, 0.45 * pa)
            dot(p, hx, py, 7, QColor("#FFFFFF"), pa)

        text(p, px0, py + 34, "0:12", 16, T3, alpha=pa * 0.9, mono=True)
        text(p, px1, py + 34, "0:38", 16, T3, alpha=pa * 0.9, mono=True,
             align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        text(p, rect.right() - 40, rect.top() + 26, "卡农 · 风物之诗琴", 17, T2, alpha=pa * 0.9,
             align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # 键位灯条（7×3 小方块）
        kx0, ky0 = rect.left() + 40, rect.top() + 128
        kw, kh, kg = 112, 28, 9
        for index in range(21):
            r_i, c_i = divmod(index, 7)
            on_at = _key_on_time(index)
            on = 0.0
            if on_at is not None and t > on_at:
                decay = clamp01((t - on_at) / 1.1)
                on = 1.0 - decay * 0.55
            x = kx0 + c_i * (kw + kg)
            y = ky0 + r_i * (kh + kg)
            r = QRectF(x, y, kw, kh)
            if on > 0.08:
                k = mix(QColor(30, 84, 210), QColor(46, 212, 127), clamp01((on - 0.55) / 0.45))
                card(p, r, 8, fill=mix(QColor(24, 30, 40), k, min(1.0, 0.35 + on * 0.75)),
                     stroke=mix(QColor(255, 255, 255, 60), k, on), alpha=pa)
                glow(p, r.center().x(), r.center().y(), 62, k, 0.26 * on * pa)
            else:
                card(p, r, 8, fill=QColor(255, 255, 255, 12), stroke=QColor(255, 255, 255, 14),
                     alpha=pa * 0.9)
            text(p, r.center().x(), r.center().y(),
                 f"{TL.NOTE_ORDER[index]}·{TL.KEY_ORDER[index]}", 14,
                 QColor("#03140C") if on > 0.08 else T2, alpha=pa * (1 if on > 0.08 else 0.65),
                 align=Qt.AlignmentFlag.AlignCenter, box_w=kw, mono=True)

    # ---- 右：两条通道 ----
    for index, (tag, sub, desc) in enumerate(TL.PLAY_CHANNELS):
        start = 1.9 + index * 0.55
        ai, adyi = stage(t, start, 0.85, 32)
        if ai <= 0:
            continue
        rect = QRectF(1090, 352 + index * 156 + adyi, 700, 132)
        picked = index == 0 and t > 4.4
        card(p, rect, 16,
             fill=QColor(42, 102, 240, 30) if picked else QColor(255, 255, 255, 10),
             stroke=QColor(75, 131, 240, 110) if picked else QColor(255, 255, 255, 20),
             alpha=ai, shadow=12 if picked else 0)
        if picked:
            glow(p, rect.left() + 40, rect.center().y(), 130, ACC, 0.14 * ai)
        # 开关胶囊
        cw, chh = 46, 26
        cx0 = rect.left() + 24
        cy0 = rect.center().y()
        p.setBrush(col(ACC if picked else QColor(255, 255, 255, 40), ai))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(cx0, cy0 - chh / 2, cw, chh), chh / 2, chh / 2)
        dot(p, cx0 + (cw - 11) if picked else cx0 + 11, cy0, 9, QColor("#FFFFFF"), ai)

        text(p, rect.left() + 96, cy0 - 20, tag, 24, T1, alpha=ai, weight=QFont.Weight.DemiBold)
        text(p, rect.left() + 96 + text_w(tag, 24, weight=QFont.Weight.DemiBold) + 14, cy0 - 20,
             sub, 18, ACC_T, alpha=ai * 0.95, mono=True)
        text(p, rect.left() + 96, cy0 + 20, desc, 18, T3, alpha=ai * 0.95)

    # 安全提示
    ai, ady = stage(t, 3.6, 0.8, 18)
    if ai > 0:
        y = 706 + ady
        card(p, QRectF(1090, y - 24, 700, 52), 26, fill=QColor(46, 212, 127, 20),
             stroke=QColor(46, 212, 127, 70), alpha=ai)
        dot(p, 1112, y + 2, 5, GREEN, ai)
        text(p, 1130, y + 2, TL.PLAY_SAFE, 19, mix(GREEN, QColor("#FFFFFF"), 0.35), alpha=ai)

    # 底部旋律音符流
    _melody_strip(p, t)


MELODY_LOCAL = TL.MELODY_START - TL.S_PLAY[0]
"""旋律第一个音在**本场景内**的时刻（scene_play 收到的是场景内时间）。"""


def _key_on_time(index: int) -> float | None:
    seq = (0, 3, 5, 8, 12, 14, 17, 19, 15, 10, 6, 1)
    if index in seq:
        pos = seq.index(index)
        return MELODY_LOCAL + pos * TL.MELODY_STEP
    return None


def _melody_strip(p: QPainter, t: float) -> None:
    x0, x1, y = 132.0, 1000.0, 752.0
    ai = ease_out(prog(t, 1.2, 1.0))
    if ai <= 0:
        return
    hline(p, x0, x1, y, QColor(255, 255, 255, 26), ai * 0.9)
    for index, note in enumerate(TL.MELODY):
        at = MELODY_LOCAL + index * TL.MELODY_STEP
        k = prog(t, at, 0.3)
        if k <= 0:
            continue
        x = x0 + (x1 - x0) * (index / (len(TL.MELODY) - 1))
        h = 26 + index % 3 * 10
        a = 1.0 - 0.5 * clamp01((t - at) / 1.6)
        p.setPen(QPen(col(ACC_L, a * ai), 2))
        p.drawLine(QPointF(x, y - 6), QPointF(x, y - 6 - h))
        dot(p, x, y - 6, 3.2, GREEN, a * ai)
        if t - at < 0.5:
            text(p, x, y - 6 - h - 18, note, 14, ACC_T,
                 alpha=(1 - (t - at) / 0.5) * ai,
                 align=Qt.AlignmentFlag.AlignCenter, box_w=40, mono=True)
    text(p, x0, y + 56, "采样音试听 · 键位灯随谱面点亮", 16, T3, alpha=ai * 0.85)


# --------------------------------------------------------------------------- #
# S6 导出
# --------------------------------------------------------------------------- #
def scene_export(p: QPainter, t: float) -> None:
    cx = W / 2
    al, dy = stage(t, 0.1, 0.8, 22)
    if al > 0:
        text(p, cx, 210 + dy, TL.EXPORT_TITLE, 54, T1, alpha=al, weight=QFont.Weight.Bold,
             align=Qt.AlignmentFlag.AlignCenter, box_w=1200)

    cards = TL.EXPORT_CARDS
    cw, chh0, gap = 500, 372, 40
    total = cw * 3 + gap * 2
    x0 = cx - total / 2
    for index, (name, ext, desc, code) in enumerate(cards):
        start = 0.7 + index * 0.28
        ai, adyi = stage(t, start, 0.9, 34)
        if ai <= 0:
            continue
        rect = QRectF(x0 + index * (cw + gap), 352 + adyi, cw, chh0)
        hot = index == 0
        card(p, rect, 20, fill=QColor(255, 255, 255, 12) if not hot else QColor(42, 102, 240, 26),
             stroke=QColor(255, 255, 255, 22) if not hot else QColor(75, 131, 240, 110),
             alpha=ai, shadow=18 if hot else 0)
        if hot:
            glow(p, rect.center().x(), rect.top() + 40, 300, ACC, 0.10 * ai)

        text(p, rect.left() + 36, rect.top() + 56, name, 28, T1, alpha=ai,
             weight=QFont.Weight.DemiBold)
        # 后缀 pill
        wE = text_w(ext, 15, mono=True) + 26
        card(p, QRectF(rect.right() - 36 - wE, rect.top() + 42, wE, 30), 15,
             fill=QColor(255, 255, 255, 14), stroke=QColor(255, 255, 255, 24), alpha=ai)
        text(p, rect.right() - 36 - wE + 13, rect.top() + 57, ext, 15, T3, alpha=ai, mono=True)

        text(p, rect.left() + 36, rect.top() + 108, desc, 19, T3, alpha=ai * 0.95)

        # 代码块
        code_rect = QRectF(rect.left() + 26, rect.top() + 150, cw - 52, 118)
        card(p, code_rect, 12, fill=QColor(6, 10, 14), stroke=QColor(255, 255, 255, 16), alpha=ai)
        # 逐字打出（两行）
        lines = code.split("\n")
        flat_len = sum(len(x) for x in lines) + len(lines) - 1
        typed = int(flat_len * prog(t, start + 0.4, 1.0))
        fx = code_rect.left() + 20
        fy = code_rect.top() + 36
        left = typed
        for row, line in enumerate(lines):
            shown = line[: max(0, left)]
            left -= len(line) + 1
            text(p, fx, fy + row * 38, str(row + 1), 15, T3, alpha=ai * 0.45, mono=True)
            if shown:
                cursor_row = 0 if typed <= len(lines[0]) else len(lines) - 1
                _code_line(p, fx + 26, fy + row * 38, shown, ai,
                           t if row == cursor_row else -1.0)
        text(p, fx, code_rect.bottom() + 28, "和弦字母 + 相对毫秒间隔", 16, T3,
             alpha=ai * 0.55)

    al, dy = stage(t, 1.9, 0.8, 18)
    if al > 0:
        text(p, cx, 806 + dy, TL.EXPORT_SUB, 22, T2, alpha=al * 0.95,
             align=Qt.AlignmentFlag.AlignCenter, box_w=1200)



def _code_line(p: QPainter, x: float, y: float, s: str, alpha: float, t: float) -> None:
    """等宽片段的语法着色：字母=白，数字=蓝，[延音]=绿。"""
    f = font(19, mono=True)
    fm = QFontMetricsF(f)
    cx = x
    index = 0
    while index < len(s):
        ch = s[index]
        if ch == "[":
            end = s.find("]", index)
            end = len(s) if end < 0 else end + 1
            seg = s[index:end]
            color = GREEN
        elif ch.isdigit():
            j = index
            while j < len(s) and s[j].isdigit():
                j += 1
            seg = s[index:j]
            color = ACC_T
            end = j
        elif ch.isalpha():
            j = index
            while j < len(s) and s[j].isalpha():
                j += 1
            seg = s[index:j]
            color = T1
            end = j
        else:
            seg = ch
            color = T3
            end = index + 1
        text(p, cx, y, seg, 19, color, alpha=alpha, mono=True)
        cx += fm.horizontalAdvance(seg)
        index = end
    # 光标
    if alpha > 0.4 and int(t * 2.2) % 2 == 0:
        p.setPen(QPen(col(ACC_T, alpha), 2))
        p.drawLine(QPointF(cx + 2, y - 12), QPointF(cx + 2, y + 12))


# --------------------------------------------------------------------------- #
# S7 收尾
# --------------------------------------------------------------------------- #
def scene_outro(p: QPainter, t: float) -> None:
    a = assets()
    cx = W / 2

    logo_a = ease_out(prog(t, 0.15, 0.85))
    scale = 0.9 + 0.1 * ease_out_back(prog(t, 0.15, 1.0), 1.15)
    size = 152 * scale
    glow(p, cx, 246, 380, ACC, 0.22 * logo_a)
    if logo_a > 0:
        p.setOpacity(logo_a)
        pix = a.logo_small
        p.drawPixmap(QRectF(cx - size / 2, 246 - size / 2, size, size), pix, QRectF(pix.rect()))
        p.setOpacity(1.0)

    al, dy = stage(t, 0.6, 0.8, 24)
    if al > 0:
        f = font(58, QFont.Weight.Bold, spacing=1.2)
        p.setFont(f)
        p.setPen(col(T1, al))
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(TL.OUTRO_TITLE)
        p.drawText(QRectF(cx - tw / 2, 388 + dy - fm.height() / 2, tw, fm.height()),
                   int(Qt.AlignmentFlag.AlignCenter), TL.OUTRO_TITLE)
        text(p, cx + tw / 2 + 16, 400 + dy, TL.OUTRO_VERSION, 18, ACC_T, alpha=al * 0.9, mono=True)

    # 四个数据
    feats = TL.OUTRO_FEATS
    gap = 60
    widths = [max(text_w(n, 46, weight=QFont.Weight.Bold), text_w(d, 18)) + 40 for n, d in feats]
    total = sum(widths) + gap * (len(feats) - 1)
    x = cx - total / 2
    for index, (num, desc) in enumerate(feats):
        ai, adyi = stage(t, 1.35 + index * 0.18, 0.75, 22)
        y = 530 + adyi
        text(p, x + widths[index] / 2, y, num, 46, T1, alpha=ai,
             weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter, box_w=widths[index],
             mono=True)
        text(p, x + widths[index] / 2, y + 46, desc, 18, T3, alpha=ai * 0.95,
             align=Qt.AlignmentFlag.AlignCenter, box_w=widths[index])
        if index < len(feats) - 1:
            hline(p, x + widths[index] + 14, x + widths[index] + 14, y, QColor(255, 255, 255),
                  ai * 0.0)
            p.setPen(QPen(col(QColor(255, 255, 255, 26), ai), 1))
            p.drawLine(QPointF(x + widths[index] + gap / 2, y - 30),
                       QPointF(x + widths[index] + gap / 2, y + 58))
        x += widths[index] + gap

    # 尾部提示 pill
    ai, ady = stage(t, 2.5, 0.9, 26)
    if ai > 0:
        label = TL.OUTRO_TAIL
        wl = text_w(label, 22, weight=QFont.Weight.Medium) + 60
        rect = QRectF(cx - wl / 2, 722 + ady, wl, 62)
        glow(p, cx, 722 + ady + 31, 280, ACC, 0.16 * ai)
        card(p, rect, 31, fill=QColor(42, 102, 240, 46), stroke=QColor(75, 131, 240, 130),
             alpha=ai, shadow=14)
        text(p, cx, 722 + ady + 31, label, 22, QColor("#FFFFFF"), alpha=ai,
             weight=QFont.Weight.Medium, align=Qt.AlignmentFlag.AlignCenter, box_w=wl)

    # 仓库信息（小字，压住底部空白）
    ai2, ady2 = stage(t, 3.2, 0.9, 16)
    if ai2 > 0:
        text(p, cx, 826 + ady2, "开源 · github.com/Ray-player", 16, T3, alpha=ai2 * 0.95,
             align=Qt.AlignmentFlag.AlignHCenter, box_w=800, spacing=0.6, mono=True)

    # 底部呼吸光带
    k = ease_in_out(prog(t, 2.9, 1.6))
    if k > 0:
        w2 = 300 * k
        g = QLinearGradient(QPointF(cx - w2, 0), QPointF(cx + w2, 0))
        g.setColorAt(0.0, col(ACC, 0.0))
        g.setColorAt(0.5, col(ACC_L, 0.5))
        g.setColorAt(1.0, col(ACC, 0.0))
        p.setPen(QPen(g, 1.4))
        p.drawLine(QPointF(cx - w2, 874), QPointF(cx + w2, 874))


# --------------------------------------------------------------------------- #
# 场景调度
# --------------------------------------------------------------------------- #
SCENES = (
    (TL.S_INTRO, scene_intro),
    (TL.S_PROBLEM, scene_problem),
    (TL.S_STEPS, scene_steps),
    (TL.S_DETECT, scene_detect),
    (TL.S_UI, scene_ui),
    (TL.S_PLAY, scene_play),
    (TL.S_EXPORT, scene_export),
    (TL.S_OUTRO, scene_outro),
)


def weights_at(t: float) -> list[tuple[int, float]]:
    out = []
    for index, ((start, stop), _) in enumerate(SCENES):
        w = 1.0
        if t < start:
            w = 0.0
        elif t < start + TL.CROSS:
            w = ease_in_out((t - start) / TL.CROSS)
        if t > stop:
            w = 0.0
        elif t > stop - TL.CROSS:
            w = min(w, ease_in_out((stop - t) / TL.CROSS))
        if w > 0.002:
            out.append((index, w))
    total = sum(w for _, w in out) or 1.0
    return [(i, w / total) for i, w in out]


def render_frame(t: float, canvas: QImage, scratch: QImage) -> None:
    """把 t 秒的画面画到 canvas 上（场景交叉叠化）。"""
    layers = weights_at(t)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.fillRect(QRectF(0, 0, W, H), BG)

    if len(layers) == 1:
        index, _ = layers[0]
        draw_bg(p, t)
        SCENES[index][1](p, t - SCENES[index][0][0])
    else:
        for index, weight in layers:
            scratch.fill(Qt.GlobalColor.transparent)
            sp = QPainter(scratch)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing)
            sp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            sp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            draw_bg(sp, t)
            SCENES[index][1](sp, t - SCENES[index][0][0])
            sp.end()
            p.setOpacity(weight)
            p.drawImage(0, 0, scratch)
            p.setOpacity(1.0)

    draw_chrome(p, t, chapter_label(t))
    p.end()
