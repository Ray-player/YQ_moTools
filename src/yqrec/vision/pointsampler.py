"""分段点采样（v1.20）：只采键帽上一圈"基准点"，不做整块/整环均值。

设计动机（与 `KeySampler` 的整环均值对比）：
- 整环均值把「信息最多的那圈像素」和「几乎不变的像素」一起平均了 ——
  0.55R~0.98R 里外圈信号强、靠近 0.55R 的一侧更接近中心图标（按键时变化小），
  平均后被稀释；
- 分段点只取固定个数、等距分布的点，再对每个点取一小块圆斑均值（抗压缩噪声），
  既保住对比度，又把样本量从 ~250 像素降到 ~400（环形）/ ~1600（方块 4 边）。

两种几何（与 `assets/instruments.json` 的 `sampling` 段一一对应）：
- **环形**：起点 = 内环圆上方水平切点 `(cx, cy - r_inner)`，
  沿内环圆周等角取 `ring_points`（默认 8）个点（顺时针），内环默认 **0.80R**；
- **方块**：起点 = 左上角 `(cx-half, cy-half)`，沿四条边顺时针各取
  `square_points`（默认 4）个点（每边步长 = 边长 / square_points），共 `4×square_points` 个点；
  半边长默认 **0.55R**。
"""

from __future__ import annotations

import math

import numpy as np

from ..types import (
    DEFAULT_POINT_PATCH_RATIO,
    DEFAULT_RING_POINTS,
    DEFAULT_SQUARE_POINTS,
    DEFAULT_SQUARE_SIDE,
    KeyGrid,
    KeyPoint,
)

REF_WIDTH = 1920.0
"""`KeyPoint.r` 与 `offset_y` 的基准宽度（与 `InstrumentSpec.button_radius` 一致）。"""


def ring_base_points(
    cx: float, cy: float, r: float, inner_ratio: float, count: int
) -> list[tuple[float, float]]:
    """内环圆周上的等角基准点：起点在内环正上方，顺时针。"""
    count = max(1, int(count))
    radius = max(0.0, float(inner_ratio)) * r
    out: list[tuple[float, float]] = []
    for k in range(count):
        angle = -math.pi / 2.0 + 2.0 * math.pi * k / count
        out.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return out


def square_base_points(
    cx: float, cy: float, r: float, square_side: float, count: int
) -> list[tuple[float, float]]:
    """方块四条边上的基准点：起点在左上角，顺时针，每边 `count` 个点。"""
    count = max(1, int(count))
    half = max(0.05, float(square_side)) * r / 2.0
    side = 2.0 * half
    step = side / count
    x0, y0 = cx - half, cy - half
    out: list[tuple[float, float]] = []
    for k in range(count):  # 上边：左 → 右
        out.append((x0 + step * k, y0))
    for k in range(count):  # 右边：上 → 下
        out.append((x0 + side, y0 + step * k))
    for k in range(count):  # 下边：右 → 左
        out.append((x0 + side - step * k, y0 + side))
    for k in range(count):  # 左边：下 → 上
        out.append((x0, y0 + side - step * k))
    return out


def base_points(
    cx: float,
    cy: float,
    r: float,
    *,
    inner_ratio: float,
    square_side: float = DEFAULT_SQUARE_SIDE,
    ring_points: int = DEFAULT_RING_POINTS,
    square_points: int = DEFAULT_SQUARE_POINTS,
) -> list[tuple[float, float]]:
    """按当前口径给出一个键的基准点（环形 / 方块由 `inner_ratio` 决定）。"""
    if inner_ratio > 0:
        return ring_base_points(cx, cy, r, inner_ratio, ring_points)
    return square_base_points(cx, cy, r, square_side, square_points)


class KeyPointSampler:
    """预计算每键的基准点像素索引，逐帧只做 gather + 均值。

    与 `KeySampler` 接口对齐（`sample` / `sample_tuple` / `key_count` / `frame_size`），
    便于 `ui/capture_worker.py` 在两种实现间切换。
    """

    def __init__(
        self,
        grid: KeyGrid,
        frame_w: int,
        frame_h: int,
        *,
        inner_ratio: float = 0.0,
        square_side: float = DEFAULT_SQUARE_SIDE,
        offset_y: float = 0.0,
        ring_points: int = DEFAULT_RING_POINTS,
        square_points: int = DEFAULT_SQUARE_POINTS,
        patch_ratio: float = DEFAULT_POINT_PATCH_RATIO,
    ) -> None:
        if frame_w <= 0 or frame_h <= 0:
            raise ValueError("帧尺寸必须为正")
        self._grid = grid
        self._frame_w = int(frame_w)
        self._frame_h = int(frame_h)
        self._inner_ratio = max(0.0, float(inner_ratio))
        self._square_side = max(0.05, float(square_side))
        self._offset_y = float(offset_y)
        self._ring_points = max(1, int(ring_points))
        self._square_points = max(1, int(square_points))
        self._patch_ratio = max(0.0, float(patch_ratio))
        self._ring = self._inner_ratio > 0
        self._idx: list[tuple[np.ndarray, np.ndarray]] = [
            self._make_index(p) for p in grid.points
        ]

    # -- 只读属性 ---------------------------------------------------------- #

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
    def square_side(self) -> float:
        return self._square_side

    @property
    def offset_y(self) -> float:
        return self._offset_y

    @property
    def is_ring(self) -> bool:
        return self._ring

    @property
    def is_point_sampler(self) -> bool:
        """标记：UI/日志用它区分"点采样"与"整块均值"。"""
        return True

    @property
    def point_counts(self) -> tuple[int, int]:
        """`(环形点数, 方块每边点数)`。"""
        return (self._ring_points, self._square_points)

    def points_of(self, index: int) -> list[tuple[float, float]]:
        """第 `index` 个键的基准点（帧像素坐标，未加纵向偏移）。"""
        p = self._grid.points[index]
        cx, cy, r = p.to_pixels(self._frame_w, self._frame_h)
        return base_points(
            cx,
            cy + self._dy(),
            r,
            inner_ratio=self._inner_ratio,
            square_side=self._square_side,
            ring_points=self._ring_points,
            square_points=self._square_points,
        )

    # -- 内部 -------------------------------------------------------------- #

    def _dy(self) -> float:
        """纵向偏移（帧像素）：配置按 1920 宽存，按实际帧宽等比缩放。"""
        return self._offset_y * self._frame_w / REF_WIDTH

    def _make_index(self, p: KeyPoint) -> tuple[np.ndarray, np.ndarray]:
        cx, cy, r = p.to_pixels(self._frame_w, self._frame_h)
        cy += self._dy()
        pts = base_points(
            cx,
            cy,
            r,
            inner_ratio=self._inner_ratio,
            square_side=self._square_side,
            ring_points=self._ring_points,
            square_points=self._square_points,
        )
        patch = self._patch_ratio * r
        offsets: list[tuple[int, int]] = [(0, 0)]
        if patch >= 0.5:
            reach = int(math.floor(patch))
            for dy in range(-reach, reach + 1):
                for dx in range(-reach, reach + 1):
                    if (dx or dy) and math.hypot(dx, dy) <= patch:
                        offsets.append((dy, dx))

        ys: list[int] = []
        xs: list[int] = []
        for x, y in pts:
            px, py = int(round(x)), int(round(y))
            for dy, dx in offsets:
                yy, xx = py + dy, px + dx
                if 0 <= yy < self._frame_h and 0 <= xx < self._frame_w:
                    ys.append(yy)
                    xs.append(xx)
        if not ys:
            return (np.zeros(0, dtype=np.intp), np.zeros(0, dtype=np.intp))
        return (
            np.asarray(ys, dtype=np.intp),
            np.asarray(xs, dtype=np.intp),
        )

    # -- 采样 -------------------------------------------------------------- #

    def sample(self, frame: np.ndarray) -> np.ndarray:
        """返回 (K,3) uint8 RGB 均值；`frame` 必须是 HxWx3 BGR。"""
        h, w = frame.shape[:2]
        if (w, h) != (self._frame_w, self._frame_h):
            raise ValueError(
                f"帧尺寸变化: 期望 {self._frame_w}x{self._frame_h}，实际 {w}x{h}；"
                "请重建 KeyPointSampler"
            )
        out = np.zeros((len(self._idx), 3), dtype=np.uint8)
        for i, (ys, xs) in enumerate(self._idx):
            if ys.size == 0:
                continue
            pixels = frame[ys, xs]  # (M,3) BGR
            b, g, r = pixels.mean(axis=0)
            out[i] = (round(float(r)), round(float(g)), round(float(b)))
        return out

    def sample_tuple(self, frame: np.ndarray) -> tuple[tuple[int, int, int], ...]:
        arr = self.sample(frame)
        return tuple((int(v[0]), int(v[1]), int(v[2])) for v in arr)
