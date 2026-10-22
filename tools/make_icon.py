"""把一张方形 PNG 做成多尺寸 Windows `.ico`（含圆角透明四角）。

为什么需要它：设计给的 PNG 通常是**直角满幅**，直接塞进 `.ico` 会在任务栏/资源管理器里
露出四个白角（深色背景下尤其难看）。参考 `assets/icons/fqwz.ico` 的做法是先把四角挖透明。

圆角比例沿用 `fqwz.ico` 的实测值：64px 帧里半径 11px → **0.172 × 边长**，
换图时不用重新量。

**为什么自己拼 ICO 容器**：`PIL.Image.save(format="ICO")` 会从一张大图 LANCZOS 缩出所有
尺寸，缩放核的振铃会让圆角边缘的 alpha 提前抬升（实测 64px 圆角量出来是 8px 而不是 11px）。
所以这里每个尺寸各自 resize、各自铺 4× 超采样遮罩，再按 ICO 规范拼容器
（≤255 用 Pillow 生成的 BMP 条目、256 用 PNG 条目，Windows 与 PyInstaller 都认）。

用法：
    .venv/Scripts/python.exe tools/make_icon.py assets/icons/mo_icon.png assets/icons/mo_motools.ico
    .venv/Scripts/python.exe tools/make_icon.py <png> <ico> --radius-ratio 0.22
"""

from __future__ import annotations

import argparse
import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image

DEFAULT_RADIUS_RATIO = 0.22
"""圆角半径 / 边长。

对着 `assets/icons/fqwz.ico`（64×64，顶边直线段从 11px 起）做并排目视比对后取 0.22：
0.172 明显偏方、0.25 略圆，0.22 与参考图观感最接近。
"""

DEFAULT_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""Windows 会用到的尺寸（16 任务栏小图标 / 256 大图标视图）。"""

SUPERSAMPLE = 4
"""遮罩超采样倍数：先在 4 倍网格上算覆盖，再取平均当抗锯齿。"""


def rounded_mask(size: tuple[int, int], radius_ratio: float) -> Image.Image:
    """返回一张 L 模式遮罩：圆角矩形内 255、四角 0。

    **不用 `ImageDraw.rounded_rectangle`**：它用贝塞尔近似圆角，曲线在角部比标准圆弧
    更"鼓"（实测 64px 下量出来的圆角是 8px，而参考图标是 11px）。这里直接按
    圆角矩形距离场算，然后 4× 超采样取平均 —— 半径精确、边缘自然抗锯齿。
    """
    width, height = size
    ratio = max(0.0, min(0.5, radius_ratio))
    radius = ratio * min(width, height)
    big_w, big_h = width * SUPERSAMPLE, height * SUPERSAMPLE
    radius_big = radius * SUPERSAMPLE

    ys, xs = np.mgrid[0:big_w, 0:big_h]
    # 到"内缩矩形"的有向距离：圆角部分就是四个角的圆弧
    qx = np.abs(xs + 0.5 - big_w / 2.0) - (big_w / 2.0 - radius_big)
    qy = np.abs(ys + 0.5 - big_h / 2.0) - (big_h / 2.0 - radius_big)
    inner = np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2
    inside = np.maximum(qx, qy) <= 0.0
    inside |= inner <= radius_big**2

    coverage = inside.astype(np.float32).reshape(height, SUPERSAMPLE, width, SUPERSAMPLE)
    mask = (coverage.mean(axis=(1, 3)) * 255.0).round().astype(np.uint8)
    return Image.fromarray(mask, "L")


def square_source(png: Path) -> Image.Image:
    """读图并裁成正方形（非方图按中心裁，避免变形）。"""
    source = Image.open(png).convert("RGB")
    if source.width != source.height:
        side = min(source.size)
        left = (source.width - side) // 2
        top = (source.height - side) // 2
        source = source.crop((left, top, left + side, top + side))
    return source


def _frame_bytes(image: Image.Image) -> bytes:
    """把单张图交给 Pillow 存成"只有一个尺寸的 ico"，再抽出它的图像数据块。

    这样 ≤255 的尺寸拿到的就是 Pillow 生成的 BMP(DIB) 条目、256 拿到 PNG 条目 ——
    比手写 DIB 靠谱，也不用碰 Pillow 的私有 API。
    """
    buffer = io.BytesIO()
    image.save(buffer, format="ICO", sizes=[image.size])
    raw = buffer.getvalue()
    count = struct.unpack_from("<H", raw, 4)[0]
    if count != 1:
        raise RuntimeError(f"单尺寸 ICO 意外包含 {count} 个条目")
    size, offset = struct.unpack_from("<II", raw, 6 + 8)
    return raw[offset : offset + size]


def write_ico(frames: list[Image.Image], ico: Path) -> None:
    """按 ICO 规范拼装多尺寸容器（每个条目都是合法 RT_ICON 数据）。"""
    payloads = [(frame.size, _frame_bytes(frame)) for frame in frames]
    header = struct.pack("<HHH", 0, 1, len(payloads))
    directory = b""
    body = b""
    offset = len(header) + 16 * len(payloads)
    for (width, height), data in payloads:
        directory += struct.pack(
            "<BBBBHHII",
            width % 256,  # 256 用 0 表示，符合 ICO 规范
            height % 256,
            0,  # 调色板颜色数（真彩为 0）
            0,  # 保留位
            1,  # color planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        body += data
        offset += len(data)
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(header + directory + body)


def build(png: Path, ico: Path, radius_ratio: float, sizes: tuple[int, ...]) -> None:
    source = square_source(png)
    frames: list[Image.Image] = []
    for size in sizes:
        frame = source.resize((size, size), Image.LANCZOS).convert("RGBA")
        frame.putalpha(rounded_mask((size, size), radius_ratio))
        frames.append(frame)
    write_ico(frames, ico)
    print(f"{ico}  {ico.stat().st_size / 1024:.1f} KB  sizes={list(sizes)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="方形 PNG → 圆角多尺寸 ICO")
    parser.add_argument("png", type=Path)
    parser.add_argument("ico", type=Path)
    parser.add_argument("--radius-ratio", type=float, default=DEFAULT_RADIUS_RATIO)
    args = parser.parse_args()
    build(args.png, args.ico, args.radius_ratio, DEFAULT_SIZES)


if __name__ == "__main__":
    main()
