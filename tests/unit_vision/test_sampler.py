"""采样器：BGR→RGB、ROI 均值、尺寸校验。"""

from __future__ import annotations

import numpy as np
import pytest

from yqrec.types import KeyGrid, KeyPoint
from yqrec.vision.sampler import KeySampler


def _grid(centers: list[tuple[float, float]], w: int, h: int, r_px: float = 10) -> KeyGrid:
    pts = tuple(KeyPoint(u=x / w, v=y / h, r=r_px / 1920 * 1920 / w) for x, y in centers)
    return KeyGrid(points=pts, key_count=len(centers))


def test_sample_bgr_to_rgb():
    w, h = 200, 120
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    # 在 (50,60) 附近填充 B=10,G=20,R=30
    frame[50:70, 40:60] = (10, 20, 30)
    grid = _grid([(50, 60)], w, h, r_px=10)
    s = KeySampler(grid, w, h, stride=1)
    out = s.sample(frame)
    assert out.shape == (1, 3)
    assert tuple(int(v) for v in out[0]) == (30, 20, 10)


def test_sample_two_keys_and_dtype():
    w, h = 400, 200
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[40:80, 40:80] = (1, 2, 3)
    frame[100:140, 300:340] = (7, 8, 9)
    grid = _grid([(60, 60), (320, 120)], w, h, r_px=20)
    s = KeySampler(grid, w, h, stride=2)
    out = s.sample(frame)
    assert out.dtype == np.uint8
    assert tuple(int(v) for v in out[0]) == (3, 2, 1)
    assert tuple(int(v) for v in out[1]) == (9, 8, 7)


def test_edge_clamping():
    w, h = 100, 100
    frame = np.full((h, w, 3), 128, dtype=np.uint8)
    grid = _grid([(2, 2)], w, h, r_px=30)  # ROI 会越界
    s = KeySampler(grid, w, h, stride=1)
    out = s.sample(frame)
    assert tuple(int(v) for v in out[0]) == (128, 128, 128)


def test_frame_size_mismatch_raises():
    w, h = 100, 100
    grid = _grid([(50, 50)], w, h, r_px=10)
    s = KeySampler(grid, w, h)
    with pytest.raises(ValueError):
        s.sample(np.zeros((200, 200, 3), dtype=np.uint8))


def test_sample_tuple():
    w, h = 100, 100
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[40:60, 40:60] = (5, 6, 7)
    grid = _grid([(50, 50)], w, h, r_px=10)
    s = KeySampler(grid, w, h, stride=1)
    assert s.sample_tuple(frame) == ((7, 6, 5),)


def test_ring_sampling_ignores_the_center_icon():
    """外环采样必须绕开键帽中心：中心暗块不应拉低结果（这是 M2m 的核心动机）。"""
    w, h = 400, 400
    frame = np.full((h, w, 3), 200, dtype=np.uint8)
    # 中心 30px 半径画一个深色"音符图标"
    yy, xx = np.mgrid[0:h, 0:w]
    center = (xx - 200) ** 2 + (yy - 200) ** 2 <= 30**2
    frame[center] = (40, 40, 40)

    grid = _grid([(200, 200)], w, h, r_px=80)
    square = KeySampler(grid, w, h, stride=1).sample(frame)[0]
    ring = KeySampler(grid, w, h, stride=1, inner_ratio=0.55).sample(frame)[0]

    assert int(square[0]) < 190, "整块方形会被中心图标拉低"
    assert int(ring[0]) >= 198, f"外环应基本不受中心影响，实际 {int(ring[0])}"


def test_ring_mask_shape_matches_roi():
    """环形掩膜的尺寸必须与切片 ROI 一致（否则 sample 会静默退回整块）。"""
    w, h = 640, 360
    grid = _grid([(100, 100), (300, 200), (600, 350)], w, h, r_px=20)
    s = KeySampler(grid, w, h, stride=3, inner_ratio=0.5)
    assert s.inner_ratio == pytest.approx(0.5)
    for (sy, sx), mask in zip(s._slices, s._masks, strict=True):  # noqa: SLF001
        roi_h = len(range(sy.start, sy.stop, sy.step or 1))
        roi_w = len(range(sx.start, sx.stop, sx.step or 1))
        assert mask.shape == (roi_h, roi_w)
        assert mask.any(), "掩膜不能是空的（否则会退回整块采样）"


def test_ring_ratio_out_of_range_raises():
    w, h = 100, 100
    grid = _grid([(50, 50)], w, h, r_px=10)
    with pytest.raises(ValueError):
        KeySampler(grid, w, h, inner_ratio=0.99)


def test_offset_y_shifts_the_sampling_window():
    """纵向偏移：正数向下。上半亮、下半暗的帧里，正偏移应采到更暗的颜色。"""
    w, h = 400, 400
    frame = np.full((h, w, 3), 40, dtype=np.uint8)
    frame[:200] = 220  # 上半亮、下半暗，分界线在 y=200
    grid = _grid([(200, 200)], w, h, r_px=20)  # 圆心正好在分界线上

    # offset_y 以 1920 宽为基准：400 宽帧要 192 才能移动 40px
    shift = 40 * 1920 // w
    base = KeySampler(grid, w, h, stride=1).sample(frame)[0]
    down = KeySampler(grid, w, h, stride=1, offset_y=shift).sample(frame)[0]
    up = KeySampler(grid, w, h, stride=1, offset_y=-shift).sample(frame)[0]

    assert abs(int(base[0]) - 130) < 15, f"跨分界线应是中间值，实际 {int(base[0])}"
    assert int(down[0]) < 80, f"正偏移应落进下半（暗），实际 {int(down[0])}"
    assert int(up[0]) > 190, f"负偏移应落进上半（亮），实际 {int(up[0])}"


def test_offset_y_scales_with_frame_width():
    """偏移按 1920 宽归一：同一配置在窄帧上位移更小。"""
    def _shift(w: int, h: int) -> float:
        grid = _grid([(w // 2, h // 2)], w, h, r_px=int(w * 20 / 1920) or 8)
        return KeySampler(grid, w, h, stride=2, offset_y=192).offset_y * w / 1920

    assert _shift(1920, 1080) == pytest.approx(192.0)
    assert _shift(960, 540) == pytest.approx(96.0)


def test_square_side_controls_the_box_size():
    """方块边长 `square_side`（× R）：1R 时只采中间 1R 见方，2R 时回到旧口径。

    帧：圆心周围 1R 内是亮色，外层一圈（含 1R 方块的角）是暗色。
    边长 1R → 只采到亮色；边长 2R → 掺进暗色，均值明显变暗。
    """
    w = h = 400
    r_px = 40
    frame = np.full((h, w, 3), 30, dtype=np.uint8)
    yy, xx = np.ogrid[:h, :w]
    frame[(xx - 200) ** 2 + (yy - 200) ** 2 <= r_px**2] = 220
    grid = _grid([(200, 200)], w, h, r_px=r_px)

    one = KeySampler(grid, w, h, stride=1, inner_ratio=0.0, square_side=1.0).sample(frame)[0]
    two = KeySampler(grid, w, h, stride=1, inner_ratio=0.0, square_side=2.0).sample(frame)[0]

    assert int(one[0]) > 200, f"1R 方块应全落在亮圆内，实际 {int(one[0])}"
    assert int(two[0]) < int(one[0]) - 20, (
        f"2R 方块的四角吃到暗背景，应明显更暗：2R={int(two[0])} / 1R={int(one[0])}"
    )


def test_square_side_validation():
    grid = _grid([(50, 50)], 200, 120, r_px=10)
    with pytest.raises(ValueError):
        KeySampler(grid, 200, 120, inner_ratio=0.0, square_side=0.0)


def test_ring_mode_ignores_square_side():
    """环形模式走的是掩膜，`square_side` 不该影响结果（ROI 仍是 ±R）。"""
    w = h = 300
    frame = np.full((h, w, 3), 100, dtype=np.uint8)
    grid = _grid([(150, 150)], w, h, r_px=30)

    a = KeySampler(grid, w, h, stride=1, inner_ratio=0.55, square_side=1.0).sample(frame)[0]
    b = KeySampler(grid, w, h, stride=1, inner_ratio=0.55, square_side=2.0).sample(frame)[0]
    assert tuple(int(v) for v in a) == tuple(int(v) for v in b)
