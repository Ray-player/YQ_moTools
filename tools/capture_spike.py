"""P0 捕获基准脚本（02 §7 T1/T2）。

用法示例::

    python tools/capture_spike.py --backend all --seconds 5 --rect 560,700,1600,900
    python tools/capture_spike.py --backend dxgi --seconds 5 --window 原神
    python tools/capture_spike.py --backend all --seconds 5 --dump-frames out/

输出 JSON：每个后端的 fps / 帧数 / p95 帧间隔 / 首帧是否全黑 / 首帧 PNG。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YQRec 捕获后端基准")
    p.add_argument("--backend", default="all",
                   help="all | dxgi | wgc | gdi | printwindow | file")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--rect", default="0,0,1600,900", help="屏幕区域 left,top,width,height")
    p.add_argument("--window", default=None, help="窗口标题子串（用 WindowTarget）")
    p.add_argument("--file", default=None, help="文件后端输入（mp4/PNG 目录）")
    p.add_argument("--dump-frames", default=None, help="首帧 PNG 输出目录")
    p.add_argument("--fps", type=int, default=60)
    return p.parse_args()


def build_target(args: argparse.Namespace):
    from yqrec.types import Rect, ScreenTarget, WindowTarget

    if args.window:
        from yqrec.capture.windowing import find_window

        hwnd = find_window(args.window)
        if hwnd is None:
            raise SystemExit(f"找不到窗口: {args.window}")
        return WindowTarget(hwnd=hwnd, norm=(0.0, 0.0, 1.0, 1.0), title=args.window)
    left, top, w, h = (int(x) for x in args.rect.split(","))
    return ScreenTarget(monitor=0, rect=Rect(left, top, w, h))


def main() -> int:
    args = parse_args()
    from yqrec.capture.manager import CaptureManager

    target = build_target(args)
    manager = CaptureManager()
    if args.file:
        manager.set_file_source(args.file)

    available = [getattr(c, "name", c.__name__) for c in manager.candidates(target)]
    if args.backend != "all":
        available = [n for n in available if n == args.backend]

    results = []
    for name in available or [None]:
        if name is None:
            results.append({"backend": None, "error": "无可用后端"})
            continue
        manager2 = CaptureManager()
        if args.file:
            manager2.set_file_source(args.file)
        try:
            manager2.start(target, prefer=name, target_fps=args.fps)
        except Exception as exc:
            results.append({"backend": name, "error": str(exc)})
            continue

        gaps: list[float] = []
        frames = 0
        first = None
        t_prev = None
        t_end = time.perf_counter() + args.seconds
        while time.perf_counter() < t_end:
            got = manager2.latest(timeout_ms=200)
            if got is None:
                continue
            frame, _ts = got
            now = time.perf_counter()
            if t_prev is not None:
                gaps.append(now - t_prev)
            t_prev = now
            frames += 1
            if first is None:
                first = frame
        stats = manager2.stats()
        manager2.stop()

        entry = {
            "backend": name,
            "frames": frames,
            "fps": round(frames / args.seconds, 2),
            "gap_p95_ms": round(_percentile(gaps, 95) * 1000, 2),
            "black": bool(first is not None and float(first.mean()) < 1.0),
            "shape": list(first.shape) if first is not None else None,
            "dropped": stats.dropped,
        }
        if first is not None and args.dump_frames:
            out = Path(args.dump_frames)
            out.mkdir(parents=True, exist_ok=True)
            try:
                import cv2

                cv2.imwrite(str(out / f"{name}.png"), first)
                entry["png"] = str(out / f"{name}.png")
            except Exception as exc:
                entry["png_error"] = str(exc)
        results.append(entry)

    print(json.dumps({"target": type(target).__name__, "results": results},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
