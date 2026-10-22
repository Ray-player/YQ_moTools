"""镜花之琴 / 老旧的诗琴：refTex 参考图的"静态识别准确度"金标（M4 §7.4 简化版）。

测的是**配置**：给定"全部松开时学到的逐键基线"，当前 `weights` / `tolerance` / `delta`
能不能把参考图里已经按下的那几个键点亮、其余不点亮。口径与运行时一致
（环形 0.80R×8 点、`thresholds_from_baseline` + `PressDetector`）。

> 参考图 `assets/refTex/` 在 `.gitignore` 里（游戏截图，5MB+）；文件缺失时本组用例
> **跳过**而不是失败。要跑就在本机放回这 10 张图，然后 `pytest tests/test_real_reftex_golden.py`。
> 它不能替代实机验证：静帧测不出时序、连击与邻键光效干扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REF_DIR = ROOT / "assets" / "refTex"

sys.path.insert(0, str(ROOT / "tools"))
import eval_reftex as ev  # noqa: E402  （复用取色/格点/判按，避免两份口径漂移）

CASES = ["ref_镜花之琴.png", "ref_老旧的诗琴.png"]

pytestmark = pytest.mark.skipif(
    not REF_DIR.exists(), reason="assets/refTex 未入库（.gitignore），跳过静态准确度金标"
)


@pytest.mark.parametrize("name", CASES)
def test_reftex_detection_matches_ground_truth(name: str) -> None:
    """按下键必须**不多不少**被点亮（P=R=F1=1）。"""
    instrument, truth = ev.TRUTH[name]
    item = ev.evaluate(REF_DIR / name, instrument, truth)
    assert item["detected"] == sorted(truth), (
        f"{name}: 识别 {item['detected']} ≠ 真值 {sorted(truth)}"
    )
    assert item["fp"] == 0 and item["fn"] == 0


@pytest.mark.parametrize("name", CASES)
def test_reftex_margin_is_at_least_two_x(name: str) -> None:
    """按下键的 score 至少是 Δ 的 2 倍；未按下的键远低于 Δ（参考图上是 0）。"""
    instrument, truth = ev.TRUTH[name]
    item = ev.evaluate(REF_DIR / name, instrument, truth)
    assert item["min_pressed_score"] >= 2.0 * item["delta"], item
    assert item["max_unpressed_score"] < item["delta"] * 0.5, item


@pytest.mark.parametrize("name", CASES)
def test_reftex_delta_has_wide_safe_window(name: str) -> None:
    """Δ 的安全窗口要够宽（实机噪声/皮肤差异会吃掉一部分余量）。"""
    instrument, truth = ev.TRUTH[name]
    item = ev.evaluate(REF_DIR / name, instrument, truth, sweep=True)
    good = [delta for delta, matched, _ in item["sweep"] if matched]
    assert good, f"{name}: 没有任何 Δ 能完全识别"
    assert min(good) <= 10, f"{name}: Δ<10 就开始漏 → 太脆"
    assert max(good) >= 30, f"{name}: Δ>30 就漏 → 余量不够"


@pytest.mark.parametrize("instrument", ["jhzq", "ljdsq"])
def test_refs_are_filled(instrument: str) -> None:
    """这两个乐器不能再是"占位参数"：refs 必须回填（weights 由它归一化）。"""
    from yqrec.instruments import load_instrument

    spec = load_instrument(instrument)
    assert spec.detection.unpressed_ref is not None, "unpressed_ref 未回填"
    assert spec.detection.pressed_ref is not None, "pressed_ref 未回填"
    # weights 与 refs 的跌幅方向一致（同一口径算出来的，别飘）
    drops = [
        max(0, a - b)
        for a, b in zip(spec.detection.unpressed_ref, spec.detection.pressed_ref, strict=True)
    ]
    total = sum(drops)
    weights = spec.detection.weights or ()
    for weight, drop in zip(weights, drops, strict=True):
        assert abs(weight - drop / total) < 0.05, f"{instrument}: weights 与 refs 不自洽"
