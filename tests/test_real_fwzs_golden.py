"""Golden 回归：风物之诗琴的两张真实画面（按下都是键帽变青绿）。

| 夹具 | 来源 | 按下 |
|---|---|---|
| `fwzs_1069x598.png` | 红叶树背景，键帽不透明 | `-7`（M） |
| `fwzs_1695x956.jpg` | 金黄花海背景，键帽半透明 | `+3 / 1 / 2 / 5`（E / A / S / G） |

这张对照是关键：**同一个乐器、两种背景，绝对跌幅差一倍**（红树 69，花海 28），
所以 v1.20 起改用 `weighted`（三通道加权平均 + 死区）：红树得分 53、花海 33，
Δ=30 时两张图判出的按下集合都与真值**完全一致**。
注意花海裕度很薄（33 vs 30）—— 一旦把采样半径从 0.55R 调小到 0.40R，
得分会掉到 3~5 直接漏报，所以「半径」滑条改完必须重新学基线并看灯。

用例锁四件事：

1. `detect_keys_color` 在两张图上都必须检出 21 个键、半径接近 `button_radius`；
2. 采样口径与运行时一致（`KeySampler` + 全局 `sampling` 段推出的外环），判定走 `accepts()`；
3. Δ 取 JSON 里的默认值时，按下集合必须与地面真值完全一致（不误报、不漏报）；
4. 未按下键的指标值要明显低于 Δ（口径被放松时这条会先红）。

> **为什么要外环采样**：键帽中心画着音符/图标，按下时那部分像素几乎不变，只是稀释信号。
> 整块 2R 方形（含图标 + 四角背景）时，同一乐器两景的信噪比只有 4~12 倍；
> 只取 0.55R~0.98R 外环后，信号涨到 55~80、干扰降到 1~2，信噪比 27~55 倍。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from yqrec.instruments import load_instrument, load_sampling
from yqrec.vision.keygrid import detect_keys_color, keygrid_from_circles
from yqrec.vision.pointsampler import KeyPointSampler
from yqrec.vision.thresholds import _chroma_axis, thresholds_from_baseline

REAL_DIR = Path(__file__).parent / "data" / "real"
CASES = [
    pytest.param("fwzs_1069x598.png", {"-7"}, id="红树-不透明键帽"),
    pytest.param("fwzs_1695x956.jpg", {"+3", "1", "2", "5"}, id="花海-半透明键帽"),
]


def _imread_unicode(path: Path) -> np.ndarray:
    """`cv2.imread` 在 Windows 上读不了非 ASCII 路径，统一走 imdecode。"""
    cv2 = pytest.importorskip("cv2")
    frame = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None, f"读不动图片: {path}"
    return frame


def _samples(filename: str):
    """检出键位 → 按运行时口径采样 → 返回 (spec, {音名: rgb}, 半径)。"""
    spec = load_instrument("fwzs")
    frame = _imread_unicode(REAL_DIR / filename)
    height, width = frame.shape[:2]
    circles = detect_keys_color(frame, spec)
    assert len(circles) == spec.key_count, f"{filename}: 检出 {len(circles)} 个键"
    grid = keygrid_from_circles(circles, spec, width, height)
    sampling = load_sampling()
    # v1.20：运行时一律走分段点采样（内环圆周等角点），这里必须同口径
    rgb = KeyPointSampler(
        grid,
        width,
        height,
        inner_ratio=sampling.ring_inner_ratio,
        ring_points=sampling.ring_points,
        patch_ratio=sampling.point_patch_ratio,
    ).sample(frame)
    values = {
        note: tuple(int(v) for v in rgb[index])
        for index, note in enumerate(spec.note_order)
    }
    return spec, values, circles[0].r


def _threshold(spec, values, pressed, delta=None):
    base = np.median(
        np.asarray([values[n] for n in spec.note_order if n not in pressed], dtype=np.float32),
        axis=0,
    )
    return thresholds_from_baseline(
        np.tile(base, (spec.key_count, 1)), spec, delta or spec.detection.default_delta
    )["main"]


@pytest.mark.parametrize(("filename", "pressed"), CASES)
def test_detector_finds_21_keys(filename: str, pressed: set[str]) -> None:
    spec, _values, radius = _samples(filename)
    expected = spec.button_radius * 1094 / 1920  # 只做量级校验
    assert 0.6 * expected <= radius <= 1.8 * expected, radius
    assert spec.detection.metric == "weighted", "v1.20 起统一用加权判据"
    assert load_sampling().ring_inner_ratio > 0, "必须只采外环：中心音符会稀释信号"


@pytest.mark.parametrize(("filename", "pressed"), CASES)
def test_pressed_set_matches_ground_truth(filename: str, pressed: set[str]) -> None:
    """JSON 里的默认 Δ 必须同时满足：不漏报、不误报。"""
    spec, values, _radius = _samples(filename)
    threshold = _threshold(spec, values, pressed)
    assert threshold.mode == "weighted"

    got = {n for n in spec.note_order if threshold.accepts(values[n])}
    assert got == pressed, f"{filename}: 判定 {sorted(got)}，应 {sorted(pressed)}"


@pytest.mark.parametrize(("filename", "pressed"), CASES)
def test_unpressed_keys_stay_far_below_delta(filename: str, pressed: set[str]) -> None:
    """未按下键的得分要远小于 Δ（口径被放松 / 阈值被调低时这条先红）。"""
    spec, values, _radius = _samples(filename)
    threshold = _threshold(spec, values, pressed)
    delta = threshold.delta

    noise = [threshold.weighted_score(values[n]) for n in spec.note_order if n not in pressed]
    signal = [threshold.weighted_score(values[n]) for n in pressed]
    assert max(noise) < delta * 0.7, f"未按下最大得分 {max(noise)}（Δ={delta}）"
    assert min(signal) > delta * 1.05, f"按下最小得分 {min(signal)}（Δ={delta}）"


def test_chroma_axis_points_from_red_to_green() -> None:
    """方向必须从参考色推出来：风物之诗是"绿相对红变强"（R→G）。"""
    spec = load_instrument("fwzs")
    detection = spec.detection
    assert detection.unpressed_ref is not None and detection.pressed_ref is not None
    assert _chroma_axis(detection) == (0, 1)
