"""21 键状态灯面板：按 `spec.key_order` 排成 7×3 卡片栅格。

每张卡显示：音名（如 `+1`）、按键字母（如 `Q`）、当前 `R,G,B`、与基线的 `Δ`；
按下时整卡绿底深字，未标定时是虚线空卡。

样式**全部来自 `theme.py` 的令牌**（`main_window` 设一次全局 QSS，这里只挂 objectName
与 `state` 属性）。改属性后必须 `repolish()` —— Qt 不会因为 property 变了就重算样式，
这是设计包 `04` §8 里记的那个坑；忘了就会表现为"怎么调 Δ 都不变琥珀"。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from ..types import InstrumentSpec, KeyThreshold
from . import theme

COLUMNS = 7
"""每行 7 个键（与游戏键位布局一致）。"""

CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2}

MAX_CHANNEL = "MAX"
"""`_channel_by_area` 的特殊值：按"三通道最大跌幅"显示 Δ（max_drop 阈值模式）。"""

NEAR_RATIO = 0.60
"""Δ 达到阈值的这个比例就提前预警（琥珀）。

0.60 是经验值：正常抖动在 10~25%，低于 60% 会一直闪；高于 80% 常常来不及反应。
"""


class KeyCard(QFrame):
    """单个键的状态卡。

    三行：`+1 · Q`（音名 · 键名）/ `247/242/230`（当前 RGB）/ `Δ12`（判据量）。
    字号与颜色由 QSS 给（`9px` / `11px`），**不要再用 QFont** —— DPI 缩放时
    QSS 按 px、QFont 按 pt，两者会打架（同一个数值在 125% 下大小不一）。
    """

    def __init__(self, note: str, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("KeyCard")
        self.setMinimumSize(64, 64)
        self.setMaximumHeight(88)
        self._name = QLabel(f"{note} · {key}", self)
        self._name.setObjectName("KeyName")
        self._rgb = QLabel("—/—/—", self)
        self._rgb.setObjectName("KeyRgb")
        self._delta = QLabel("Δ—", self)
        self._delta.setObjectName("KeyDelta")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        for widget in (self._name, self._rgb, self._delta):
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(widget)
        self._pressed = False
        self._active = True
        self._threshold_delta = 0
        """该键的 Δ 阈值（逐键替换基线后与判按同口径），用于"接近阈值"预警。"""
        self._last_delta: float | None = None
        self._apply_state()

    # -- 状态 -------------------------------------------------------------- #

    def set_pressed(self, pressed: bool) -> None:
        if self._pressed == pressed:
            return
        self._pressed = pressed
        self._apply_state()
        if pressed:
            theme.set_state(self._delta, "pressed", prop="tone")
        else:
            self._refresh_delta_tone()

    def set_active(self, active: bool) -> None:
        """是否已标定（未标定时显示为虚线空卡）。"""
        if self._active == active:
            return
        self._active = active
        self._apply_state()

    def _apply_state(self) -> None:
        state = "pressed" if self._pressed else ("idle" if self._active else "disabled")
        theme.set_state(self, state)

    def refresh_style(self) -> None:
        """主题切换后调用：属性没变，但样式表换了，需要重算一次。"""
        theme.repolish(self)
        self._refresh_delta_tone()

    # -- 数值 -------------------------------------------------------------- #

    def set_metrics(self, rgb: tuple[int, int, int] | None, delta: float | None) -> None:
        # 卡片宽度有限，用紧凑写法 "247/242/230"，避免居中裁掉首尾数字
        if rgb is None:
            self._rgb.setText("—/—/—")
        else:
            self._rgb.setText(f"{rgb[0]}/{rgb[1]}/{rgb[2]}")
        self._delta.setText("Δ—" if delta is None else f"Δ{delta:.0f}")
        self._last_delta = delta
        self._refresh_delta_tone()

    def _refresh_delta_tone(self) -> None:
        """Δ 接近阈值（≥60%）时把第 3 行染成琥珀 —— 让用户"看见差一点"。"""
        near = (
            self._last_delta is not None
            and self._threshold_delta > 0
            and not self._pressed
            and self._last_delta >= NEAR_RATIO * self._threshold_delta
        )
        theme.set_state(self._delta, "near" if near else "", prop="tone")


class KeysPanel(QWidget):
    """键位灯面板（7 列栅格）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(8, 0, 8, 8)
        self._layout.setSpacing(4)
        self._cards: list[KeyCard] = []
        self._area_of_key: dict[str, str] = {}
        self._channel_by_area: dict[str, str] = {}
        self._baseline: np.ndarray | None = None
        self._thresholds: dict[str, KeyThreshold] = {}
        self._key_thresholds: list[KeyThreshold | None] = []

    def set_spec(self, spec: InstrumentSpec) -> None:
        """重建卡片（换乐器时调用）。"""
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards = []
        self._area_of_key = {}
        notes = spec.note_order
        for index, key in enumerate(spec.key_order):
            note = notes[index] if index < len(notes) else str(index + 1)
            card = KeyCard(note, key, self)
            self._layout.addWidget(card, index // COLUMNS, index % COLUMNS)
            self._cards.append(card)
            self._area_of_key[key] = spec.area_of_key(key).code
        self._layout.setRowStretch(3, 1)
        self._baseline = None
        self._channel_by_area = {}
        self._key_thresholds = []

    def refresh_style(self) -> None:
        """主题切换后逐卡重算（QSS 管不到 `setProperty` 之后的状态）。"""
        for card in self._cards:
            card.refresh_style()

    def set_baseline(
        self,
        baseline: np.ndarray | None,
        channel_by_area: dict[str, str],
        thresholds: dict[str, KeyThreshold] | None = None,
    ) -> None:
        self._baseline = None if baseline is None else np.asarray(baseline, dtype=np.float32)
        self._channel_by_area = dict(channel_by_area)
        self._thresholds = dict(thresholds or {})
        # 逐键阈值：把区域级的 Δ/权重/死区 + 该键自己的基线拼起来（与判按完全同口径）
        self._key_thresholds = []
        for index, key in enumerate(self._area_of_key):
            threshold = self._thresholds.get(self._area_of_key.get(key, ""))
            if threshold is None:
                self._key_thresholds.append(None)
                continue
            if self._baseline is not None and index < len(self._baseline):
                base = self._baseline[index]
                threshold = replace(
                    threshold,
                    base_r=int(round(float(base[0]))),
                    base_g=int(round(float(base[1]))),
                    base_b=int(round(float(base[2]))),
                )
            self._key_thresholds.append(threshold)
        # Δ 预警阈值也要同步给每张卡（换乐器/重学基线都会走到这里）
        for index, card in enumerate(self._cards):
            threshold = (
                self._key_thresholds[index] if index < len(self._key_thresholds) else None
            )
            card._threshold_delta = int(threshold.delta) if threshold is not None else 0
            card.set_active(self._baseline is not None)
        if self._baseline is None:
            for card in self._cards:
                card.set_metrics(None, None)
        else:
            for card in self._cards:
                card._refresh_delta_tone()

    def update_samples(self, rgb: np.ndarray) -> None:
        """刷新每张卡的 RGB 与判据量（与 `PressDetector` 完全同口径）。"""
        if rgb is None:
            return
        for index, card in enumerate(self._cards):
            if index >= len(rgb):
                break
            values = (int(rgb[index][0]), int(rgb[index][1]), int(rgb[index][2]))
            delta: float | None = None
            threshold = (
                self._key_thresholds[index] if index < len(self._key_thresholds) else None
            )
            if threshold is not None:
                if threshold.mode == "weighted":
                    delta = float(threshold.weighted_score(values))
                elif threshold.mode == "max_drop":
                    delta = float(threshold.max_drop(values))
                elif threshold.mode == "chroma":
                    delta = float(threshold.chroma_shift(values))
                elif threshold.mode == "euclid":
                    delta = float(threshold.euclid_distance(values))
            elif self._baseline is not None and index < len(self._baseline):
                area = self._area_of_key.get(
                    list(self._area_of_key)[index] if index < len(self._area_of_key) else "", ""
                )
                channel = self._channel_by_area.get(area, "R")
                base = self._baseline[index]
                if channel == MAX_CHANNEL:
                    delta = max(float(base[i]) - float(values[i]) for i in range(3))
                else:
                    channel_index = CHANNEL_INDEX.get(channel, 0)
                    delta = float(base[channel_index]) - float(values[channel_index])
            card.set_metrics(values, delta)

    def set_press(self, flags: list[bool] | None) -> None:
        flags = list(flags or [])
        for index, card in enumerate(self._cards):
            card.set_pressed(index < len(flags) and flags[index])

    def clear_press(self) -> None:
        for card in self._cards:
            card.set_pressed(False)
