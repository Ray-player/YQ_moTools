"""Golden 回归：真实游戏截图（原神演奏界面 1696x957）—— **谐律键琴**（`xljq`）。

> M2i 更正：这张图原先被记成风物之诗，用户确认它是**谐律键琴**（棕盘外观）。
> 其 detection 参数已迁到 `xljq`，风物之诗改用 `fwzs_1069x598.png`（见
> `test_real_fwzs_golden.py`）。

这张图是用户在真机上截的，特征是：

- 21 个琴键（3 行 × 7 列）白盘，其中 **2 个已按下变棕**：上排第 2 个（音名 `+2` / 键 W）
  和中排第 7 个（音名 `7` / 键 J）；
- 背景有树、光效、歌词文字，Hough 会检出上百个假圆 —— 所以检测必须走颜色法。

用例锁三件事，防止算法被改坏：
1. `detect_keys_color` 必须返回 21 个圆；
2. 环采样（radius 26–38，避开中间图标）+ `max_drop` 阈值（Δ=40）判出的按下集合 == `{+2, 7}`；
3. 其余 19 个键的跌幅必须明显小（< 20），否则说明判定口径被放松了。

数据文件：`tests/data/real/lyre_1696x957.jpg`（勿删）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from yqrec.instruments import load_instrument
from yqrec.vision.keygrid import circle_map_note, detect_keys_color
from yqrec.vision.thresholds import thresholds_from_baseline

IMAGE = Path(__file__).parent / "data" / "real" / "lyre_1696x957.jpg"
R_IN = 26
R_OUT = 38
EXPECTED_PRESSED = {"+2", "7"}


def _score(threshold, rgb: tuple[int, int, int]) -> float:
    """按 JSON 里实际配的判据打分（v1.20 是 weighted）。"""
    if threshold.mode == "weighted":
        return float(threshold.weighted_score(rgb))
    if threshold.mode == "chroma":
        return float(threshold.chroma_shift(rgb))
    return float(threshold.max_drop(rgb))


def _score(threshold, rgb: tuple[int, int, int]) -> float:
    """按 JSON 里实际配的判据打分（v1.20 是 weighted）。"""
    if threshold.mode == "weighted":
        return float(threshold.weighted_score(rgb))
    if threshold.mode == "chroma":
        return float(threshold.chroma_shift(rgb))
    return float(threshold.max_drop(rgb))


def _ring_mean(frame: np.ndarray, cx: float, cy: float) -> tuple[int, int, int]:
    """环采样：`R_IN..R_OUT` 圆环均值（避开琴键中间的图标）。"""
    y0, y1 = max(0, int(cy - R_OUT)), min(frame.shape[0], int(cy + R_OUT) + 1)
    x0, x1 = max(0, int(cx - R_OUT)), min(frame.shape[1], int(cx + R_OUT) + 1)
    roi = frame[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    mask = (dist >= R_IN) & (dist <= R_OUT)
    pixels = roi[mask]
    if pixels.size == 0:
        return (0, 0, 0)
    blue, green, red = pixels.reshape(-1, 3).mean(axis=0)
    return (int(round(red)), int(round(green)), int(round(blue)))


@pytest.fixture(scope="module")
def lyre_frame() -> np.ndarray:
    cv2 = pytest.importorskip("cv2")
    if not IMAGE.exists():
        pytest.skip(f"缺少真机截图: {IMAGE}")
    frame = cv2.imread(str(IMAGE))
    assert frame is not None, f"读不动图片: {IMAGE}"
    return frame


def test_color_detector_finds_21_keys(lyre_frame: np.ndarray) -> None:
    spec = load_instrument("xljq")
    circles = detect_keys_color(lyre_frame, spec)

    assert len(circles) == spec.key_count, f"检出 {len(circles)} 个，应 {spec.key_count} 个"
    xs = sorted(round(c.x) for c in circles)
    ys = sorted(round(c.y) for c in circles)
    assert xs[0] > 300 and xs[-1] < lyre_frame.shape[1], f"列中心越界: {xs}"
    assert len(set(ys)) == 3, f"应有 3 行，实际行 y: {ys}"


def test_pressed_keys_match_ground_truth(lyre_frame: np.ndarray) -> None:
    spec = load_instrument("xljq")
    circles = detect_keys_color(lyre_frame, spec)
    assert len(circles) == spec.key_count

    mapping = circle_map_note(circles, spec)
    order = spec.note_order
    ring = np.asarray(
        [_ring_mean(lyre_frame, mapping[note].x, mapping[note].y) for note in order],
        dtype=np.float32,
    )

    # 基线：19 白 + 2 棕，取中位数即为"白盘"水平（无需额外的空白帧）
    baseline = np.tile(np.median(ring, axis=0), (len(order), 1))
    # Δ 从乐器 JSON 的 detection 段取（不硬编码），保证配置改了用例跟着走
    threshold = thresholds_from_baseline(
        baseline, spec, spec.detection.default_delta
    )["main"]
    assert threshold.mode == "weighted"

    drops = {note: _score(threshold, tuple(int(v) for v in ring[i])) for i, note in enumerate(order)}
    pressed = {note for note, drop in drops.items() if drop >= threshold.delta}

    assert pressed == EXPECTED_PRESSED, f"按下集合 {sorted(pressed)}，应 {sorted(EXPECTED_PRESSED)}"
    others = [drop for note, drop in drops.items() if note not in EXPECTED_PRESSED]
    assert max(others) < 20, f"未按下键的得分过大（{max(others)}），判定口径被放松了"


def test_square_geometry_still_separates(lyre_frame: np.ndarray) -> None:
    """方块模式（v1.20 = 四边取点、恒居中）在这张真图上仍能分辨按下。

    实测（Δ=40、weighted、谐律键琴）：边长 1R 时按下得分 48.7 / 63.4、未按下最大 7.9；
    把「中心到四边」放到 0.70R（边长 1.4R）后 47.4 / 50.2、未按下 0.0 ——
    两种半径都能判对，说明方块模式对半径不敏感（环形模式则很敏感，见 fwzs golden）。
    """
    from yqrec.instruments import load_sampling
    from yqrec.vision.keygrid import keygrid_from_circles
    from yqrec.vision.pointsampler import KeyPointSampler

    spec = load_instrument("xljq")
    sampling = load_sampling()
    circles = detect_keys_color(lyre_frame, spec)
    assert len(circles) == spec.key_count

    height, width = lyre_frame.shape[:2]
    grid = keygrid_from_circles(circles, spec, width, height)
    for half in (0.5, 0.7):
        rgb = KeyPointSampler(
            grid,
            width,
            height,
            inner_ratio=0.0,
            square_side=half * 2.0,
            square_points=sampling.square_points,
            patch_ratio=sampling.point_patch_ratio,
        ).sample(lyre_frame)

        order = spec.note_order
        baseline = np.tile(np.median(rgb, axis=0), (len(order), 1))
        threshold = thresholds_from_baseline(
            baseline, spec, spec.detection.default_delta
        )["main"]

        drops = {
            note: _score(threshold, tuple(int(v) for v in rgb[i]))
            for i, note in enumerate(order)
        }
        pressed = {note for note, drop in drops.items() if drop >= threshold.delta}
        assert pressed == EXPECTED_PRESSED, (
            f"半边长 {half}R：按下集合 {sorted(pressed)}，应 {sorted(EXPECTED_PRESSED)}"
        )

        noise = [d for n, d in drops.items() if n not in EXPECTED_PRESSED]
        assert max(noise) < threshold.delta * 0.6, (
            f"半边长 {half}R：未按下最大得分 {max(noise)}（Δ={threshold.delta}）"
        )

