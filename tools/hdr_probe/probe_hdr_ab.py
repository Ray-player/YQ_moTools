"""在 HDR 屏上 A/B 对比三条取帧路径：GDI(BitBlt) / DXGI(dxcam) / hdrcapture。

为什么每路要单独起进程：DXGI Desktop Duplication **同一输出同时只允许一个会话**，
第二个 `DuplicateOutput(1)` 会直接 E_INVALIDARG；dxcam 还会在模块级缓存实例，`del` 也未必释放。
所以本脚本默认用子进程逐路执行，互不干扰。

用法（在有 HDR 显示器、开着游戏演奏界面的机器上跑）：

    python out/probe_hdr_ab.py --label sysHDR_on_gameHDR_off
    python out/probe_hdr_ab.py --label sysHDR_on_gameHDR_on
    python out/probe_hdr_ab.py --label sysHDR_off_gameHDR_off
    # 指定区域（虚拟桌面全局坐标，物理像素）
    python out/probe_hdr_ab.py --label x --rect 100 100 760 400

判读：
    GDI 与 dxgi   diff ≈ 0 且与 hdrcap ≈ 0  → 这套 HDR 配置下三条路等价，没必要上 hdrcapture
    GDI 偏亮/发白、hdrcap 正常              → 典型 HDR 症状，hdrcapture 是解
    hdrcap 与 GDI 有系统性色差              → 白点/色域换算有问题，需要复核
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
LIB_DIR = HERE.parent.parent / "out" / "pylibs"
"""兜底：`pip install --target out/pylibs j-hdrcapture`（venv 里已装就不需要）。"""
PATHS = ("gdi", "dxgi", "hdrcap")


# --------------------------------------------------------------------------- #
# 单路采集（子进程入口）
# --------------------------------------------------------------------------- #
def capture_one(path: str, rect: dict, repeat: int, out_png: Path) -> dict:
    if path == "gdi":
        import mss

        with mss.MSS() as sct:
            sct.grab(rect)
            ts = []
            for _ in range(max(repeat, 1)):
                t0 = time.perf_counter()
                img = np.asarray(sct.grab(rect), dtype=np.uint8)[:, :, :3].copy()
                ts.append((time.perf_counter() - t0) * 1000)
        cv2.imwrite(str(out_png), img)
        return {"path": path, "ms": float(np.median(ts)), "frames": len(ts)}

    if path == "dxgi":
        import dxcam

        cam = dxcam.create(output_color="BGR", backend="dxgi")
        if cam is None:
            raise RuntimeError("dxcam.create 返回 None")
        l, t, w, h = rect["left"], rect["top"], rect["width"], rect["height"]
        cam.start(region=(l, t, l + w, t + h), target_fps=60)
        ts = []
        img = None
        for _ in range(max(repeat * 5, 5)):  # 静止画面没有新帧，多试几次
            t0 = time.perf_counter()
            frame = cam.grab()
            if frame is not None:
                img = frame
                ts.append((time.perf_counter() - t0) * 1000)
                if len(ts) >= repeat:
                    break
            else:
                time.sleep(0.005)
        cam.stop()
        if img is None:
            img = cam.get_latest_frame()
        cv2.imwrite(str(out_png), img)
        return {"path": path, "ms": float(np.median(ts)) if ts else float("nan"), "frames": len(ts)}

    if path == "hdrcap":
        if LIB_DIR.is_dir():
            sys.path.insert(0, str(LIB_DIR))
        import hdrcapture

        cap = hdrcapture.Capture(timeout_ms=16)
        ts, info = [], {}
        for _ in range(max(repeat, 1)):
            t0 = time.perf_counter()
            f = cap.grab_region(rect["left"], rect["top"], rect["width"], rect["height"])
            ts.append((time.perf_counter() - t0) * 1000)
            info = f.monitor_info
        img = np.frombuffer(f.bgra, dtype=np.uint8).reshape(f.height, f.width, 4)[:, :, :3].copy()
        cv2.imwrite(str(out_png), img)
        cap.close()
        return {"path": path, "ms": float(np.median(ts)), "frames": len(ts), "monitor_info": info}

    raise ValueError(path)


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def stats(name: str, img: np.ndarray) -> dict:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).reshape(-1)
    return {
        "path": name,
        "shape": f"{img.shape[1]}x{img.shape[0]}",
        "mean": float(g.mean()),
        "p50": float(np.percentile(g, 50)),
        "p95": float(np.percentile(g, 95)),
        "clip%": float((g >= 250).mean() * 100),
    }


def hdr_state() -> str:
    """复用 out/probe_hdr_disp.py（DisplayConfig 口径，比 IDXGIOutput6 稳）。"""
    import io
    from contextlib import redirect_stdout

    try:
        import probe_hdr_disp as p

        buf = io.StringIO()
        with redirect_stdout(buf):
            p.main()
        return buf.getvalue().strip()
    except Exception as exc:
        return f"(HDR 状态读取失败: {exc})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--rect", nargs=4, type=int, metavar=("L", "T", "W", "H"))
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--only", choices=PATHS, help="内部用：只跑某一路并输出 png")
    ap.add_argument("--out", help="内部用：--only 模式的输出 png")
    args = ap.parse_args()

    if args.rect:
        l, t, w, h = args.rect
    else:
        l, t, w, h = 0, 0, 960, 540
    rect = {"left": l, "top": t, "width": w, "height": h}

    out_dir = HERE / "hdr_ab"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")

    # ---- 子进程模式 ---------------------------------------------------- #
    if args.only:
        res = capture_one(args.only, rect, args.repeat, Path(args.out))
        print("RESULT " + json.dumps(res))
        return 0

    # ---- 主进程：逐路起子进程 ------------------------------------------ #
    print("=== 显示状态（DisplayConfig） ===")
    print(hdr_state())
    print()

    frames: dict[str, np.ndarray] = {}
    timings: dict[str, float] = {}
    extras: dict[str, dict] = {}

    for path in PATHS:
        png = out_dir / f"{args.label}_{stamp}_{path}.png"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--only",
            path,
            "--rect",
            str(l),
            str(t),
            str(w),
            str(h),
            "--repeat",
            str(args.repeat),
            "--out",
            str(png),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if line is None:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            print(f"[{path}] 跳过：{msg[-1] if msg else '未知错误'}")
            continue
        res = json.loads(line[len("RESULT ") :])
        img = cv2.imread(str(png), cv2.IMREAD_COLOR)
        if img is None:
            print(f"[{path}] 跳过：没写出图片")
            continue
        frames[path] = img
        timings[path] = res.get("ms", float("nan"))
        extras[path] = res

    if not frames:
        print("\n三条路都失败，无法比对。")
        return 1

    rows = [stats(name, img) for name, img in frames.items()]
    print(f"\n{'路径':<8}{'尺寸':<12}{'均值':>7}{'p50':>7}{'p95':>7}{'≥250占比%':>11}{'单帧ms':>9}")
    print("-" * 62)
    for r in rows:
        print(
            f"{r['path']:<8}{r['shape']:<12}{r['mean']:>7.1f}{r['p50']:>7.1f}"
            f"{r['p95']:>7.1f}{r['clip%']:>11.2f}{timings.get(r['path'], float('nan')):>9.1f}"
        )

    for path in ("dxgi", "hdrcap"):
        if path not in extras:
            continue
        info = extras[path].get("monitor_info")
        if info:
            print(f"\n[{path}] monitor_info: {info}")

    base = frames.get("gdi")
    if base is not None:
        b = base.astype(np.int16)
        for name, img in frames.items():
            if name == "gdi" or img.shape != base.shape:
                continue
            a = img.astype(np.int16)
            diff = float(np.abs(b - a).mean())
            print(f"GDI vs {name}: 平均绝对差 {diff:.2f}")
            vis = np.clip(np.abs(b - a).sum(axis=2), 0, 255).astype(np.uint8)
            cv2.imwrite(str(out_dir / f"{args.label}_{stamp}_diff_{name}.png"), vis)

    print(f"\n图片目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
