"""键位检测/网格校正/圆→音映射。"""

from __future__ import annotations

import random

import pytest

from yqrec.errors import KeyGridMismatch
from yqrec.types import Circle
from yqrec.vision.keygrid import (
    circle_map_note,
    correct_button_coordinates,
    keygrid_from_circles,
    keygrid_from_note_map,
    keygrid_from_points,
)


def _synthetic_circles(
    *, rows: int = 3, cols: int = 7, x0: float = 100, y0: float = 200,
    dx: float = 50, dy: float = 40, r: float = 20, noise: float = 0.0,
) -> list[Circle]:
    rng = random.Random(42)
    out = []
    for row in range(rows):
        for col in range(cols):
            out.append(
                Circle(
                    x=x0 + col * dx + rng.uniform(-noise, noise),
                    y=y0 + row * dy + rng.uniform(-noise, noise),
                    r=r,
                )
            )
    return out


def test_correct_alignment_and_order():
    circles = _synthetic_circles(noise=3)
    random.Random(0).shuffle(circles)
    fixed = correct_button_coordinates(circles)
    assert len(fixed) == 21

    # 输出：先最下面一排；行内 X 升序；行内 Y 相等；跨行同列 X 相等
    ys = [c.y for c in fixed]
    xs = [c.x for c in fixed]
    for i in range(3):
        row_y = ys[i * 7 : (i + 1) * 7]
        assert max(row_y) - min(row_y) < 1e-6
        row_x = xs[i * 7 : (i + 1) * 7]
        assert row_x == sorted(row_x)
    # 第三行（最上）的 y 小于第一行
    assert ys[0] > ys[7] > ys[14]
    # 同列 X 相等
    for col in range(7):
        assert abs(xs[col] - xs[7 + col]) < 1e-6


def test_correct_passthrough_when_not_multiple_of_seven():
    circles = _synthetic_circles(rows=2, cols=7)[:10]
    assert correct_button_coordinates(circles) == circles


def test_correct_passthrough_single_row():
    circles = _synthetic_circles(rows=1, cols=7)
    assert correct_button_coordinates(circles) == circles


def test_circle_map_note_orders_top_row_as_high_octave(spec_fwzs):
    circles = _synthetic_circles()
    mapping = circle_map_note(circles, spec_fwzs)
    assert set(mapping) == set(spec_fwzs.note_order)
    # 最上排 = +1..+7，且 +1 在 +7 左侧
    assert mapping["+1"].y < mapping["1"].y < mapping["-1"].y
    assert mapping["+1"].x < mapping["+7"].x
    # 下排最低（y 最大）
    assert mapping["-7"].y > mapping["7"].y


def test_circle_map_note_mismatch_raises(spec_fwzs):
    with pytest.raises(KeyGridMismatch):
        circle_map_note(_synthetic_circles()[:20], spec_fwzs)


def test_keygrid_from_circles_is_note_ordered(spec_fwzs):
    grid = keygrid_from_circles(_synthetic_circles(), spec_fwzs, 1600, 900)
    assert len(grid.points) == 21
    # +1..+7 在上排（v 最小）
    assert grid.points[0].v < grid.points[7].v < grid.points[14].v
    # 半径按 1920 归一化（取乐器 JSON 的 button_radius，不硬编码）
    assert abs(grid.points[0].r - spec_fwzs.button_radius / 1920) < 1e-9


def test_keygrid_from_note_map_roundtrip(spec_fwzs):
    circles = _synthetic_circles()
    mapping = circle_map_note(circles, spec_fwzs)
    grid = keygrid_from_note_map(mapping, spec_fwzs, 1000, 500)
    assert grid.points[0].u == pytest.approx(mapping["+1"].x / 1000)
    assert grid.points[0].v == pytest.approx(mapping["+1"].y / 500)


def test_keygrid_from_points_manual_fallback(spec_fwzs):
    pts = [(c.x, c.y) for c in _synthetic_circles()]
    grid = keygrid_from_points(pts, spec_fwzs, 1920, 1080)
    assert grid.key_count == 21
    assert grid.points[0].v < grid.points[-1].v
