"""在有持续 present 的场景下测 hdrcapture 的取帧延迟与吞吐。

主线程跑 tkinter 造 60fps 画面（tkinter 必须在主线程），工作线程连续 grab。
"""
import sys, threading, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "out" / "pylibs"))

import numpy as np
import hdrcapture

RESULT = {}
STOP = threading.Event()


def worker():
    cap = hdrcapture.Capture(timeout_ms=16)
    mon = cap.monitors[1]
    x, y, w, h = mon.rect
    RESULT["monitor"] = (mon.rect, mon.friendly_name)

    def bench(label, fn):
        for _ in range(6):
            fn()
        ts, dup, prev = [], 0, None
        for _ in range(120):
            t0 = time.perf_counter()
            f = fn()
            ts.append((time.perf_counter() - t0) * 1000)
            cur = np.frombuffer(f.bgra, dtype=np.uint8)[::4096].copy()
            if prev is not None and np.array_equal(cur, prev):
                dup += 1
            prev = cur
        RESULT[label] = (np.median(ts), np.percentile(ts, 90), dup)

    bench(f"整屏 {w}x{h}", lambda: cap.grab(mon))
    bench("区域 1600x900", lambda: cap.grab_region(x + 100, y + 80, 1600, 900))
    RESULT["stats"] = cap.stats
    STOP.set()


try:
    import tkinter as tk
except Exception as exc:
    print("tkinter 不可用:", exc)
    raise SystemExit(1)

root = tk.Tk()
root.geometry("400x240+40+40")
root.attributes("-topmost", True)
cv = tk.Canvas(root, width=400, height=240, highlightthickness=0)
cv.pack()
rect_id = cv.create_rectangle(0, 0, 400, 240, fill="#000000")
state = {"i": 0, "t0": None, "n": 0}


def tick():
    if STOP.is_set():
        root.destroy()
        return
    if state["t0"] is None:
        state["t0"] = time.perf_counter()
    state["i"] = (state["i"] + 11) % 256
    cv.itemconfig(rect_id, fill=f"#{state['i']:02x}{(255 - state['i']):02x}80")
    state["n"] += 1
    root.after(8, tick)


threading.Thread(target=worker, daemon=True).start()
root.after(600, tick)
root.mainloop()

dt = time.perf_counter() - (state["t0"] or time.perf_counter())
print(f"[animator] 重绘 {state['n']} 次 / {dt:.1f}s = {state['n'] / max(dt, 1e-6):.0f} fps")
print("monitor:", RESULT.get("monitor"))
for k in ("整屏 1920x1080", "区域 1600x900"):
    if k in RESULT:
        med, p90, dup = RESULT[k]
        print(f"{k}: median {med:.2f}ms  p90 {p90:.2f}  重复帧 {dup}/120  -> {1000 / med:.0f} fps 上限")
print("stats:", RESULT.get("stats"))
