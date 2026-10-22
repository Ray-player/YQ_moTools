"""逐键取色（03 §3.3）：环形外圈均值，或可调边长的小方块均值。

**为什么要外环**（M2m）：键帽中心画着音符/图标，那些像素在按下时几乎不变 ——
它们只是把真正的信号稀释掉。用整块方形（含中心图标 + 四角背景）时，
同一个乐器在不同背景下的绝对跌幅能差一倍（花海 28 / 红树 69）；
只取外环后，两景的信号都涨到 50~80、干扰降到 2 左右，
信噪比从 4~12 倍提到 26~55 倍（实测见 `tests/test_real_fwzs_golden.py`）。

两种口径的几何都由**全局 `sampling` 段**给出（所有乐器统一，改 JSON 即时生效）：
- 环形：`inner_ratio·R .. RING_OUTER_RATIO·R`，环宽 = `ring_width_ratio·R`；
- 小方块：边长 `square_side_ratio·R` 的正方形，居中于键心（M2p 起为 1R，原为 2R）。

`inner_ratio=0` 表示走方块模式。
"""

from __future__ import annotations

import numpy as np

from ..types import DEFAULT_SQUARE_SIDE, RING_OUTER_RATIO, KeyGrid, KeyPoint

REF_WIDTH = 1920.0
"""`KeyPoint.r` 与 `offset_y` 的基准宽度（与 `InstrumentsSpec.button_radius` 一致）。"""

OUTER_RATIO = RING_OUTER_RATIO
"""环形外径占键半径的比例（历史名称，值来自 `types.RING_OUTER_RATIO`）。"""


class KeySampler:
    """预计算每键的切片（+ 可选环形掩膜），逐帧只做切片 + 均值。

    - 像素半径取 `KeyGrid` 中的归一化半径（来自 `InstrumentSpec.button_radius`）；
    - `inner_ratio > 0` 时只统计 `inner_ratio·R .. OUTER_RATIO·R` 的圆环（避开中心音符与四角背景）；
    - `inner_ratio == 0` 时统计边长 `square_side·R` 的正方形（`square_side=2` 即旧行为）；
    - 支持 `stride` 降采样（默认 4，面积减少 16 倍）；
    - 返回 (K, 3) uint8，顺序 = `KeyGrid.points` 顺序 = 音名顺序。
    """

    def __init__(
        self,
        grid: KeyGrid,
        frame_w: int,
        frame_h: int,
        *,
        stride: int = 4,
        inner_ratio: float = 0.0,
        square_side: float = DEFAULT_SQUARE_SIDE,
        offset_y: float = 0.0,
    ) -> None:
        if frame_w <= 0 or frame_h <= 0:
            raise ValueError("帧尺寸必须为正")
        if stride < 1:
            raise ValueError("stride 必须 >= 1")
        if not 0.0 <= inner_ratio < OUTER_RATIO:
            raise ValueError(f"inner_ratio 必须在 [0, {OUTER_RATIO}) 内: {inner_ratio}")
        if square_side <= 0:
            raise ValueError(f"square_side 必须为正: {square_side}")
        self._grid = grid
        self._frame_w = frame_w
        self._frame_h = frame_h
        self._stride = stride
        self._inner_ratio = float(inner_ratio)
        self._square_side = float(square_side)
        self._offset_y = float(offset_y)
        self._ring = self._inner_ratio > 0
        self._slices: list[tuple[slice, slice]] = [
            self._make_slice(p) for p in grid.points
        ]
        self._masks: list[np.ndarray] | None = (
            [self._make_mask(p) for p in grid.points] if self._ring else None
        )

    @property
    def key_count(self) -> int:
        return self._grid.key_count

    @property
    def frame_size(self) -> tuple[int, int]:
        return (self._frame_w, self._frame_h)

    @property
    def inner_ratio(self) -> float:
        return self._inner_ratio

    @property
    def offset_y(self) -> float:
        return self._offset_y

    @property
    def square_side(self) -> float:
        return self._square_side

    @property
    def is_ring(self) -> bool:
        return self._ring

    def _dy(self) -> float:
        """纵向偏移（帧像素）：配置里按 1920 宽存，这里按实际帧宽等比缩放。"""
        return self._offset_y * self._frame_w / REF_WIDTH

    def _half_extent(self, r: float) -> float:
        """采样窗的半边长（帧像素）：环形要整块 ±R 再靠掩膜裁，方块取 side/2。"""
        return r if self._ring else r * self._square_side / 2.0

    def _bounds(self, p: KeyPoint) -> tuple[int, int, int, int]:
        cx, cy, r = p.to_pixels(self._frame_w, self._frame_h)
        cy += self._dy()
        half = self._half_extent(r)
        x0 = max(0, int(cx - half))
        y0 = max(0, int(cy - half))
        x1 = min(self._frame_w, max(x0 + 1, int(cx + half)))
        y1 = min(self._frame_h, max(y0 + 1, int(cy + half)))
        return x0, y0, x1, y1

    def _make_slice(self, p: KeyPoint) -> tuple[slice, slice]:
        x0, y0, x1, y1 = self._bounds(p)
        return (slice(y0, y1, self._stride), slice(x0, x1, self._stride))

    def _make_mask(self, p: KeyPoint) -> np.ndarray:
        """按 stride 采样后的网格生成圆环掩膜（尺寸与切片 ROI 严格一致）。"""
        cx, cy, r = p.to_pixels(self._frame_w, self._frame_h)
        cy += self._dy()
        x0, y0, x1, y1 = self._bounds(p)
        ys = np.arange(y0, y1, self._stride, dtype=np.float64) - cy
        xs = np.arange(x0, x1, self._stride, dtype=np.float64) - cx
        if ys.size == 0 or xs.size == 0:
            return np.zeros((1, 1), dtype=bool)
        gy, gx = np.meshgrid(ys, xs, indexing="ij")
        dist2 = gx * gx + gy * gy
        mask = dist2 <= (r * OUTER_RATIO) ** 2
        mask &= dist2 >= (r * self._inner_ratio) ** 2
        return mask

    def sample(self, frame: np.ndarray) -> np.ndarray:
        """返回 (K,3) uint8 RGB 均值；`frame` 必须是 HxWx3 BGR。"""
        h, w = frame.shape[:2]
        if (w, h) != (self._frame_w, self._frame_h):
            raise ValueError(
                f"帧尺寸变化: 期望 {self._frame_w}x{self._frame_h}，实际 {w}x{h}；"
                "请重建 KeySampler"
            )
        out = np.empty((len(self._slices), 3), dtype=np.uint8)
        for i, (sy, sx) in enumerate(self._slices):
            roi = frame[sy, sx]
            if roi.size == 0:
                out[i] = 0
                continue
            mask = self._masks[i] if self._masks is not None else None
            if mask is None or mask.shape != roi.shape[:2] or not mask.any():
                pixels = roi.reshape(-1, 3)
            else:
                pixels = roi[mask]
            b, g, r = pixels.mean(axis=0)
            out[i] = (round(r), round(g), round(b))  # BGR -> RGB
        return out

    def sample_tuple(self, frame: np.ndarray) -> tuple[tuple[int, int, int], ...]:
        """转成 `RgbSample.rgb` 需要的不可变结构。"""
        arr = self.sample(frame)
        return tuple((int(v[0]), int(v[1]), int(v[2])) for v in arr)
