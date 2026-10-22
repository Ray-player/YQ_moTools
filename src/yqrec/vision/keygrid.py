"""键位检测与网格校正（算法见 03 §3.2）。

纯几何部分不依赖 cv2，可直接单测；只有 `detect_keys` 在调用时惰性导入 cv2/numpy。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import KeyGridMismatch
from ..types import Circle, InstrumentSpec, KeyGrid, KeyPoint

_MAGIC_PER_ROW = 7
"""屏幕上的琴键每排 7 个（21 键 = 3 排；14 键 = 2 排）。"""

_REF_WIDTH = 1920.0
"""标定参考宽度：所有归一化半径以它为基准（决策 4）。"""


def correct_button_coordinates(circles: Sequence[Circle]) -> list[Circle]:
    """网格校正（03 §3.2 Step 2）。

    规则：
    - 长度必须是 7 的倍数且 >= 14，否则原样返回；
    - 按 Y 排序后每 7 个一组，组内按 X 排序；
    - 每行的 Y 取该行均值；每列的 X 取所有行同列均值；
    - 输出顺序：**倒序遍历行**（画面最下面一排先输出），行内按 X 升序。
    """
    circles = list(circles)
    n = len(circles)
    if n == 0 or n % _MAGIC_PER_ROW != 0 or n < 2 * _MAGIC_PER_ROW:
        return circles

    rows = n // _MAGIC_PER_ROW
    ordered = sorted(circles, key=lambda c: c.y)

    grid: list[list[Circle]] = []
    for r in range(rows):
        row = sorted(ordered[r * _MAGIC_PER_ROW : (r + 1) * _MAGIC_PER_ROW], key=lambda c: c.x)
        grid.append(row)

    row_y = [sum(c.y for c in row) / _MAGIC_PER_ROW for row in grid]
    col_x = [
        sum(grid[r][c].x for r in range(rows)) / rows for c in range(_MAGIC_PER_ROW)
    ]

    out: list[Circle] = []
    for r in range(rows - 1, -1, -1):  # 倒序：最下面一排先出
        for c in range(_MAGIC_PER_ROW):
            src = grid[r][c]
            out.append(Circle(col_x[c], row_y[r], src.r))
    return out


def _row_major(circles: Sequence[Circle], key_count: int) -> list[Circle]:
    """按 (Y, X) 行主序排列；若键数是 7 的倍数，先按 7 个一组再组内按 X。"""
    ordered = sorted(circles, key=lambda c: (c.y, c.x))
    if key_count % _MAGIC_PER_ROW == 0 and key_count // _MAGIC_PER_ROW >= 1:
        rows = key_count // _MAGIC_PER_ROW
        out: list[Circle] = []
        for r in range(rows):
            out.extend(
                sorted(ordered[r * _MAGIC_PER_ROW : (r + 1) * _MAGIC_PER_ROW], key=lambda c: c.x)
            )
        return out
    return ordered


def circle_map_note(circles: Sequence[Circle], spec: InstrumentSpec) -> dict[str, Circle]:
    """圆 → 音名（03 §3.2 Step 3）。

    返回 `{note_name: circle}`，键顺序与 `spec.note_order` 一致：
    最上排 = 高音 `+1..+7`，中排 `1..7`，下排 `-1..-7`。
    """
    circles = list(circles)
    if len(circles) != spec.key_count:
        raise KeyGridMismatch(
            f"{spec.name}: 检测到 {len(circles)} 个琴键，应为 {spec.key_count} 个"
        )
    ordered = _row_major(circles, spec.key_count)
    return {note: ordered[i] for i, note in enumerate(spec.note_order)}


def detect_keys(frame, hough: dict, key_count: int) -> list[Circle]:
    """Hough 找圆（03 §3.2 Step 1）。

    `frame`: HxWx3 BGR ndarray；`hough`: instruments.json 中的 hough 段。
    返回像素坐标（已把 Y 偏移加回），顺序未排序。
    """
    import cv2  # 惰性导入：无 GUI/无 cv2 环境也能 import 本模块

    h, w = frame.shape[:2]
    scale = w / _REF_WIDTH
    y_off = int(h / 3)
    roi = frame[y_off:, :]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    mask = cv2.inRange(gray, 0, int(hough["upper_bound"]))
    found = cv2.HoughCircles(
        mask,
        cv2.HOUGH_GRADIENT,
        dp=float(hough["dp"]),
        minDist=float(hough["min_dist"]) * scale,
        param1=float(hough["param1"]),
        param2=float(hough["param2"]),
        minRadius=int(float(hough["min_radius"]) * scale),
        maxRadius=int(float(hough["max_radius"]) * scale),
    )
    if found is None:
        return []
    return [Circle(float(x), float(y) + y_off, float(r)) for x, y, r in found[0]]


# -- 颜色法检测（真机画面：白盘 ∪ 棕盘） ---------------------------------- #

COLOR_WHITE_S_MAX = 45
"""白键判定：饱和度 < 45（发白）。"""

COLOR_WHITE_V_MIN = 195
"""白键判定：亮度 > 195。"""

COLOR_BROWN_H_MIN = 8
COLOR_BROWN_H_MAX = 35
"""棕键（按下）色相区间（OpenCV H ∈ 0..180）。"""

COLOR_BROWN_S_MIN = 45
"""棕键判定：饱和度 > 45（白盘按下后明显发黄）。"""

COLOR_BROWN_S_MAX = 170
"""棕键饱和度上界。

必须卡掉高饱和橙色：金黄/橙色背景（花海、火焰）在 H 8..35 且 S≈220 时会被误判成"棕盘"，
整个背景连成一片，反而把按键吞掉（实测风物之诗花海截图完全检不出键）。
棕盘实测 S≈74，留到 170 有足够余量。"""

COLOR_BROWN_V_MIN = 110
COLOR_BROWN_V_MAX = 250
"""棕键亮度区间：上界排除纯白高光。"""

COLOR_CIRCULARITY_MIN = 0.55
"""圆度 4πA/P² 下限：滤掉长条（树、光带）。"""

COLOR_AREA_MIN_RATIO = 0.45
COLOR_AREA_MAX_RATIO = 2.6
"""面积过滤：相对 `π·button_radius²` 的比例区间；超过上界时用 1.8πR² 兜底。"""

COLOR_ROW_TOL_RATIO = 0.6
"""行聚类容差（× button_radius）。"""

COLOR_COL_TOL_RATIO = 0.35
"""列聚类容差（× button_radius）。"""


def detect_keys_color(frame, spec: InstrumentSpec) -> list[Circle]:
    """按颜色（白盘 ∪ 棕盘）找琴键，再用"行聚类 + 列拟合"补成 3×7 网格。

    为什么需要它：真机截图里 Hough 会把背景树/光效一起算进来（实测 130~160 个圆），
    `correct_button_coordinates` 要求数量是 7 的倍数，数量不符就整张图放弃 —— 表现为
    "识别到按键但一个圈都不画"。白盘/棕盘的颜色特征则非常稳定。

    Args:
        frame: `HxWx3` BGR ndarray。
        spec: 乐器参数（只用 `button_radius` 做尺度参考）。

    Returns:
        21（或 `spec.key_count`）个 `Circle`（像素坐标，顺序无所谓 —— 上层
        `circle_map_note` 会按 Y/X 重排）。**返回空列表表示该方法不适用**，上层应回退 Hough。
    """
    import cv2
    import numpy as np

    if frame is None or getattr(frame, "ndim", 0) != 3:
        return []
    height, width = int(frame.shape[0]), int(frame.shape[1])
    if height <= 0 or width <= 0:
        return []
    per_row = _MAGIC_PER_ROW
    if spec.key_count % per_row != 0:
        return []
    rows_wanted = spec.key_count // per_row

    # `spec.button_radius` 以 1920 宽为基准归一化（03 §1），换成当前帧的像素半径
    radius = float(spec.button_radius) * width / _REF_WIDTH
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(np.int16)
    sat = hsv[:, :, 1].astype(np.int16)
    val = hsv[:, :, 2].astype(np.int16)
    white = (sat < COLOR_WHITE_S_MAX) & (val > COLOR_WHITE_V_MIN)
    brown = (
        (hue > COLOR_BROWN_H_MIN)
        & (hue < COLOR_BROWN_H_MAX)
        & (sat > COLOR_BROWN_S_MIN)
        & (sat < COLOR_BROWN_S_MAX)
        & (val > COLOR_BROWN_V_MIN)
        & (val < COLOR_BROWN_V_MAX)
    )
    mask = ((white | brown).astype(np.uint8)) * 255

    # 形态学：先闭（补掉图标缝隙）再开（去掉细碎噪点）
    k = max(3, int(round(radius / 7)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _hierarchy = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    area_lo = np.pi * (COLOR_AREA_MIN_RATIO * radius) ** 2
    area_hi = np.pi * (COLOR_AREA_MAX_RATIO * radius) ** 2
    candidates: list[Circle] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < area_lo or area > area_hi:
            continue
        perimeter = float(cv2.arcLength(contour, True))
        if perimeter <= 0.0:
            continue
        if 4.0 * np.pi * area / (perimeter * perimeter) <= COLOR_CIRCULARITY_MIN:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(contour)
        candidates.append(Circle(float(cx), float(cy), float(r)))
    if len(candidates) < per_row:
        return []

    # 1) 行聚类：按 y 排序，与当前行均值差 < 0.6R 归同一行
    candidates.sort(key=lambda c: c.y)
    row_groups: list[list[Circle]] = []
    for circle in candidates:
        if row_groups:
            row_mean_y = float(np.mean([c.y for c in row_groups[-1]]))
            if abs(circle.y - row_mean_y) < COLOR_ROW_TOL_RATIO * radius:
                row_groups[-1].append(circle)
                continue
        row_groups.append([circle])
    row_groups = [group for group in row_groups if len(group) >= 3]
    if len(row_groups) < rows_wanted:
        return []
    # 取点数最多的前 N 行（点数相同按 y 升序，保持稳定）
    row_groups.sort(key=lambda group: -len(group))
    row_groups = sorted(row_groups[:rows_wanted], key=lambda group: float(np.mean([c.y for c in group])))

    # 2) 列聚类：三行 x 合并 → 容差内聚成一列 → 用列间距补齐缺失列
    merged_x = sorted(c.x for group in row_groups for c in group)
    clusters: list[list[float]] = [[merged_x[0]]]
    for x in merged_x[1:]:
        if x - clusters[-1][-1] <= COLOR_COL_TOL_RATIO * radius:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    cluster_centers = [float(np.median(group)) for group in clusters]
    # 丢掉"只有零星圆点"的列：琴键列每列应有接近行数的圆点数，
    # 而背景符号（高音/低音谱号、加号）只会零星命中 1 个，混进来会让基准列整体左移。
    min_per_column = max(2, rows_wanted - 1)
    strong = [
        center
        for center, group in zip(cluster_centers, clusters, strict=False)
        if len(group) >= min_per_column
    ]
    if len(strong) >= per_row:
        cluster_centers = strong
    if len(cluster_centers) < 2:
        return []
    gaps = [b - a for a, b in zip(cluster_centers, cluster_centers[1:], strict=False)]
    step = float(np.median(gaps))
    if step <= 1.0:
        return []

    def _snap(base: float) -> tuple[list[float], int]:
        """按 `base + i*step` 排 7 列，就近吸附到真实列；返回 (列坐标, 吸附到的圆点总数)。"""
        xs: list[float] = []
        hits = 0
        for index in range(per_row):
            ideal = base + index * step
            nearest_idx = min(
                range(len(cluster_centers)),
                key=lambda k: abs(cluster_centers[k] - ideal),
            )
            nearest = cluster_centers[nearest_idx]
            if abs(nearest - ideal) <= 0.25 * step:
                xs.append(nearest)
                hits += len(clusters[nearest_idx])
            else:
                xs.append(ideal)
        return xs, hits

    # 基准列不能拍脑袋取最左：背景符号（谱号/加号）可能凑成一列，会让 7 列整体左移。
    # 试遍所有候选做基准，取"吸附到的真实圆点最多"的那组。
    best_xs: list[float] = []
    best_hits = -1
    for candidate in cluster_centers:
        xs, hits = _snap(candidate)
        if hits > best_hits:
            best_hits, best_xs = hits, xs
    column_xs = best_xs or [cluster_centers[0] + i * step for i in range(per_row)]

    row_ys = [float(np.mean([c.y for c in group])) for group in row_groups]
    radius_est = float(np.median([c.r for group in row_groups for c in group]))
    return [Circle(x, y, radius_est) for y in row_ys for x in column_xs]


def keygrid_from_circles(
    circles: Sequence[Circle], spec: InstrumentSpec, frame_w: int, frame_h: int
) -> KeyGrid:
    """把圆转成归一化 KeyGrid（按音名顺序排列，内部自行做行主序排序）。

    半径用 `spec.button_radius`（标定值），**不用** Hough 半径。
    """
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError("帧尺寸必须为正")
    ordered = _row_major(list(circles), spec.key_count)
    order = spec.note_order
    pts = [
        KeyPoint(u=c.x / frame_w, v=c.y / frame_h, r=spec.button_radius / _REF_WIDTH)
        for c, _note in zip(ordered, order, strict=False)
    ]
    return KeyGrid(points=tuple(pts), key_count=spec.key_count)


def keygrid_from_note_map(
    mapping: dict[str, Circle], spec: InstrumentSpec, frame_w: int, frame_h: int
) -> KeyGrid:
    """按 `circle_map_note` 的结果（note → circle）构造 KeyGrid，顺序最可靠。"""
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError("帧尺寸必须为正")
    missing = set(spec.note_order) - set(mapping)
    if missing:
        raise KeyGridMismatch(f"缺少音名: {sorted(missing)}")
    pts = [
        KeyPoint(
            u=mapping[note].x / frame_w,
            v=mapping[note].y / frame_h,
            r=spec.button_radius / _REF_WIDTH,
        )
        for note in spec.note_order
    ]
    return KeyGrid(points=tuple(pts), key_count=spec.key_count)


def keygrid_from_points(
    points_px: Sequence[tuple[float, float]], spec: InstrumentSpec, frame_w: int, frame_h: int
) -> KeyGrid:
    """手动点选兜底：用户按任意顺序点 N 个点，走一次网格校正后归一化。"""
    if len(points_px) != spec.key_count:
        raise KeyGridMismatch(f"手动点选 {len(points_px)} 个，应为 {spec.key_count} 个")
    circles = correct_button_coordinates([Circle(x, y, 0.0) for x, y in points_px])
    if len(circles) != spec.key_count:
        circles = [Circle(x, y, 0.0) for x, y in points_px]
    return keygrid_from_circles(circles, spec, frame_w, frame_h)
