"""YQRec 样式表模板（唯一一份，日/夜两套主题共用）。

设计意图
--------
* **几何与配色分离**：本文件只描述"长什么样"（尺寸/间距/圆角/边框/状态），
  颜色一律写成 ``{{token-name}}`` 占位符，由 :func:`render` 用 ``theme.py`` 的令牌替换。
  这样切换主题只换颜色，绝不会出现"夜里有圆角、日间变直角"这类漂移。
* **不写死任何 #hex**：需要新颜色时先往 ``theme.py`` 的 ``THEMES`` 里加令牌，
  再来这里引用。评审时若在本文件搜到 ``#``（注释除外）就是 bug。

给实现者
--------
* 唯一入口是 :func:`render`，由 ``theme.qss()`` 调用；不要直接读这个常量。
* 选择器用 **objectName**（``#Primary`` / ``#OnTop`` …），不用动态 property，
  因为 property 变更要手动 ``unpolish/polish``，很容易忘（见 docs/04 §3.4）。
* ``:focus`` 只改**边框颜色**不动宽度 —— 否则获得焦点时按钮会跳 1px。
"""

from __future__ import annotations

from collections.abc import Mapping

#: 样式表模板。``{{name}}`` 会被 ``theme.py`` 的令牌替换。
TEMPLATE = r"""
/* =====================================================================
   0. 全局：字体与文字色
   ===================================================================== */
QMainWindow {
    background: {{bg-canvas}};
}
QWidget {
    color: {{text-primary}};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 12px;
}
QDialog, QMessageBox {
    background: {{bg-panel}};
}

/* =====================================================================
   1. 容器：工具栏 / 区块卡 / 分割条
   ===================================================================== */
QToolBar {
    background: {{bg-panel}};
    border: 0;
    border-bottom: 1px solid {{border-subtle}};
    padding: 0;
    spacing: 0;
}
QWidget#ToolbarHolder {
    background: transparent;
}
/* 区块卡：三等分区域的外壳（键位状态 / 预览 / 录制内容） */
QFrame#SectionCard {
    background: {{bg-panel}};
    border: 1px solid {{border-subtle}};
    border-radius: 8px;
}
/* 分割条与画布同色 → 视觉上"消失"，hover 才浮出一条细线 */
QSplitter::handle:vertical {
    background: {{bg-canvas}};
    height: 8px;
}
QSplitter::handle:vertical:hover {
    background: {{border-strong}};
}

/* =====================================================================
   2. 文字层级：区块标题 / 次级说明 / 数值
   ===================================================================== */
QLabel {
    background: transparent;
}
QLabel#SectionTitle {
    color: {{text-secondary}};
    font-size: 12px;
    font-weight: 600;
}
QLabel#SectionMeta {
    color: {{text-tertiary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
QLabel#Meta {
    color: {{text-tertiary}};
    font-size: 11px;
}
QLabel#FieldLabel {
    color: {{text-tertiary}};
    font-size: 11px;
}
/* 区域信息胶囊：1600x900 · 屏 1/2 */
QLabel#RegionChip {
    background: {{bg-surface}};
    border: 1px solid {{border-subtle}};
    border-radius: 6px;
    color: {{text-secondary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
    padding: 0 8px;
    min-height: 24px;
}
QLabel#RegionChip[state="empty"] {
    color: {{text-tertiary}};
    background: transparent;
    border-style: dashed;
    border-color: {{border-default}};
}
/* 录制区右侧的一行状态（"录制中… 12 个音 · 0:05"） */
QLabel#RecordInfo {
    color: {{text-tertiary}};
    font-size: 11px;
}
/* 半径数值 0.80R */
QLabel#ValueChip {
    color: {{text-primary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
    min-width: 42px;
}

/* =====================================================================
   3. 按钮：三档层级 + 图标按钮
   ---------------------------------------------------------------------
   Secondary  = 默认（缓和的底 + 1px 边）
   Primary    = 主操作，一行里最多一个（accent 填充）
   Quiet      = 次级/工具（无底无边，hover 才浮现）
   Danger     = "停止预览 / 停止录制" 这类收尾操作（danger 柔和底，不实心）
   ===================================================================== */
QPushButton {
    background: {{bg-surface}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
    color: {{text-primary}};
    padding: 0 10px;
    min-height: 24px;
    outline: 0;
}
QPushButton:hover {
    background: {{bg-surface-hover}};
    border-color: {{border-strong}};
}
QPushButton:pressed {
    background: {{bg-surface-active}};
}
QPushButton:disabled {
    background: {{bg-surface}};
    border-color: {{border-subtle}};
    color: {{text-disabled}};
}
/* 焦点框：只换边框色，不换宽度（避免布局跳动） */
QPushButton:focus {
    border-color: {{accent}};
}

QPushButton#Primary {
    background: {{accent}};
    border-color: {{accent}};
    color: {{accent-fg}};
    font-weight: 600;
}
QPushButton#Primary:hover {
    background: {{accent-hover}};
    border-color: {{accent-hover}};
}
QPushButton#Primary:pressed {
    background: {{accent-pressed}};
}
QPushButton#Primary:disabled {
    background: {{bg-surface}};
    border-color: {{border-subtle}};
    color: {{text-disabled}};
}
QPushButton#Primary:focus {
    border-color: {{accent-border}};
}

QPushButton#Quiet {
    background: transparent;
    border-color: transparent;
    color: {{text-secondary}};
}
QPushButton#Quiet:hover {
    background: {{bg-surface}};
    border-color: {{border-subtle}};
    color: {{text-primary}};
}
QPushButton#Quiet:pressed {
    background: {{bg-surface-active}};
}

QPushButton#Danger {
    background: {{danger-soft}};
    border-color: {{danger}};
    color: {{danger}};
    font-weight: 600;
}
QPushButton#Danger:hover {
    background: {{danger}};
    border-color: {{danger}};
    color: {{danger-fg}};
}

/* 16:9 这类开关：勾选 = accent 柔和底 + accent 文字 */
QPushButton#Toggle {
    background: transparent;
    border-color: {{border-default}};
    color: {{text-tertiary}};
    padding: 0 8px;
}
QPushButton#Toggle:hover {
    border-color: {{border-strong}};
    color: {{text-secondary}};
}
QPushButton#Toggle:checked {
    background: {{accent-soft}};
    border-color: {{accent-border}};
    color: {{accent-text}};
}

/* 图钉 / 主题切换等纯图标按钮 */
QPushButton#IconButton, QPushButton#OnTop {
    background: transparent;
    border-color: transparent;
    padding: 0;
}
QPushButton#IconButton:hover, QPushButton#OnTop:hover {
    background: {{bg-surface}};
    border-color: {{border-subtle}};
}
QPushButton#OnTop:checked {
    background: {{accent-soft}};
    border-color: {{accent-border}};
}

/* 分段控件（检测方式 / 编辑模式）：容器做底，段内透明 */
QWidget#Segmented {
    background: {{bg-surface}};
    border: 1px solid {{border-subtle}};
    border-radius: 6px;
}
QPushButton#Segment {
    background: transparent;
    border: 0;
    border-radius: 4px;
    color: {{text-tertiary}};
    padding: 0 8px;
    min-height: 20px;
    font-size: 11px;
}
QPushButton#Segment:hover {
    color: {{text-primary}};
}
QPushButton#Segment:checked {
    background: {{bg-panel}};
    color: {{text-primary}};
    font-weight: 600;
}
QPushButton#Segment:disabled {
    color: {{text-disabled}};
}

/* =====================================================================
   4. 输入：下拉 / 文本框 / 滑条
   ===================================================================== */
QComboBox {
    background: {{bg-surface}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
    color: {{text-primary}};
    padding: 0 6px;
    min-height: 24px;
}
QComboBox:hover {
    border-color: {{border-strong}};
}
QComboBox:focus, QComboBox:on {
    border-color: {{accent}};
}
QComboBox:disabled {
    background: {{bg-surface}};
    border-color: {{border-subtle}};
    color: {{text-disabled}};
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 18px;
    border: 0;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("{{chevron}}");
    width: 10px;
    height: 10px;
}
QComboBox QAbstractItemView {
    background: {{bg-elevated}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
    color: {{text-primary}};
    outline: 0;
    padding: 4px;
    selection-background-color: {{accent-soft}};
    selection-color: {{accent-text}};
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 0 6px;
    border-radius: 4px;
}

QPlainTextEdit#RecordView {
    background: {{bg-input}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
    color: {{text-primary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
    padding: 6px 8px;
    selection-background-color: {{accent}};
    selection-color: {{accent-fg}};
}
QPlainTextEdit#RecordView:focus {
    border-color: {{accent}};
}
QPlainTextEdit#RecordView[readonly="true"] {
    background: {{bg-surface}};
    color: {{text-secondary}};
}

QSlider {
    min-height: 18px;
}
QSlider::groove:horizontal {
    height: 4px;
    background: {{border-subtle}};
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: {{accent}};
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {{accent}};
    border: 2px solid {{bg-panel}};
}
QSlider::handle:horizontal:hover {
    background: {{accent-hover}};
}
QSlider::handle:horizontal:disabled {
    background: {{text-disabled}};
}

/* =====================================================================
   5. 键位卡（三种状态用 objectName + state 属性）
   ---------------------------------------------------------------------
   idle     已标定、未按下
   pressed  判定按下（全局最抢眼的信号：绿底深字）
   disabled 未标定（虚线空卡）
   ===================================================================== */
QFrame#KeyCard {
    background: {{bg-surface}};
    border: 1px solid {{border-subtle}};
    border-radius: 6px;
}
QFrame#KeyCard QLabel {
    background: transparent;
    color: {{text-secondary}};
    font-family: Consolas, "Cascadia Mono", monospace;
}
QFrame#KeyCard QLabel#KeyName {
    color: {{text-primary}};
    font-size: 11px;
    font-weight: 600;
}
QFrame#KeyCard QLabel#KeyRgb,
QFrame#KeyCard QLabel#KeyDelta {
    color: {{text-tertiary}};
    font-size: 9px;
}
/* Δ 接近阈值（60%~100%）：提前预警，但仍未判按下 */
QFrame#KeyCard QLabel#KeyDelta[tone="near"] {
    color: {{warn}};
}

QFrame#KeyCard[state="pressed"] {
    background: {{success}};
    border-color: {{success}};
}
QFrame#KeyCard[state="pressed"] QLabel,
QFrame#KeyCard[state="pressed"] QLabel#KeyName,
QFrame#KeyCard[state="pressed"] QLabel#KeyRgb,
QFrame#KeyCard[state="pressed"] QLabel#KeyDelta {
    color: {{success-fg}};
}

QFrame#KeyCard[state="disabled"] {
    background: transparent;
    border: 1px dashed {{border-default}};
}
QFrame#KeyCard[state="disabled"] QLabel,
QFrame#KeyCard[state="disabled"] QLabel#KeyName {
    color: {{text-disabled}};
}

/* =====================================================================
   6. 试听控制条：合成一个胶囊，内部图标按钮透明
   ===================================================================== */
QWidget#PlayBar {
    background: {{bg-surface}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
}
QPushButton#Media {
    background: transparent;
    border: 0;
    border-radius: 4px;
    padding: 0;
}
QPushButton#Media:hover {
    background: {{bg-surface-hover}};
}
QPushButton#Media:pressed {
    background: {{bg-surface-active}};
}
QPushButton#Media:disabled {
    background: transparent;
}
QPushButton#MediaStop {
    background: transparent;
    border: 0;
    color: {{text-secondary}};
    padding: 0 8px;
    min-height: 22px;
}
QPushButton#MediaStop:hover {
    color: {{danger}};
}

/* =====================================================================
   7. 浮层：手动点选面板（带投影，由 QGraphicsDropShadowEffect 提供）
   ===================================================================== */
QFrame#EditPanel {
    background: {{bg-elevated}};
    border: 1px solid {{border-default}};
    border-radius: 8px;
}
QFrame#EditPanel QLabel#EditCount {
    color: {{text-tertiary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
QFrame#EditPanel QLabel#EditHint {
    color: {{text-tertiary}};
    font-size: 11px;
}

/* =====================================================================
   8. 状态栏：徽标 + 数值 + 单条消息
   ===================================================================== */
QStatusBar {
    background: {{bg-panel}};
    border-top: 1px solid {{border-subtle}};
    color: {{text-tertiary}};
    min-height: 26px;
}
QStatusBar::item {
    border: 0;
}
QStatusBar QLabel {
    color: {{text-tertiary}};
    font-size: 11px;
}
QLabel#BackendChip {
    background: {{bg-surface}};
    border: 1px solid {{border-subtle}};
    border-radius: 4px;
    color: {{text-secondary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 10px;
    padding: 0 6px;
    min-height: 16px;
}
QLabel#BackendChip[tone="warn"] {
    background: {{warn-soft}};
    border-color: {{warn}};
    color: {{warn}};
}
QLabel#StatusText {
    color: {{text-tertiary}};
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
/* 统一消息区：info / warn / error 三种语气，替代原来 showMessage + 红字并存 */
QLabel#Toast {
    color: {{text-secondary}};
    font-size: 11px;
}
QLabel#Toast[tone="warn"] {
    color: {{warn}};
}
QLabel#Toast[tone="error"] {
    color: {{danger}};
}

/* =====================================================================
   9. 滚动条 / 提示气泡 / 原生对话框按钮
   ===================================================================== */
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: {{border-strong}};
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: {{text-tertiary}};
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: {{border-strong}};
    border-radius: 4px;
    min-width: 24px;
}

QToolTip {
    background: {{bg-elevated}};
    border: 1px solid {{border-default}};
    border-radius: 6px;
    color: {{text-primary}};
    padding: 5px 8px;
}
QMessageBox QPushButton {
    min-width: 72px;
}
"""


def render(tokens: Mapping[str, str]) -> str:
    """把模板里的 ``{{token}}`` 全部替换成实际值。

    参数 ``tokens`` 必须包含模板用到的**所有**键，漏一个就抛 ``KeyError`` ——
    这是故意的：宁可启动报错，也不要静默渲染出一份半透明的坏样式。
    """
    css = TEMPLATE
    missing: list[str] = []
    for name, value in tokens.items():
        placeholder = "{{" + name + "}}"
        if placeholder in css:
            css = css.replace(placeholder, value)
    # 检查还有没有没被替换的占位符（= 令牌表漏项）
    for start in _iter_placeholders(css):
        missing.append(start)
    if missing:
        raise KeyError("样式表缺少令牌: " + ", ".join(sorted(set(missing))))
    return css


def _iter_placeholders(css: str):
    """找出残留的 ``{{xxx}}``（只用于报错定位）。"""
    index = 0
    while True:
        left = css.find("{{", index)
        if left < 0:
            return
        right = css.find("}}", left)
        if right < 0:
            return
        yield css[left + 2 : right]
        index = right + 2
