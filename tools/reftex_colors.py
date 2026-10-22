"""参考图取色 → 建议判按参数（M4 §3）。

用途：拿 `assets/refTex/*.png` 这类"乐器界面截图"实测每个键帽的颜色，
进而算出「按下时的通道跌幅 → 权重 / refs / Δ 建议值」。

口径与运行时严格一致（这点最重要，否则数字和 03 §5.1 的老数据对不上）：

- 键位：`detect_keys()`（Hough）拿到的圆聚类出 7 列 × 3 行，再按规则格点补全
  `key_count` 个键 —— 不做颜色法（参考图里有圆盘下方的白色键名方标，
  颜色法在上面必然失败：掩膜里圆盘与方标连成一块，圆度/面积过滤全灭）。
- 取色：`KeyPointSampler(inner_ratio=0.80, ring_points=8, patch_ratio=0.06)`
  —— 与 `instruments.json` 的 `sampling` 默认值一致。
- 未按下参考：每行 7 键的**中位数**（按下键是少数，中位数天然抗污染）。
- 按下识别：相对行中位数 `max_drop > --press-drop`（默认 25）。

用法::

    python tools/reftex_colors.py assets/refTex/ref_余音.png --instrument yy
    python tools/reftex_colors.py --all --json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from yqrec.instruments import load_instruments  # noqa: E402
from yqrec.types import KeyGrid, KeyPoint  # noqa: E402
from yqrec.vision.keygrid import detect_keys  # noqa: E402
from yqrec.vision.pointsampler import KeyPointSampler  # noqa: E402

KEYS = (
    ["Q", "W", "E", "R", "T", "Y", "U"],
    ["A", "S", "D", "F", "G", "H", "J"],
    ["Z", "X", "C", "V", "B", "N", "M"],
)
"""(行, 列) → 键盘字母。参考图 3 排都是这个布局。"""

# 参考图里带降号「b」的键（只有老三的诗琴有），仅用于报告里标注
FLAT_KEYS = frozenset("WEYUSJCN")
FLAT_INSTRUMENTS = frozenset({"ljdsq"})

DEFAULT_HOUGH = {
    "dp": 2.0,
    "min_dist": 80,
    "param1": 20,
    "param2": 40,
    "min_radius": 40,
    "max_radius": 53,
    "upper_bound": 239,
}


def imread_unicode(path: Path) -> np.ndarray:
    """`cv2.imread` 打不开中文路径，统一走 imdecode。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise SystemExit(f"读不出图片: {path}")
    return frame


def _cluster(values: list[float], tol: float = 12.0) -> list[float]:
    ordered = sorted(float(v) for v in values)
    groups: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tol:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [float(np.mean(g)) for g in groups]


def _fill_to(centers: list[float], count: int) -> list[float]:
    out = sorted(centers)
    if len(out) >= count:
        return out[:count]
    if len(out) < 2:
        raise SystemExit("Hough 只聚出一组坐标，无法拟合网格")
    step = float(np.median(np.diff(out)))
    while len(out) < count:
        out.insert(0, out[0] - step)
    return out


def lattice(frame: np.ndarray, hough: dict, key_count: int) -> tuple[list[float], list[float], float]:
    """Hough → 7 列 × N 行的规则格点（像素坐标）。"""
    circles = detect_keys(frame, hough, key_count)
    if not circles:
        raise SystemExit("Hough 没找到任何圆")
    cols = _fill_to(_cluster([c.x for c in circles]), 7)
    rows = _fill_to(_cluster([c.y for c in circles]), key_count // 7)
    radius = float(np.median([c.r for c in circles]))
    return rows, cols, radius


def build_grid(rows: list[float], cols: list[float], radius_px: float, w: int, h: int) -> KeyGrid:
    points = [
        # KeyPoint.r 是**比值**（r / 帧宽），不是 1920 基准像素
        KeyPoint(u=cx / w, v=cy / h, r=radius_px / w)
        for cy in rows
        for cx in cols
    ]
    return KeyGrid(points=tuple(points), key_count=len(points))


def sample_image(frame: np.ndarray, spec, hough: dict, inner_ratio: float) -> np.ndarray:
    rows, cols, radius = lattice(frame, hough, spec.key_count)
    h, w = frame.shape[:2]
    grid = build_grid(rows, cols, radius, w, h)
    sampler = KeyPointSampler(
        grid,
        w,
        h,
        inner_ratio=inner_ratio,
        ring_points=8,
        square_points=4,
        patch_ratio=0.06,
    )
    return sampler.sample(frame).astype(int)


def analyse(rgb: np.ndarray, press_drop: float, tolerance: int = 8, *, has_flat: bool = False) -> dict:
    """把 (K,3) 键色拆成「每行未按下参考 + 按下键 Δ + 建议权重」。"""
    rows: list[dict] = []
    pressed_deltas: list[np.ndarray] = []
    for ri in range(3):
        block = rgb[ri * 7 : (ri + 1) * 7]
        base = np.median(block, axis=0)
        keys: list[dict] = []
        for ci, key in enumerate(KEYS[ri]):
            cur = block[ci]
            delta = base - cur
            is_pressed = float(delta.max()) > press_drop
            keys.append(
                {
                    "key": key,
                    "rgb": [int(v) for v in cur],
                    "delta": [int(v) for v in delta],
                    "max_drop": float(delta.max()),
                    "pressed": is_pressed,
                    "flat": has_flat and key in FLAT_KEYS,
                }
            )
            if is_pressed:
                pressed_deltas.append(delta)
        rows.append(
            {
                "row": ri,
                "base": [int(v) for v in base],
                "keys": keys,
            }
        )

    weights = None
    score = None
    if pressed_deltas:
        mean_delta = np.mean(pressed_deltas, axis=0)
        raw = np.maximum(0.0, mean_delta)
        weights = (raw / raw.sum()).round(3).tolist()
        drop = np.maximum(0.0, mean_delta - tolerance)
        score = round(float((np.array(weights) * drop).sum() / sum(weights)), 1)

    return {
        "rows": rows,
        "weights": weights,
        "score": score,
        "pressed": [k["key"] for r in rows for k in r["keys"] if k["pressed"]],
        "flat_pressed": [
            k["key"] for r in rows for k in r["keys"] if k["pressed"] and k["flat"]
        ],
    }


def print_report(name: str, shape: tuple[int, int], result: dict, tolerance: int) -> None:
    print(f"\n=== {name}  ({shape[1]}x{shape[0]}) ===")
    for row in result["rows"]:
        print(f"-- 行{row['row']}  未按下≈{tuple(row['base'])}")
        for item in row["keys"]:
            mark = "◉" if item["pressed"] else ("b" if item["flat"] else " ")
            print(
                f"   {mark} {item['key']}  rgb={tuple(item['rgb'])}  "
                f"Δ={tuple(item['delta'])}  max_drop={item['max_drop']:5.0f}"
            )
    if result["weights"]:
        print(
            f"\n  按下键: {' '.join(result['pressed'])}"
            + (f"（含降号键 {' '.join(result['flat_pressed'])}）" if result["flat_pressed"] else "")
        )
        print(f"  建议 weights = {result['weights']}   （tolerance={tolerance} 时 score≈{result['score']}）")
        print(f"  建议 delta   = {max(10, min(20, int(result['score'] / 2.5)))}   （score/2.5，且不超过 20）")
    else:
        print("\n  没有检出按下键：确认截图里至少有一个键处于按下态。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="参考图取色 → 建议判按参数")
    parser.add_argument("images", nargs="*", help="截图路径（支持中文路径）")
    parser.add_argument("--instrument", default="fwzs", help="乐器 id（决定 button_radius / hough）")
    parser.add_argument("--all", action="store_true", help="跑 assets/refTex/*.png")
    parser.add_argument("--inner-ratio", type=float, default=0.80, help="环形采样半径（×R）")
    parser.add_argument("--press-drop", type=float, default=25.0, help="判为按下的最大跌幅")
    parser.add_argument("--tolerance", type=int, default=8, help="死区（与 detection.tolerance 对齐）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于贴进文档/脚本处理）")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.images]
    if args.all or not paths:
        paths = sorted(Path(p) for p in glob.glob(str(ROOT / "assets" / "refTex" / "*.png")))
    if not paths:
        parser.error("没有找到任何图片")

    specs = load_instruments()
    spec = specs.get(args.instrument) or next(iter(specs.values()))
    hough = spec.hough or DEFAULT_HOUGH

    payload = {}
    for path in paths:
        frame = imread_unicode(path)
        rgb = sample_image(frame, spec, hough, args.inner_ratio)
        result = analyse(
            rgb, args.press_drop, args.tolerance, has_flat=spec.id in FLAT_INSTRUMENTS
        )
        if args.json:
            payload[path.name] = {"shape": list(frame.shape[:2]), **result}
        else:
            print_report(path.name, frame.shape[:2], result, args.tolerance)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
