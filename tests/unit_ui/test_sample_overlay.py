"""采样浮层渲染（M2o/M2p）：环形只盖外圈、方块按 `square_side` 盖、偏移会平移方块。

几何口径（单位都是键半径 R，测试里 R = 40px）：
- 环形：`ring_width 0.43R` → 内径 0.55R，外径 0.98R（22px ~ 39.2px）；
- 小方块：边长 `square_side × R`，M2p 起默认 1R（半边长 0.5R = 20px），之前是 2R。
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("numpy")

BASE = (120, 120, 120)  # 底色：三通道一致，任何色偏都能看出来
W = H = 400
RADIUS = 40.0
PADDING = 8  # preview.PADDING：窗口正好比画面大两圈 padding 时，帧像素 = 控件像素 - 8
SIZE = W + PADDING * 2
INNER = 0.55  # 全局 sampling 推出的环内径（0.98 - 0.43）
SIDE = 1.0  # 全局 sampling 的方块边长（× R）


def _widget() -> object:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from yqrec.types import KeyGrid, KeyPoint
    from yqrec.ui.preview import PreviewWidget

    app = QApplication.instance() or QApplication([])
    frame = np.full((H, W, 3), BASE, dtype=np.uint8)
    qimg = QImage(frame.data, W, H, 3 * W, QImage.Format.Format_RGB888).copy()

    widget = PreviewWidget()
    widget.resize(SIZE, SIZE)
    widget.show()
    widget.set_frame(qimg)
    app.processEvents()

    grid = KeyGrid(points=(KeyPoint(u=0.5, v=0.5, r=RADIUS / W),), key_count=1)
    return widget, grid, app


def _pixel(widget, fx: int, fy: int) -> tuple[int, int, int]:
    """取帧坐标 (fx, fy) 处的实际渲染色。"""
    color = widget.grab().toImage().pixelColor(fx + PADDING, fy + PADDING)
    return (color.red(), color.green(), color.blue())


def _is_tinted(rgb: tuple[int, int, int]) -> bool:
    """蓝色浮层：B 抬升、R 压低（底色是灰，方向明确）。"""
    r, g, b = rgb
    return b > BASE[2] + 12 and r < BASE[0] - 5


def test_ring_overlay_skips_the_center() -> None:
    widget, grid, app = _widget()
    widget.show_sample_overlay(grid, inner_ratio=INNER, square_side=SIDE, offset_y=0.0)
    app.processEvents()

    # 半径 40 → 外环 22..39.2；距中心 30px（水平）在环上
    assert _is_tinted(_pixel(widget, 230, 200)), "环带应被蓝色盖住"
    assert _pixel(widget, 200, 200) == BASE, "中心孔（0.55R 内）不该被涂色"
    widget.close()


def test_square_overlay_uses_half_side() -> None:
    """M2p：方块边长 = 1R（半边长 0.5R = 20px），四角不再被涂。"""
    widget, grid, app = _widget()
    widget.show_sample_overlay(grid, inner_ratio=0.0, square_side=SIDE, offset_y=0.0)
    app.processEvents()

    assert _is_tinted(_pixel(widget, 200, 200)), "方块连中心一起盖（与环形的关键差别）"
    assert _is_tinted(_pixel(widget, 215, 215)), "半对角 21px 仍在 1R 方块内"
    assert _pixel(widget, 230, 230) == BASE, "半对角 42px 已超出 1R 方块（旧 2R 时会被涂）"
    assert _pixel(widget, 200, 225) == BASE, "正下方 25px > 半边长 20px，框外"
    widget.close()


def test_square_side_scales_with_config() -> None:
    """边长跟着 `sampling.square_side_ratio` 走：2R 时必须盖回四角（旧行为可复原）。"""
    widget, grid, app = _widget()
    widget.show_sample_overlay(grid, inner_ratio=0.0, square_side=2.0, offset_y=0.0)
    app.processEvents()
    assert _is_tinted(_pixel(widget, 230, 230)), "边长 2R 时半对角 42px 应在框内"
    widget.close()


def test_ring_overlay_leaves_hole_and_outside_clean() -> None:
    """环形只画 22~39.2px 这一圈：孔内（<0.55R）与外径以外都不该被涂。

    注意 1R 方块的「四角」正好落在环带里（半对角 28.3px），所以这里不能用
    "方块角落"来区分两种模式 —— 用孔内点区分（方块模式会涂，环形不会）。
    """
    widget, grid, app = _widget()
    widget.show_sample_overlay(grid, inner_ratio=INNER, square_side=SIDE, offset_y=0.0)
    app.processEvents()
    assert _pixel(widget, 212, 212) == BASE, "孔内 17px 不该被涂（方块模式会涂）"
    assert _pixel(widget, 200, 242) == BASE, "42px 已超出外径 0.98R"
    widget.close()


def test_square_offset_moves_the_box_down() -> None:
    """`PreviewWidget.show_sample_overlay` 收的是**帧像素**；1920 基准的换算在 main_window。"""
    widget, grid, app = _widget()

    # 边长 1R → 半边长 20px，下边界 y=220；偏移 15px 后到 235
    widget.show_sample_overlay(grid, inner_ratio=0.0, square_side=SIDE, offset_y=0.0)
    app.processEvents()
    assert _pixel(widget, 200, 228) == BASE, "无偏移时 y=228 已在框外"

    widget.show_sample_overlay(grid, inner_ratio=0.0, square_side=SIDE, offset_y=15.0)
    app.processEvents()
    assert _is_tinted(_pixel(widget, 200, 228)), "中心下移 15px 后 y=228 应落进方块"
    assert _pixel(widget, 200, 242) == BASE, "y=242 仍在框外"
    widget.close()


def test_main_window_converts_offset_to_frame_pixels() -> None:
    """1920 基准的 `square_offset_y` 要按帧宽等比缩放后才交给预览层。"""
    widget, grid, app = _widget()
    dy_px = 25 * W / 1920.0  # 25px @1920 → 400 宽帧只有 5.2px
    widget.show_sample_overlay(grid, inner_ratio=0.0, square_side=SIDE, offset_y=dy_px)
    app.processEvents()
    # 1R 方块下边界 200+5.2+20 = 225.2：y=222 在里面，y=232 在外面
    assert _is_tinted(_pixel(widget, 200, 222)), "下移 5.2px 后 y=222 应进框"
    assert _pixel(widget, 200, 232) == BASE, "y=232 仍在框外"
    widget.close()


def test_clear_overlay_restores_the_frame() -> None:
    widget, grid, app = _widget()
    widget.show_sample_overlay(grid, inner_ratio=INNER, square_side=SIDE, offset_y=0.0)
    app.processEvents()
    assert widget.has_sample_overlay is True
    assert _is_tinted(_pixel(widget, 230, 200))

    widget.clear_sample_overlay()
    app.processEvents()
    assert widget.has_sample_overlay is False
    assert _pixel(widget, 230, 200) == BASE, "清除后应完全恢复原画面"
    widget.close()


def test_overlay_ignores_none_grid() -> None:
    widget, _grid, app = _widget()
    widget.show_sample_overlay(None, inner_ratio=INNER, square_side=SIDE, offset_y=0.0)
    app.processEvents()
    assert widget.has_sample_overlay is False
    widget.close()


def _is_reddish(rgb: tuple[int, int, int]) -> bool:
    """小红点：R 明显高于底色的另外两个通道。"""
    r, g, b = rgb
    return r > 170 and r - max(g, b) > 60


def _grid_with_dots(widget, grid, app, **sampling) -> None:
    """检测圈 + 采样点（M2q.3：点跟着 grid 常显，不再只在学基线时出现）。"""
    widget.set_sampling(**sampling)
    widget.set_grid(grid)
    app.processEvents()


def test_sampling_dots_are_drawn_when_grid_is_shown() -> None:
    """检测圈一显示就带小红点：内环 0.55R = 22px，起点在正上方。"""
    widget, grid, app = _widget()
    _grid_with_dots(widget, grid, app, inner_ratio=INNER, ring_points=16)

    assert _is_reddish(_pixel(widget, 200, 178)), "内环正上方应有一个红点"
    assert _is_reddish(_pixel(widget, 222, 200)), "顺时针第 5 个点（正右方）也应有红点"
    assert not _is_reddish(_pixel(widget, 200, 200)), "内环以内不画点（避开中心音符）"
    widget.close()


def test_sampling_dots_follow_square_edges() -> None:
    """方块模式：点落在四条边上（左上角起点、半边长 0.5R = 20px）。"""
    widget, grid, app = _widget()
    _grid_with_dots(widget, grid, app, inner_ratio=0.0, square_side=SIDE, square_points=8)

    assert _is_reddish(_pixel(widget, 180, 180)), "左上角是起点"
    assert _is_reddish(_pixel(widget, 220, 220)), "右下角也有点"
    assert not _is_reddish(_pixel(widget, 200, 200)), "方块中心不画点"
    widget.close()


def test_dots_move_when_radius_changes() -> None:
    """拖「半径」滑条 → 点立刻换到新半径上（0.55R → 0.80R）。"""
    widget, grid, app = _widget()
    _grid_with_dots(widget, grid, app, inner_ratio=0.55, ring_points=8)
    assert _is_reddish(_pixel(widget, 200, 178)), "0.55R = 22px 处有点"

    widget.set_sampling(inner_ratio=0.80, ring_points=8)
    app.processEvents()
    assert not _is_reddish(_pixel(widget, 200, 178)), "旧半径上的点要消失"
    assert _is_reddish(_pixel(widget, 200, 168)), "0.80R = 32px 处出现新点"
    widget.close()


def test_dots_stay_when_baseline_overlay_clears() -> None:
    """学基线的浮层清掉后，红点仍常显（它们属于检测圈本身）。"""
    widget, grid, app = _widget()
    _grid_with_dots(widget, grid, app, inner_ratio=INNER, ring_points=16)
    widget.show_sample_overlay(
        grid, inner_ratio=INNER, square_side=SIDE, offset_y=0.0, point_mode="points"
    )
    app.processEvents()

    widget.clear_sample_overlay()
    app.processEvents()
    assert widget.has_sample_overlay is False
    assert _is_reddish(_pixel(widget, 200, 178)), "红点不该跟着浮层一起消失"
    widget.close()


def test_dots_can_be_turned_off() -> None:
    widget, grid, app = _widget()
    _grid_with_dots(widget, grid, app, inner_ratio=INNER, ring_points=16, show_points=False)
    assert not _is_reddish(_pixel(widget, 200, 178))
    widget.close()


def test_pressed_circle_fill_is_lighter_than_before() -> None:
    """按下时的绿色要淡（填充不透明度 ≈14%），保证能透过圈看清捕获画面。

    颜色统一走 `theme.OVERLAY` 令牌（设计包要求：叠加层不跟主题走，
    但也不许在绘制代码里写死 `QColor(...)`）。
    """
    from yqrec.ui import theme

    assert theme.overlay("press-fill").alpha() <= 60, "用户要求调低按下时的绿色不透明度"

    widget, grid, app = _widget()
    widget.set_sampling(show_points=False)  # 只看填充，避免红点干扰取色
    widget.set_grid(grid)
    widget.set_press([True])
    app.processEvents()

    # 圈内（半径 40，取中心上方 20px）：应偏绿，但底色仍透得出来
    inside = _pixel(widget, 200, 180)
    assert inside[1] > inside[0], "按下应偏绿"
    assert inside[0] > 85, f"底色没透出来（R={inside[0]}），填充太实"

    # 圈外仍是纯底色
    assert _pixel(widget, 300, 300) == BASE
    widget.close()
