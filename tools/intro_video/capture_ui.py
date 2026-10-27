"""抓取 YQ_moTools 真实界面截图（介绍视频素材）。

真实流程复现：真机语料 → 颜色检测 21 键 → 采样 → 判据量 → 界面灯板 / 预览圈。
产物：out/intro_video/shots/{ui_light,ui_dark,_crop_a}.png
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHOTS = ROOT / "out" / "intro_video" / "shots"
TMP_APPDATA = SHOTS / "_appdata"
SHOTS.mkdir(parents=True, exist_ok=True)
TMP_APPDATA.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(TMP_APPDATA)
os.environ.setdefault("QT_SCALE_FACTOR", "2")
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yqrec.instruments import load_instruments, load_sampling  # noqa: E402
from yqrec.ui import theme  # noqa: E402
from yqrec.ui.main_window import MainWindow  # noqa: E402
from yqrec.vision.keygrid import (  # noqa: E402
    circle_map_note,
    detect_keys_color,
    keygrid_from_note_map,
)
from yqrec.vision.pointsampler import KeyPointSampler  # noqa: E402

REAL = ROOT / "tests" / "data" / "real" / "fwzs_1695x956.jpg"

TICK_SAMPLE = (
    "0A120C120E120G240F120A120D240C480\n"
    "A120C120E120G240A120C120E120G480\n"
    "C240A240F240D240E480G120C240A480"
)


def bgr_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def make_window(name: str, dark: bool, spec_id: str = "fwzs") -> tuple[MainWindow, dict]:
    QApplication.instance() or QApplication(sys.argv[:1])
    theme.set_current(theme.DARK if dark else theme.LIGHT)
    win = MainWindow()
    win.resize(558, 913)
    win._apply_theme(theme.DARK if dark else theme.LIGHT)

    spec = load_instruments()[spec_id]
    sampling = load_sampling()
    win._spec = spec

    frame = cv2.imread(str(REAL))
    h, w = frame.shape[:2]
    circles = detect_keys_color(frame, spec)
    mapping = circle_map_note(circles, spec)
    grid = keygrid_from_note_map(mapping, spec, w, h)

    inner = 0.98 - sampling.ring_width_ratio
    sampler = KeyPointSampler(
        grid,
        w,
        h,
        inner_ratio=inner,
        ring_points=sampling.ring_points,
        patch_ratio=sampling.point_patch_ratio,
    )
    rgb = sampler.sample(frame)

    win._region = (0, 0, w, h)
    win._update_region_label()
    win._channel_by_area = {area.code: "R" for area in spec.areas}
    win._baseline = np.array(
        [spec.detection.unpressed_ref for _ in range(spec.key_count)], dtype=np.float32
    )
    win._recompute_thresholds()
    win._set_grid(grid)
    win._preview.set_frame(bgr_to_qimage(frame))
    win._preview.set_grid(grid)
    win._keys_panel.update_samples(rgb)

    deltas = []
    for i in range(spec.key_count):
        t = win._keys_panel._key_thresholds[i]
        if t is not None:
            deltas.append((float(t.weighted_score(tuple(int(v) for v in rgb[i]))), i))
    deltas.sort(reverse=True)
    pressed = [False] * spec.key_count
    for score, index in deltas[:3]:
        pressed[index] = True
    win._keys_panel.set_press(pressed)
    win._preview.set_press(pressed)
    win._record_view.setPlainText(TICK_SAMPLE)
    win._refresh_sidebar_meta()
    win._flash_status(f"识别中 · 已识别 128 音 · 采样 60fps · 源 60fps")
    QApplication.instance().processEvents()
    info = {"deltas": deltas[:5], "grid_ok": len(grid.points), "frame": (w, h)}
    return win, info


def shoot(win: MainWindow, name: str) -> pathlib.Path:
    app = QApplication.instance()
    win.show()
    app.processEvents()
    app.processEvents()
    out = SHOTS / f"{name}.png"
    win.grab().save(str(out))
    win.hide()
    app.processEvents()
    return out


def crop_game() -> pathlib.Path:
    """从真机语料裁出干净的琴键区（去掉谱面文字 / 左上键盘 / 水印）。"""
    from PIL import Image

    src = Image.open(REAL)
    out = SHOTS / "_crop_a.png"
    src.crop((180, 430, 1420, 930)).save(out)
    return out


def main() -> None:
    print(crop_game())
    win, info = make_window("light", dark=False)
    print("light:", info)
    print(shoot(win, "ui_light"))
    win2, _ = make_window("dark", dark=True)
    print(shoot(win2, "ui_dark"))


if __name__ == "__main__":
    main()
