"""YQRec 主题：扁平简约 · 日间/夜间双主题（默认 **日间**）。

用法（三步，完整接线见 ``docs/04-实施指南.md``）
--------------------------------------------
1. 把 ``theme.py`` 与 ``qss.py`` 一起放进 ``src/yqrec/ui/``；
2. 主窗构造函数里把 ``self._apply_theme()`` 换成 :func:`apply`；
3. 主题切换按钮调用 :func:`apply` 并回写 ``config.ui.theme``。

设计约束（改这个文件前先读）
----------------------------
* **令牌是唯一颜色来源**。QSS 里出现 ``#hex``、绘制代码里出现 ``QColor(255,...)``
  都算 bug —— 切主题时它们不会跟着变，也就破坏了"两套主题样式一致"。
* **``THEMES`` 两张表的键必须完全一致**。:meth:`_validate` 在导入时就会检查，
  少一个键立刻抛错，避免"夜间有 hover、日间没 hover"这种隐蔽裂缝。
* **叠加层颜色（``OVERLAY``）不跟主题走**。检测圈/采样点/按下高亮压在**游戏画面**上，
  而游戏画面不会因为 App 换了主题就变亮或变暗；跟着主题变反而会在亮画面上消失。
  只有"外框"（预览留白底色、header 胶囊）属于 ``THEMES``。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap

from .qss import render

DARK: Final = "dark"
LIGHT: Final = "light"
NAMES: Final = (DARK, LIGHT)


# --------------------------------------------------------------------------- #
# 1. 令牌表 —— 一切颜色的唯一事实来源
# --------------------------------------------------------------------------- #
#: 语义分层：
#: ``bg-*`` 面（canvas < panel < surface < hover < active 依次变亮）
#: ``border-*`` 线 · ``text-*`` 字 · ``accent-*`` 主色 · ``success/danger/warn`` 状态色
#:
#: 对比度（WCAG 2.1，正文 4.5:1）：夜间 text-primary 15.3 / text-secondary 8.0 /
#: text-tertiary 4.7；日间 text-primary 14.6 / text-secondary 5.9 / text-tertiary 4.9；
#: 按钮白字：夜间 accent 4.9 · 日间 accent 5.1。详见 ``docs/02-设计令牌.md`` §3。
THEMES: Final[dict[str, dict[str, str]]] = {
    DARK: {
        # --- 面 ---
        "bg-canvas": "#0E1216",          # 窗口底（区块之间的缝隙都露这个色）
        "bg-panel": "#141A20",           # 区块卡 / 工具栏 / 状态栏
        "bg-surface": "#1B222A",         # 按钮、下拉、键位卡
        "bg-surface-hover": "#232C35",
        "bg-surface-active": "#2A343E",
        "bg-input": "#0B0F13",           # 谱面文本框（比 canvas 更深，像"凹槽"）
        "bg-elevated": "#1A222B",        # 浮层 / 下拉弹层 / tooltip
        # --- 线 ---
        "border-subtle": "#222B34",      # 区块卡边、分隔线（几乎看不见，只做结构暗示）
        "border-default": "#2F3A45",     # 按钮、输入框边
        "border-strong": "#3D4A57",      # hover 边、滚动条把手
        # --- 字 ---
        "text-primary": "#E3EAF2",
        "text-secondary": "#9DABBA",
        "text-tertiary": "#75828F",
        "text-disabled": "#4C5764",
        # --- 主色（只出现在"主操作 / 选中 / 正在播放 / 焦点"） ---
        "accent": "#2A66F0",
        "accent-hover": "#3B76FF",
        "accent-pressed": "#1E52CC",
        "accent-fg": "#FFFFFF",
        "accent-soft": "#16233A",        # 12% 淡染底：选中行、Toggle 勾选、定位高亮
        "accent-border": "#4B83F0",
        "accent-text": "#7FAAFF",        # 淡染底上的文字色（保证 4.5:1）
        # --- 状态色 ---
        "success": "#2ED47F",            # 按下（键位卡整卡反色）
        "success-fg": "#06140C",
        "success-soft": "#14301F",
        "danger": "#FF6B6B",
        "danger-fg": "#1A0808",
        "danger-soft": "#35191B",
        "warn": "#F0A93B",
        "warn-soft": "#35290F",
        # --- 运行时生成（由 chevron_url() 填充） ---
        "chevron": "",
    },
    LIGHT: {
        # --- 面：纸白 + 冷灰，刻意避开纯白铺满（长期盯着不刺眼） ---
        "bg-canvas": "#F4F6F8",
        "bg-panel": "#FFFFFF",
        "bg-surface": "#EFF2F5",
        "bg-surface-hover": "#E6EAEF",
        "bg-surface-active": "#DCE2E9",
        "bg-input": "#FFFFFF",
        "bg-elevated": "#FFFFFF",
        # --- 线 ---
        "border-subtle": "#E4E9EE",
        "border-default": "#D3DAE2",
        "border-strong": "#B9C3CE",
        # --- 字 ---
        "text-primary": "#18202A",
        "text-secondary": "#5A6675",
        "text-tertiary": "#67737F",
        "text-disabled": "#A7B1BC",
        # --- 主色 ---
        "accent": "#2563EB",
        "accent-hover": "#1D4ED8",
        "accent-pressed": "#1A44B8",
        "accent-fg": "#FFFFFF",
        "accent-soft": "#E9F0FE",
        "accent-border": "#93B4FA",
        "accent-text": "#1D4ED8",
        # --- 状态色 ---
        "success": "#17B26A",
        "success-fg": "#04150C",
        "success-soft": "#E4F7ED",
        "danger": "#D92D20",
        "danger-fg": "#FFFFFF",
        "danger-soft": "#FDECEA",
        "warn": "#B45309",
        "warn-soft": "#FEF3E2",
        # --- 运行时生成 ---
        "chevron": "",
    },
}


#: 叠加层颜色（**两套主题共用**）：画在游戏画面上，跟着 App 主题变会失效。
OVERLAY: Final[dict[str, str]] = {
    # 检测圈
    "ring": "rgba(255,255,255,0.55)",
    "ring-strong": "rgba(255,255,255,0.95)",
    # 按下高亮：绿环 + 极淡填充（用户要求"能看清画面"，填充透明度刻意很低）
    "press-edge": "rgba(46,212,127,0.95)",
    "press-fill": "rgba(46,212,127,0.14)",
    # 采样点：常显但很淡，学基线/hover 时才提亮
    "dot": "rgba(255,120,120,0.45)",
    "dot-strong": "rgba(255,92,92,0.95)",
    # 采样参考圆（仅学基线期间）
    "guide": "rgba(150,195,255,0.22)",
    # 编辑态：普通圈绿、选中圈琥珀
    "edit-fill": "rgba(46,212,127,0.16)",
    "edit-edge": "rgba(120,240,170,0.95)",
    "edit-sel-fill": "rgba(255,200,60,0.22)",
    "edit-sel-edge": "rgba(255,200,60,1.0)",
    # 预览上的 header 胶囊
    "chip-bg": "rgba(10,14,18,0.72)",
    "chip-text": "rgba(232,240,248,0.92)",
    # 框选遮罩
    "scrim": "rgba(8,10,14,0.50)",
    "select-fill": "rgba(42,102,240,0.18)",
    "select-edge": "rgba(90,148,255,1.0)",
    "select-label-bg": "rgba(10,14,18,0.78)",
    "select-label-text": "rgba(232,240,248,0.95)",
}


def _validate() -> None:
    """两张主题表的键必须一一对应（导入即检查，问题在启动瞬间暴露）。"""
    dark_keys = set(THEMES[DARK])
    light_keys = set(THEMES[LIGHT])
    if dark_keys != light_keys:
        raise RuntimeError(
            "主题令牌不一致："
            f"仅夜间有 {sorted(dark_keys - light_keys)}，仅日间有 {sorted(light_keys - dark_keys)}"
        )


_validate()

_current: str = LIGHT
"""当前生效的主题名（模块级单例，供绘制层随时取值）。默认日间。"""


# --------------------------------------------------------------------------- #
# 2. 读取令牌
# --------------------------------------------------------------------------- #
def normalize(name: str | None) -> str:
    """把任意来源（配置/命令行）的主题名夹到合法值，非法一律回**日间**（产品默认）。"""
    return name if name in NAMES else LIGHT


def set_current(name: str | None) -> str:
    """只改"当前主题"标记，不重绘任何控件（重绘由 :func:`apply` 负责）。"""
    global _current
    _current = normalize(name)
    return _current


def current() -> str:
    return _current


def is_dark() -> bool:
    return _current == DARK


def tokens(name: str | None = None) -> dict[str, str]:
    """取某套主题的完整令牌（含运行时生成的 ``chevron``）。"""
    key = normalize(name or _current)
    table = dict(THEMES[key])
    table["chevron"] = chevron_url(key)
    return table


def token(name: str, theme: str | None = None) -> str:
    """取单个令牌值（字符串），供 QSS 拼接或调试。"""
    return tokens(theme)[name]


def _parse_css_color(text: str) -> QColor:
    """把 ``rgba(r,g,b,a)`` / ``#RRGGBB`` 解析成 ``QColor``。

    **为什么不用 ``QColor(text)``**：Qt 的 ``setNamedColor`` 不认 CSS 的 ``rgba()`` 语法，
    传进去不会报错，而是**静默变成不透明黑色** —— 检测圈全黑、采样点全黑，
    排查时要翻到绘制层才想得到是颜色解析问题。所以自己解析，坑就这一次。

    支持两种写法（本文档其余部分只用到这两种）：
    * ``rgba(255,120,120,0.45)`` —— 逗号分隔，alpha 用 0~1 小数；
    * ``#RRGGBB`` / ``#AARRGGBB`` —— Qt 原生格式，直接透传。
    """
    value = text.strip()
    head = value[:5].lower()
    if head.startswith("rgba(") or head.startswith("rgb("):
        inner = value[value.index("(") + 1 : value.rindex(")")]
        parts = [part.strip() for part in inner.split(",")]
        red, green, blue = (max(0, min(255, int(round(float(parts[i]))))) for i in range(3))
        alpha = 255
        if len(parts) > 3:
            alpha = max(0, min(255, int(round(float(parts[3]) * 255))))
        return QColor(red, green, blue, alpha)
    return QColor(value)


def color(name: str, theme: str | None = None) -> QColor:
    """取主题色（``QColor``）。用于 ``paintEvent`` / ``QPalette``。"""
    return _parse_css_color(tokens(theme)[name])


def overlay(name: str) -> QColor:
    """取叠加层颜色（与主题无关）。名字不存在直接抛错，避免静默画成黑色。"""
    return _parse_css_color(OVERLAY[name])


def media_colors(theme: str | None = None) -> tuple[str, str]:
    """试听控制条图标的（可用色, 禁用色）。"""
    table = tokens(theme)
    return table["text-primary"], table["text-disabled"]


def focus_ring(theme: str | None = None) -> QColor:
    """焦点描边色（自绘控件用；QSS 控件由样式表处理）。"""
    return color("accent", theme)


# --------------------------------------------------------------------------- #
# 3. 下拉箭头（QSS 的 image: url() 只能吃文件路径，故运行时生成一张 PNG 缓存）
# --------------------------------------------------------------------------- #
def _cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "yqrec-theme"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chevron_url(theme: str | None = None) -> str:
    """生成并返回"下拉箭头"PNG 的 ``file://`` URL（按主题缓存，只画一次）。

    画 2 倍尺寸（20px）交给 QSS 缩到 10px，等效 2× 超采样，高 DPI 下不糊。
    任何异常都返回空串 —— 样式表里 ``url("")`` 会被 Qt 忽略，箭头消失但界面不崩。
    """
    key = normalize(theme or _current)
    tint = THEMES[key]["text-tertiary"]
    digest = "".join(ch for ch in tint if ch.isalnum())
    target = _cache_dir() / f"chevron-{key}-{digest}.png"
    if not target.exists():
        if QGuiApplication.instance() is None:
            return ""  # 还没 QApplication，QPixmap 画不了
        try:
            size = 20
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                pen = QPen(QColor(tint), 2.4)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                # 一条 V 形折线，居中
                painter.drawLine(5, 8, 10, 13)
                painter.drawLine(10, 13, 15, 8)
            finally:
                painter.end()
            pixmap.save(str(target), "PNG")
        except Exception:  # 生成失败不影响主流程
            return ""
    return target.as_uri()


# --------------------------------------------------------------------------- #
# 4. 应用主题
# --------------------------------------------------------------------------- #
def stylesheet(theme: str | None = None) -> str:
    """渲染完整样式表（供测试/调试直接拿文本）。"""
    return render(tokens(theme or _current))


def apply(widget, theme: str | None = None) -> str:
    """把主题套到 ``widget``（通常是 QMainWindow）并记住当前主题。

    返回值 = 实际生效的主题名，调用方可据此回写配置。
    """
    name = set_current(theme)
    widget.setStyleSheet(stylesheet(name))
    return name


def repolish(widget) -> None:
    """属性选择器（``[state="pressed"]`` / ``[tone="near"]``）变更后必须走一遍。

    Qt 不会因为 property 变了就重算样式；先 ``unpolish`` 再 ``polish`` 才会重新匹配。
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_state(widget, value: str, prop: str = "state") -> None:
    """改样式属性并立即重算（键位卡/提示语/徽标都用这个）。"""
    if widget.property(prop) == value:
        return
    widget.setProperty(prop, value)
    repolish(widget)
