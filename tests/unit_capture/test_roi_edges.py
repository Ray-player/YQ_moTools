"""`roi_from_norm` 边界补充用例（P1 · printwindow 纯函数）。

工程师主用例覆盖了整窗/半窗/四舍五入/1px 下限/非法尺寸；本文件补**钳制与不变量**：

- 归一化区间越界（负数 / >1）必须被 clamp 回客户区内，而不是切出负数下标；
- 反转区间（u1 < u0）不得产出 0 宽高（numpy 切片会静默变空图）；
- 任意输入组合的通用不变量：`x >= 0`、`x + w <= width`、`w,h >= 1`。
"""

from __future__ import annotations

import pytest

from yqrec.capture.printwindow_backend import roi_from_norm

NORMS = [
    (0.0, 0.0, 1.0, 1.0),
    (-0.5, -0.5, 1.5, 1.5),
    (0.0, 0.0, 2.0, 2.0),
    (-1.0, -1.0, -0.5, -0.5),
    (0.5, 0.5, 2.0, 2.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.9, 0.9, 0.1, 0.1),
    (1.0, 1.0, 1.0, 1.0),
    (0.999, 0.999, 1.0, 1.0),
]

SIZES = [(1, 1), (100, 50), (640, 360), (1920, 1080)]


def test_roi_from_norm_clamps_out_of_range_norm() -> None:
    """`(u0,v0,u1,v1)` 越界 → clamp 成整窗，不得出现负下标或超界宽高。"""
    assert roi_from_norm(100, 50, (-0.5, -0.5, 1.5, 1.5)) == (0, 0, 100, 50)
    assert roi_from_norm(100, 50, (0.0, 0.0, 2.0, 2.0)) == (0, 0, 100, 50)
    assert roi_from_norm(100, 50, (-1.0, -1.0, -0.5, -0.5)) == (0, 0, 1, 1)


def test_roi_from_norm_inverted_norm_keeps_one_pixel() -> None:
    """反转区间（u1 < u0）最少 1px：宽高为 0 会让 `buf[y0:y1, x0:x1]` 静默变空图。"""
    x, y, w, h = roi_from_norm(100, 100, (0.9, 0.9, 0.1, 0.1))
    assert (w, h) == (1, 1)
    assert (x, y) == (90, 90)


def test_roi_from_norm_degenerate_norm_stays_in_bounds() -> None:
    """`(0,0,0,0)`：左上角仍至少 1px，且不越界。"""
    assert roi_from_norm(100, 100, (0.0, 0.0, 0.0, 0.0)) == (0, 0, 1, 1)


@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("norm", NORMS)
def test_roi_invariants_hold_for_any_norm(
    width: int, height: int, norm: tuple[float, float, float, float]
) -> None:
    """通用不变量：可直接拿去切 numpy 缓冲，永不越界、永不为空。"""
    x, y, w, h = roi_from_norm(width, height, norm)
    assert 0 <= x < width, (width, height, norm, (x, y, w, h))
    assert 0 <= y < height, (width, height, norm, (x, y, w, h))
    assert w >= 1 and h >= 1, (width, height, norm, (x, y, w, h))
    assert x + w <= width, (width, height, norm, (x, y, w, h))
    assert y + h <= height, (width, height, norm, (x, y, w, h))


def test_roi_matches_numpy_slice_shape() -> None:
    """宽度/高度语义必须与 numpy 切片一致（`w = x1 - x0`，不是归一化宽度）。"""
    import numpy as np

    buf = np.zeros((36, 64, 4), dtype=np.uint8)
    for norm in NORMS[:4]:
        x, y, w, h = roi_from_norm(64, 36, norm)
        piece = buf[y : y + h, x : x + w, :3]
        assert piece.shape == (h, w, 3), (norm, piece.shape, (x, y, w, h))
        assert piece.size > 0
