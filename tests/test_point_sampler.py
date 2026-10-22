"""v1.20 新算法：分段点采样 + 加权判据 + 逐键基线 + 半径写回。

这些用例锁的是"改成新算法后不能悄悄退回去"的几件事：
1. 点采样的几何（环形起点在正上方、方块起点在左上角、点数 = 4 × 每边点数）；
2. 加权判据的三条语义（只算变暗、死区、权重归一化）；
3. `PressDetector` 给每个键用它自己的基线（区域均值会把被遮挡的键判成常按）；
4. 「半径」滑条把值写回 `instruments.json` 且不破坏文件其它内容。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from yqrec.instruments import (
    instruments_path,
    load_instrument,
    load_sampling,
    parse_detection,
    update_sampling,
)
from yqrec.timeline.detector import DetectorConfig, PressDetector
from yqrec.types import (
    DEFAULT_POINT_PATCH_RATIO,
    DEFAULT_RING_POINTS,
    DEFAULT_SQUARE_POINTS,
    KeyGrid,
    KeyPoint,
    KeyThreshold,
)
from yqrec.vision.pointsampler import (
    KeyPointSampler,
    base_points,
    ring_base_points,
    square_base_points,
)


def _grid(keys: int = 21) -> KeyGrid:
    pts = tuple(KeyPoint(u=0.1 + 0.04 * i, v=0.8, r=50 / 1920) for i in range(keys))
    return KeyGrid(points=pts, key_count=keys)


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #


def test_ring_points_start_at_top_and_go_clockwise() -> None:
    pts = ring_base_points(100.0, 100.0, 40.0, inner_ratio=0.5, count=8)
    assert len(pts) == 8
    x0, y0 = pts[0]
    assert x0 == pytest.approx(100.0, abs=1e-6), "起点应在正上方（x = 圆心）"
    assert y0 == pytest.approx(100.0 - 20.0, abs=1e-6), "半径 = 0.5R = 20px"
    # 顺时针：第 2 个点在右上
    x1, y1 = pts[1]
    assert x1 > 100.0 and y1 < 100.0


def test_square_points_start_at_topleft_and_walk_all_four_edges() -> None:
    pts = square_base_points(100.0, 100.0, 40.0, square_side=1.0, count=4)
    assert len(pts) == 16, "4 条边 × 每边 4 个点"
    assert pts[0] == pytest.approx((80.0, 80.0)), "起点 = 左上角（方块半边长 0.5R = 20px）"
    assert pts[1][0] > pts[0][0] and pts[1][1] == pytest.approx(80.0), "上边向右"
    assert pts[4] == pytest.approx((120.0, 80.0)), "第 5 个点换到右边起点"
    assert max(y for _x, y in pts) == pytest.approx(120.0), "下边在 cy + half"
    assert min(x for x, _y in pts) == pytest.approx(80.0), "左边在 cx - half"


def test_square_is_always_centered_and_radius_moves_all_four_edges() -> None:
    side1 = square_base_points(0.0, 0.0, 10.0, square_side=1.0, count=4)
    side2 = square_base_points(0.0, 0.0, 10.0, square_side=2.0, count=4)
    assert max(abs(x) for x, _y in side1) == pytest.approx(5.0)
    assert max(abs(x) for x, _y in side2) == pytest.approx(10.0), "半边长翻倍=更远"
    # 四条边到中心的距离都是 half（居中），点落在边上的离散位置不影响这一点
    assert max(abs(y) for _x, y in side2) == pytest.approx(10.0)
    assert max(abs(x) for x, _y in side2) == pytest.approx(10.0)
    assert all(abs(x) <= 10.0 + 1e-9 and abs(y) <= 10.0 + 1e-9 for x, y in side2)


def test_base_points_switches_geometry_by_inner_ratio() -> None:
    ring = base_points(50.0, 50.0, 20.0, inner_ratio=0.5, ring_points=12)
    square = base_points(50.0, 50.0, 20.0, inner_ratio=0.0, square_points=3)
    assert len(ring) == 12
    assert len(square) == 12


# --------------------------------------------------------------------------- #
# 采样
# --------------------------------------------------------------------------- #


def test_point_sampler_averages_patch_and_returns_rgb() -> None:
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    frame[:, :] = (10, 20, 30)  # BGR → RGB (30, 20, 10)
    grid = KeyGrid(points=(KeyPoint(u=0.5, v=0.5, r=40 / 200),), key_count=1)
    sampler = KeyPointSampler(
        grid, 200, 200, inner_ratio=0.6, ring_points=8, patch_ratio=DEFAULT_POINT_PATCH_RATIO
    )
    rgb = sampler.sample(frame)
    assert rgb.shape == (1, 3)
    assert tuple(int(v) for v in rgb[0]) == (30, 20, 10), "BGR → RGB 顺序要对"


def test_point_sampler_rejects_frame_size_change() -> None:
    grid = _grid(3)
    sampler = KeyPointSampler(grid, 200, 200, inner_ratio=0.5)
    with pytest.raises(ValueError):
        sampler.sample(np.zeros((201, 200, 3), dtype=np.uint8))


def test_point_sampler_clips_out_of_frame_points() -> None:
    grid = KeyGrid(points=(KeyPoint(u=0.01, v=0.01, r=200 / 300),), key_count=1)
    frame = np.full((100, 300, 3), 128, dtype=np.uint8)
    rgb = KeyPointSampler(grid, 300, 100, inner_ratio=0.9, ring_points=16).sample(frame)
    assert tuple(int(v) for v in rgb[0]) == (128, 128, 128), "越界点被裁掉，不能报错"


def test_point_sampler_exposes_counts_and_origin() -> None:
    sampler = KeyPointSampler(
        _grid(2), 1920, 1080, inner_ratio=0.55, ring_points=24, square_points=5
    )
    assert sampler.is_point_sampler is True
    assert sampler.point_counts == (24, 5)
    assert len(sampler.points_of(0)) == 24


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #


def _weighted(**kwargs) -> KeyThreshold:
    base = dict(
        mode="weighted",
        base_r=200,
        base_g=200,
        base_b=200,
        delta=40,
        weights=(0.5, 0.3, 0.2),
        tolerance=8,
    )
    base.update(kwargs)
    return KeyThreshold(**base)  # type: ignore[arg-type]


def test_weighted_score_only_counts_darkening() -> None:
    thr = _weighted()
    assert thr.weighted_score((120, 120, 120)) == pytest.approx(72.0), "200-120=80，减死区 8"
    assert thr.weighted_score((240, 240, 240)) == 0.0, "变亮不算按下"


def test_weighted_score_dead_zone_swallows_small_deviations() -> None:
    thr = _weighted()
    assert thr.weighted_score((195, 195, 195)) == 0.0, "跌幅 5 < 死区 8 → 0"
    assert thr.weighted_score((192, 192, 192)) == pytest.approx(0.0)
    assert thr.weighted_score((188, 188, 188)) == pytest.approx(4.0), "(12-8)=4"


def test_weighted_score_respects_channel_weights() -> None:
    thr = _weighted(weights=(1.0, 0.0, 0.0))
    assert thr.weighted_score((120, 200, 200)) == pytest.approx(72.0), "只有 R 有权重"
    assert thr.weighted_score((200, 120, 120)) == pytest.approx(0.0)


def test_weighted_score_ignores_channels_without_baseline() -> None:
    thr = _weighted(base_b=-1, weights=(0.5, 0.5, 0.0))
    score = thr.weighted_score((120, 120, 0))
    assert score == pytest.approx(72.0), "B 没有基线 → 不参与，权重按有效通道归一"


def test_accepts_uses_delta_for_weighted() -> None:
    thr = _weighted()
    assert thr.accepts((150, 150, 150)) is True  # 42 ≥ 40
    assert thr.accepts((170, 170, 170)) is False  # 22 < 40


def test_parse_detection_reads_weights_and_tolerance() -> None:
    spec = parse_detection(
        {"metric": "weighted", "weights": [0.7, -0.2, 0.5], "tolerance": 12}, "demo"
    )
    assert spec.metric == "weighted"
    assert spec.weights == pytest.approx((0.7, 0.0, 0.5)), "负权重按 0 处理"
    assert spec.tolerance == 12

    bad = parse_detection({"metric": "weighted", "weights": [1, 2], "tolerance": 999}, "demo")
    assert bad.weights is None and bad.tolerance == 8, "非法值回落"


# --------------------------------------------------------------------------- #
# 逐键基线
# --------------------------------------------------------------------------- #


def test_detector_uses_per_key_baseline() -> None:
    """两个键颜色差 60：用自己的基线时都不判按下，用区域均值时暗的那个会常亮。"""
    spec = load_instrument("xlyq")
    keys = spec.key_order
    baseline = np.full((len(keys), 3), 240.0, dtype=np.float32)
    baseline[0] = (160.0, 160.0, 160.0)  # 第 0 键被角色挡住，本身就暗

    thresholds = {
        area.code: KeyThreshold(
            mode="weighted",
            base_r=240,
            base_g=240,
            base_b=240,
            delta=20,
            weights=(1 / 3, 1 / 3, 1 / 3),
            tolerance=0,
        )
        for area in spec.areas
    }
    frame_rgb = np.asarray(baseline, dtype=np.float32)  # 全部"未按下"

    # 默认 min_hold=0（单帧即确认），喂两帧只为覆盖两种状态
    detector = PressDetector(spec, thresholds, baseline=baseline, cfg=DetectorConfig())
    assert detector.update(frame_rgb, 0.0) == []
    assert detector.update(frame_rgb, 0.05) == [], "各键对各自基线 → 没有任何按下"

    naive = PressDetector(spec, thresholds, cfg=DetectorConfig())
    events = naive.update(frame_rgb, 0.0) + naive.update(frame_rgb, 0.05)
    assert [e.key for e in events] == [keys[0]], "用区域均值时暗键会被误判为按下"


def test_detector_falls_back_when_baseline_shape_is_wrong() -> None:
    spec = load_instrument("xlyq")
    thresholds = {
        area.code: KeyThreshold(mode="weighted", base_r=10, base_g=10, base_b=10, delta=1)
        for area in spec.areas
    }
    detector = PressDetector(spec, thresholds, baseline=[[1, 2, 3]], cfg=DetectorConfig())
    assert detector.update(np.full((spec.key_count, 3), 10), 0.0) == []


# --------------------------------------------------------------------------- #
# 半径写回 instruments.json
# --------------------------------------------------------------------------- #


@pytest.fixture()
def temp_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    src = instruments_path()
    dst = tmp_path / "assets"
    dst.mkdir()
    shutil.copyfile(src, dst / "instruments.json")
    monkeypatch.setenv("YQREC_ASSETS", str(dst))
    return dst / "instruments.json"


def test_update_sampling_writes_inner_radius_without_touching_layout(temp_assets: Path) -> None:
    before = temp_assets.read_text(encoding="utf-8")
    assert update_sampling(ring_width_ratio=0.36) is True

    after = temp_assets.read_text(encoding="utf-8")
    assert '"ring_width_ratio": 0.36' in after
    assert after.count('"_comment"') == before.count('"_comment"'), "注释段必须原样保留"
    assert len(after.splitlines()) == len(before.splitlines()), "行数不变（不是整份重排）"

    sampling = load_sampling()
    assert sampling.ring_inner_ratio == pytest.approx(0.62)


def test_update_sampling_writes_square_half_side(temp_assets: Path) -> None:
    assert update_sampling(square_side_ratio=1.4) is True
    assert load_sampling().square_half_ratio == pytest.approx(0.7)


def test_update_sampling_is_noop_when_value_unchanged(temp_assets: Path) -> None:
    assert update_sampling(ring_width_ratio=0.18) is False
    assert update_sampling() is False


def test_instruments_json_keeps_all_instruments_after_write(temp_assets: Path) -> None:
    update_sampling(ring_width_ratio=0.40)
    data = json.loads(temp_assets.read_text(encoding="utf-8"))
    assert [i["id"] for i in data["instruments"]] == [
        "fwzs",
        "xlyq",
        "jhzq",
        "ljdsq",
        "ykq",
        "yy",
    ]
    assert data["version"] >= 4


def test_default_point_counts_are_stable() -> None:
    assert DEFAULT_RING_POINTS == 8 and DEFAULT_SQUARE_POINTS == 4
    sampling = load_sampling()
    assert sampling.ring_points >= 6 and sampling.square_points >= 3
    assert sampling.ring_inner_ratio == pytest.approx(0.80), "默认内环 0.80R"
    assert sampling.square_half_ratio == pytest.approx(0.55), "默认方块半边长 0.55R"
