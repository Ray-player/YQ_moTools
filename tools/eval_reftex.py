"""用 `assets/refTex` 参考图做**静态**识别准确度评估（M4 §3.4 / §7.4 的简化版）。

能回答什么、不能回答什么（先说清楚，别被数字骗）：

- ✅ 能答：给定"未按下"的逐键基线，这套 `weights` + `tolerance` + `delta` 能不能把参考图里
  **已经按下**的那几个键点亮、其余不点亮；还能扫出 **Δ 的安全窗口**（多小会误报、多大开始漏）。
- ❌ 不能答：时序精度、连击、按住时长、邻键光效干扰 —— 那要实机录像/实机操作，
  参考图只有一张静帧。

评估口径与运行时严格一致：

1. 键位：Hough 出圆 → 聚类 7 列 × 3 行 → 规则格点补全（参考图带白色键名方标，
   颜色法在上面必然失败，见 M4 §1.3/B2）；
2. 取色：`KeyPointSampler(0.80R × 8 点, patch 0.06R)`；
3. 基线：**逐键**。未按下的键用自身颜色，按下的键用**本行中位数**顶替
   （模拟"全部松开时学到的基线"）；
4. 判按：`thresholds_from_baseline()` + `PressDetector`（与 GUI 同一条路径，含
   `confirm_frames`），同一帧喂两遍以模拟 60fps 下按住不动；
5. 真值：人工核对过的按下键集合（见 `TRUTH`），不是由算法自己推的。

用法::

    python tools/eval_reftex.py                     # 跑全部参考图
    python tools/eval_reftex.py -i jhzq ljdsq       # 只看这两个
    python tools/eval_reftex.py --sweep             # 打印每个 Δ 的识别结果（找安全窗口）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import numpy as np  # noqa: E402
import reftex_colors as rc  # noqa: E402  复用取色/格点（同一口径，避免两份实现漂移）

from yqrec.instruments import load_instruments  # noqa: E402
from yqrec.timeline.detector import PressDetector  # noqa: E402
from yqrec.vision.thresholds import thresholds_from_baseline  # noqa: E402

TRUTH: dict[str, tuple[str, set[str]]] = {
    # 文件名 → (乐器 id, 人工核对的按下键)
    "ref_风物之诗琴.png": ("fwzs", {"Q", "F", "M"}),
    "ref_谐律键琴.png": ("xljq", {"Q", "F", "M"}),
    "ref_镜花之琴.png": ("jhzq", {"F", "Z"}),
    "ref_老旧的诗琴.png": ("ljdsq", {"Q", "F", "M"}),
    "ref_优可琴.png": ("ykq", {"F", "Z"}),
    "ref_优可琴sp2.png": ("ykq", {"Q"}),  # 和弦行（蓝盘）按下
    "ref_余音.png": ("yy", {"F", "Z"}),
    "ref_余音sp2.png": ("yy", {"Q", "U"}),  # 和弦行（紫盘）按下
}
"""参考图的真值。`sp/sp2` 是按下动画的另一帧，单独列（盘面颜色还没回弹到常态）。"""


def _keys_of(rgb: np.ndarray, press_drop: float = 25.0) -> set[str]:
    """按下键集合（仅用于**构造基线**时的占位，不作为真值）。"""
    pressed: set[str] = set()
    for row in range(3):
        block = rgb[row * 7 : (row + 1) * 7]
        base = np.median(block, axis=0)
        for col, key in enumerate(rc.KEYS[row]):
            if float((base - block[col]).max()) > press_drop:
                pressed.add(key)
    return pressed


def synthetic_baseline(rgb: np.ndarray, pressed: set[str]) -> np.ndarray:
    """模拟"全部松开时学到的逐键基线"：按下的键用本行中位数顶替。"""
    base = rgb.astype(float).copy()
    for row in range(3):
        block = rgb[row * 7 : (row + 1) * 7]
        row_median = np.median(block, axis=0)
        for col, key in enumerate(rc.KEYS[row]):
            if key in pressed:
                base[row * 7 + col] = row_median
    return base


def detect(
    spec, rgb: np.ndarray, baseline: np.ndarray, delta: float
) -> tuple[set[str], dict[str, float]]:
    """按 GUI 的路径判一遍：返回 (点亮集合, 逐键 score)。"""
    thresholds = thresholds_from_baseline(baseline, spec, delta)
    detector = PressDetector(spec, thresholds=thresholds, baseline=baseline)
    down: set[str] = set()
    for ts in (0.0, 1 / 60.0):  # confirm_frames=2 → 连续两帧
        for event in detector.update(rgb, ts):
            if event.down:
                down.add(event.key)
    detector.flush(1.0)
    # 逐键 score（thresholds 里的 base_* 是区域均值，这里换成逐键基线 —— 与
    # PressDetector 内部、以及 UI 键位卡显示的 Δ 数值同一口径）
    scores: dict[str, float] = {}
    for i, key in enumerate(spec.key_order):
        thr = rc_key_base(thresholds[spec.area_of_key(key).code], baseline[i])
        scores[key] = float(thr.weighted_score(tuple(int(v) for v in rgb[i])))
    return down, scores


def rc_key_base(thr, base_row):
    """把阈值的 `base_*` 换成逐键基线（与 `PressDetector` 内部同一步）。"""
    from dataclasses import replace

    return replace(
        thr,
        base_r=int(base_row[0]),
        base_g=int(base_row[1]),
        base_b=int(base_row[2]),
    )


def evaluate(path: Path, instrument: str, truth: set[str], *, sweep: bool = False) -> dict:
    specs = load_instruments()
    spec = specs[instrument]
    frame = rc.imread_unicode(path)
    rgb = rc.sample_image(frame, spec, spec.hough or rc.DEFAULT_HOUGH, 0.80)

    baseline = synthetic_baseline(rgb, _keys_of(rgb))
    delta = float(spec.detection.default_delta)
    down, scores = detect(spec, rgb, baseline, delta)

    tp = len(down & truth)
    fp = len(down - truth)
    fn = len(truth - down)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    pressed_scores = [scores[k] for k in sorted(truth)]
    unpressed_scores = [v for k, v in scores.items() if k not in truth]

    result = {
        "file": path.name,
        "instrument": instrument,
        "delta": delta,
        "truth": sorted(truth),
        "detected": sorted(down),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "min_pressed_score": min(pressed_scores) if pressed_scores else 0.0,
        "max_unpressed_score": max(unpressed_scores) if unpressed_scores else 0.0,
        "scores": scores,
    }
    if sweep:
        window = []
        for delta in range(4, 61):
            got, _ = detect(spec, rgb, baseline, float(delta))
            window.append((delta, got == truth, sorted(got)))
        result["sweep"] = window
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="refTex 静态识别准确度评估")
    parser.add_argument("-i", "--instrument", nargs="*", help="只评估这些乐器 id")
    parser.add_argument("-f", "--file", nargs="*", help="只评估这些图片文件")
    parser.add_argument("--sweep", action="store_true", help="扫 Δ=4..60，找安全窗口")
    parser.add_argument("--verbose", action="store_true", help="打印每个按下键的 score")
    args = parser.parse_args(argv)

    ref_dir = ROOT / "assets" / "refTex"
    rows: list[dict] = []
    for name, (instrument, truth) in TRUTH.items():
        if args.instrument and instrument not in args.instrument:
            continue
        if args.file and name not in args.file:
            continue
        path = ref_dir / name
        if not path.exists():
            continue
        rows.append(evaluate(path, instrument, truth, sweep=args.sweep))

    for item in rows:
        ok = "✅" if item["detected"] == item["truth"] else "❌"
        print(
            f"\n{ok} {item['file']:22s} {item['instrument']:5s} Δ={item['delta']}\n"
            f"   真值: {' '.join(item['truth']) or '(无)'}\n"
            f"   识别: {' '.join(item['detected']) or '(无)'}\n"
            f"   P={item['precision']:.2f} R={item['recall']:.2f} F1={item['f1']:.2f}"
            f"  (TP={item['tp']} FP={item['fp']} FN={item['fn']})\n"
            f"   score: 按下最小 {item['min_pressed_score']:.1f} / 未按下最大"
            f" {item['max_unpressed_score']:.1f}  → 余量"
            f" {item['min_pressed_score'] / max(item['delta'], 1):.1f}×"
        )
        if args.verbose:
            print("   " + "  ".join(f"{k}={v:.0f}" for k, v in sorted(item["scores"].items())))
        if args.sweep and "sweep" in item:
            good = [d for d, matched, _ in item["sweep"] if matched]
            if good:
                print(f"   Δ 安全窗口: {min(good)} ~ {max(good)}  （此范围内识别集合完全正确）")
                bad = [(d, got) for d, matched, got in item["sweep"] if not matched]
                if bad:
                    print(f"   首次失败: Δ={bad[0][0]} → {' '.join(bad[0][1]) or '(无)'}")

    if rows:
        avg = sum(r["f1"] for r in rows) / len(rows)
        print(f"\n合计 {len(rows)} 张图，平均 F1 = {avg:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
