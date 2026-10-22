"""检测算法对比：整块均值（现状）vs 分段点采样 + 加权判据（M3b 候选）。

数据集：`assets/refTex/*.png`（10 张真机截图，6 个乐器，含 3 个不同机位）。
真值：`out/refTex/calib_ref_*.json` 的 `pressed`（已与截图人工核对：
      风物之诗 = Q/F/M，与图上青绿键一致）。

对比维度（**同一张图、同一套网格、同一个基线**，只差采样几何 + 判据）：
  1. `annulus+max_drop`   现状默认（0.55R~0.98R 环带均值 + 三通道最大跌幅）
  2. `annulus+chroma`     现状 fwzs 用的判据
  3. `square1R+max_drop`  现状另一档检测方式
  4. `point_ring+max_drop`   只换采样（隔离变量）
  5. `point_ring+weighted`   换采样 + 换判据（候选方案）
  6. `point_square+weighted`
  7. `annulus+weighted`      只换判据（隔离变量）

指标（Δ 无关，跨方法可比）：
  signal_min = 按下键里的最小得分      noise_max = 未按下键里的最大得分
  SNR        = signal_min / max(noise_max, 0.5)
  Δ 窗口宽窄 = 0.6·signal_min / (1.5·noise_max)（>1 才存在合法 Δ）

用法：.venv/Scripts/python.exe tools/compare_detectors.py [--ring-points 16] [--json out/x.json]

> 口径说明：本脚本为了"只差采样几何 + 判据"这一件事，给所有方法用**同一个 area 均值基线**。
> 从 v1.20 起运行时实际用的是**逐键基线**（`PressDetector(baseline=...)`），它会额外消掉
> "键与键之间颜色不同"（半透明键帽被角色压暗、背景渐变）这一大块误差 —— 所以这里的
> `noise_max` 对两种方法都偏悲观，适合横向比判据，不要当成运行时绝对值。
> 运行时验收以 `tests/test_real_*_golden.py`（真图 + 地面真值）为准。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yqrec.instruments import load_instruments, load_sampling  # noqa: E402
from yqrec.types import KeyGrid, KeyPoint, KeyThreshold  # noqa: E402
from yqrec.vision.pointsampler import KeyPointSampler  # noqa: E402
from yqrec.vision.sampler import KeySampler  # noqa: E402
from yqrec.vision.thresholds import _chroma_axis, press_weights  # noqa: E402

REF_DIR = ROOT / "assets" / "refTex"
CALIB_DIR = ROOT / "out" / "refTex"
NOTES = [f"+{i}" for i in range(1, 8)] + [str(i) for i in range(1, 8)] + [
    f"-{i}" for i in range(1, 8)
]

IMAGES: list[tuple[str, str, str]] = [
    ("ref_风物之诗琴.png", "fwzs", "calib_ref_fwzs.json"),
    ("ref_谐律键琴.png", "xljq", "calib_ref_xljq.json"),
    ("ref_镜花之琴.png", "jhzq", "calib_ref_jhzq.json"),
    ("ref_老旧的诗琴.png", "ljdsq", "calib_ref_ljdsq.json"),
    ("ref_优可琴.png", "ykq", "calib_ref_ykq.json"),
    ("ref_优可琴sp.png", "ykq", "calib_ref_ykq_sp.json"),
    ("ref_优可琴sp2.png", "ykq", "calib_ref_ykq_sp2.json"),
    ("ref_余音.png", "yy", "calib_ref_yy.json"),
    ("ref_余音sp.png", "yy", "calib_ref_yy_sp.json"),
    ("ref_余音sp2.png", "yy", "calib_ref_yy_sp2.json"),
]
"""`(图, 乐器 id, 标定 JSON)`。sp/sp2 是同一乐器不同机位/背景。"""


# --------------------------------------------------------------------------- #
# 网格拟合（与 out/calib_refs.py 同源：亮盘掩膜 → 列投影 → 行二维搜索）
# --------------------------------------------------------------------------- #


def imread_u(path: Path) -> np.ndarray:
    """Windows 下中文/空格路径安全的 imread。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


def _runs(profile: np.ndarray, thr: float) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    start = None
    for i, v in enumerate(profile):
        if v > thr and start is None:
            start = i
        elif v <= thr and start is not None:
            out.append((start, i - 1, i - start))
            start = None
    if start is not None:
        out.append((start, len(profile) - 1, len(profile) - start))
    return out


def find_grid(img: np.ndarray, r_hint: float) -> tuple[list[float], list[float], float]:
    """返回 `(col_x[7], row_y[3], r_px)`。"""
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hh, ss, vv = (hsv[:, :, i].astype(np.int16) for i in range(3))
    white = (ss < 45) & (vv > 190)
    brown = (hh > 8) & (hh < 35) & (ss > 45) & (ss < 170) & (vv > 110) & (vv < 250)
    mask = cv2.morphologyEx((white | brown).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask[: int(0.45 * h), :] = 0

    band = mask.copy()
    band[: int(0.5 * h), :] = 0
    col_profile = band.sum(axis=0)
    segs = [r for r in _runs(col_profile, max(8, 0.35 * r_hint)) if r[2] > r_hint]
    segs = [s for s in segs if 0.9 * r_hint <= s[2] <= 3.2 * r_hint]
    centers = [float((a + b) / 2) for a, b, _c in segs]
    widths = [float(c) for _a, _b, c in segs]
    if len(centers) < 3:
        raise RuntimeError(f"列数不足: {centers}")

    step = float(np.median(np.diff(centers)))
    seed = centers[0]
    for i in range(len(centers) - 2):
        if abs((centers[i + 2] - centers[i]) - 2 * step) <= 0.15 * step:
            seed = centers[i]
            break
    cand: list[float] = []
    for k in range(-7, 8):
        x = seed + k * step
        if 0 < x < w:
            near = min(centers, key=lambda c: abs(c - x))
            cand.append(near if abs(near - x) <= 0.2 * step else x)
    cand = sorted({round(v, 1) for v in cand})
    best7: list[float] | None = None
    best_hits = -1
    for i in range(len(cand) - 6):
        win = cand[i : i + 7]
        if all(abs(v - step) <= 0.15 * step for v in np.diff(win)):
            hits = sum(1 for v in win if abs(min(centers, key=lambda c: abs(c - v)) - v) < 2)
            if hits > best_hits:
                best_hits, best7 = hits, win
    col_x = list(best7) if best7 is not None else [seed + i * step for i in range(7)]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rr = float(np.median(widths)) / 2.0
    size = int(2 * 0.90 * rr) + 3
    yy, xx = np.mgrid[0:size, 0:size]
    c = (yy.shape[0] - 1) / 2.0
    d = np.hypot(xx - c, yy - c) / rr
    ring = (d >= 0.62) & (d <= 0.90)
    oy = (yy[ring] - c).astype(np.int32)
    ox = (xx[ring] - c).astype(np.int32)

    def score(y0: float, sy: float) -> float:
        vals = []
        for ri in range(3):
            cy = int(round(y0 + ri * sy))
            for x in col_x:
                cx = int(round(x))
                vals.append(float(gray[np.clip(cy + oy, 0, h - 1), np.clip(cx + ox, 0, w - 1)].mean()))
        arr = np.asarray(vals)
        return float(arr.mean() - 0.55 * arr.std())

    best = (-1e9, (0.0, 0.0))
    for y0 in np.arange(0.55 * h, 0.86 * h, 5.0):
        for sy in np.arange(1.9 * rr, 3.3 * rr, 5.0):
            if y0 + 2 * sy > h - 1:
                continue
            s = score(y0, sy)
            if s > best[0]:
                best = (s, (float(y0), float(sy)))
    y0, sy = best[1]
    for dy in np.arange(-5.0, 5.1, 1.0):
        for ds in np.arange(-5.0, 5.1, 1.0):
            s = score(y0 + dy, sy + ds)
            if s > best[0]:
                best = (s, (y0 + dy, sy + ds))
    y0, sy = best[1]
    return col_x, [y0 + i * sy for i in range(3)], rr


# --------------------------------------------------------------------------- #
# 采样 + 打分
# --------------------------------------------------------------------------- #


def grid_from(col_x: list[float], row_y: list[float], rr: float, w: int, h: int) -> KeyGrid:
    pts = []
    for y in row_y:
        for x in col_x:
            pts.append(KeyPoint(u=x / w, v=y / h, r=rr / w))
    return KeyGrid(points=tuple(pts), key_count=len(pts))


def sample_frame(
    kind: str, grid: KeyGrid, img: np.ndarray, sampling, ring_points: int, square_points: int
) -> np.ndarray:
    h, w = img.shape[:2]
    inner = sampling.ring_inner_ratio
    side = sampling.square_side_ratio
    if kind == "annulus":
        return KeySampler(grid, w, h, stride=4, inner_ratio=inner, square_side=side).sample(img)
    if kind == "annulus_square":
        return KeySampler(grid, w, h, stride=4, inner_ratio=0.0, square_side=side).sample(img)
    if kind == "point_ring":
        return KeyPointSampler(
            grid, w, h, inner_ratio=inner, ring_points=ring_points,
            patch_ratio=sampling.point_patch_ratio,
        ).sample(img)
    if kind == "point_square":
        return KeyPointSampler(
            grid, w, h, inner_ratio=0.0, square_side=side, square_points=square_points,
            patch_ratio=sampling.point_patch_ratio,
        ).sample(img)
    raise ValueError(kind)


def score_keys(
    rgb: np.ndarray,
    indexes: list[int],
    metric: str,
    *,
    base: np.ndarray,
    weights=None,
    tolerance: int = 0,
    axis=(0, 1),
) -> np.ndarray:
    """用**生产代码**的判据给每个键打分（base 是该 area 的未按下均值）。"""
    thr = KeyThreshold(
        mode=metric,
        base_r=int(round(float(base[0]))),
        base_g=int(round(float(base[1]))),
        base_b=int(round(float(base[2]))),
        delta=0,
        weights=tuple(weights) if weights is not None else (1 / 3, 1 / 3, 1 / 3),
        tolerance=int(tolerance),
        shift_from=int(axis[0]),
        shift_to=int(axis[1]),
    )
    out = []
    for i in indexes:
        value = (int(rgb[i][0]), int(rgb[i][1]), int(rgb[i][2]))
        if metric == "max_drop":
            out.append(thr.max_drop(value))
        elif metric == "chroma":
            out.append(thr.chroma_shift(value))
        elif metric == "weighted":
            out.append(thr.weighted_score(value))
        else:
            out.append(thr.euclid_distance(value))
    return np.asarray(out, dtype=np.float64)


@dataclass
class Case:
    label: str
    metric: str
    sampler: str
    tolerance: int = 0
    weights: tuple[float, float, float] | None = None


def run(cases: list[Case], ring_points: int, square_points: int, verbose: bool) -> dict:
    instruments = load_instruments()
    sampling = load_sampling()
    results: dict[str, dict[str, dict]] = {c.label: {} for c in cases}
    per_image: dict[str, dict] = {}

    for filename, inst_id, calib_name in IMAGES:
        path = REF_DIR / filename
        if not path.exists():
            print(f"!! 缺图 {path}")
            continue
        spec = instruments[inst_id]
        img = imread_u(path)
        h, w = img.shape[:2]
        calib = json.loads((CALIB_DIR / calib_name).read_text(encoding="utf-8"))
        gt_notes = set(calib.get("pressed") or [])
        col_x, row_y, rr = find_grid(img, spec.button_radius * w / 1920.0)
        grid = grid_from(col_x, row_y, rr, w, h)

        # 只评 main 区（chord 盘色相各异，同列反推基线不成立）
        area = next(a for a in spec.areas if a.code == "main")
        key_index = {k: i for i, k in enumerate(spec.key_order)}
        indexes = [key_index[k] for k in area.keys if k in key_index]
        pressed = [i for i in indexes if NOTES[i] in gt_notes]
        released = [i for i in indexes if NOTES[i] not in gt_notes]
        if not pressed or not released:
            print(f"!! {filename}: 真值异常 pressed={pressed}")
            continue

        axis = _chroma_axis(spec.detection)
        weights = press_weights(spec.detection)
        per_image[filename] = {"inst": inst_id, "r_px": round(rr, 2), "pressed": [NOTES[i] for i in pressed]}

        caches: dict[str, np.ndarray] = {}
        for case in cases:
            if case.sampler not in caches:
                caches[case.sampler] = sample_frame(
                    case.sampler, grid, img, sampling, ring_points, square_points
                )
            rgb = caches[case.sampler]
            base = rgb[released].mean(axis=0)  # 与运行时同口径：area 内未按下键的均值
            scores = score_keys(
                rgb, indexes, case.metric, base=base,
                weights=case.weights if case.weights is not None else weights,
                tolerance=case.tolerance, axis=axis,
            )
            sig = float(scores[[indexes.index(i) for i in pressed]].min())
            noise = float(scores[[indexes.index(i) for i in released]].max())
            row = results[case.label].setdefault(inst_id, {"sig": [], "noise": [], "imgs": []})
            row["sig"].append(sig)
            row["noise"].append(noise)
            row["imgs"].append(filename)
        if verbose:
            print(f"  {filename:24s} r={rr:5.1f} 按下={per_image[filename]['pressed']}")

    summary: dict[str, dict] = {}
    for case in cases:
        per_inst = {}
        for inst_id, row in results[case.label].items():
            sig = float(np.min(row["sig"]))
            noise = float(np.max(row["noise"]))
            per_inst[inst_id] = {
                "signal_min": round(sig, 1),
                "noise_max": round(noise, 1),
                "snr": round(sig / max(noise, 0.5), 2),
                "window": round(0.6 * sig / max(1.5 * noise, 1e-6), 2),
                "images": len(row["sig"]),
            }
        snrs = [v["snr"] for v in per_inst.values()]
        summary[case.label] = {
            "per_instrument": per_inst,
            "snr_geo_mean": round(float(np.exp(np.mean(np.log(np.maximum(snrs, 1e-3))))), 2),
            "worst_snr": round(min(snrs), 2) if snrs else 0.0,
        }
    return {"summary": summary, "images": per_image}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ring-points", type=int, default=16)
    ap.add_argument("--square-points", type=int, default=8)
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    base_cases = [
        Case("A 环带+max_drop（现状）", "max_drop", "annulus"),
        Case("B 环带+chroma（现状 fwzs）", "chroma", "annulus"),
        Case("C 方块1R+max_drop（现状）", "max_drop", "annulus_square"),
        Case("D 点环+max_drop", "max_drop", "point_ring"),
        Case("E 点环+weighted", "weighted", "point_ring"),
        Case("F 点方+weighted", "weighted", "point_square"),
        Case("G 环带+weighted", "weighted", "annulus"),
    ]
    payload = run(base_cases, args.ring_points, args.square_points, not args.quiet)
    report(payload)
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def report(payload: dict) -> None:
    summary = payload["summary"]
    insts = ["fwzs", "xljq", "jhzq", "ljdsq", "ykq", "yy"]
    print()
    print(f"{'方法':28s}" + "".join(f"{i:>12s}" for i in insts) + f"{'SNR几何均':>12s}{'最差':>8s}")
    print("-" * 128)
    for label, data in summary.items():
        per = data["per_instrument"]
        line = f"{label:28s}"
        for i in insts:
            v = per.get(i)
            line += f"{v['snr']:>12.1f}" if v else f"{'-':>12s}"
        line += f"{data['snr_geo_mean']:>12.1f}{data['worst_snr']:>8.1f}"
        print(line)
    print()
    print("明细（signal_min / noise_max）：")
    for label, data in summary.items():
        print(f"  {label}")
        for i, v in data["per_instrument"].items():
            print(
                f"    {i:6s} sig={v['signal_min']:6.1f}  noise={v['noise_max']:5.1f}  "
                f"SNR={v['snr']:5.1f}  Δ窗口={v['window']:5.2f}  ({v['images']} 张)"
            )


if __name__ == "__main__":
    main()
