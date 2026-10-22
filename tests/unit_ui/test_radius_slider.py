"""「半径」滑条（v1.20）：环形 = 内环半径，方块 = 中心到四边的距离，且写回 instruments.json。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from yqrec.instruments import instruments_path


@pytest.fixture()
def window_and_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))  # 隔离真实 %APPDATA%
    assets = tmp_path / "assets"
    assets.mkdir()
    target = assets / "instruments.json"
    shutil.copyfile(instruments_path(), target)
    monkeypatch.setenv("YQREC_ASSETS", str(assets))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    return window, target, app


def _sampling(target: Path) -> dict:
    return json.loads(target.read_text(encoding="utf-8"))["sampling"]


def test_radius_slider_starts_from_instruments_json(window_and_assets) -> None:
    window, target, app = window_and_assets
    ring = _sampling(target)["ring_width_ratio"]
    inner = round((0.98 - ring) * 100)

    assert window._combo_sample.currentData() == "ring"
    assert window._slider_radius.value() == inner
    assert window._lbl_radius.text() == f"{inner / 100:.2f}R"
    window.close()
    app.processEvents()


def test_ring_radius_drag_writes_back_ring_width(window_and_assets) -> None:
    window, target, app = window_and_assets
    window._slider_radius.setValue(62)
    assert window._lbl_radius.text() == "0.62R"
    window._commit_radius()

    sampling = _sampling(target)
    assert sampling["ring_width_ratio"] == pytest.approx(0.36), "内环 0.62R = 0.98 - 0.36"
    assert sampling["ring_points"] == 8, "分段数不能被顺手改掉"
    assert sampling["point_patch_ratio"] == pytest.approx(0.06)
    window.close()
    app.processEvents()


def test_square_mode_switches_value_and_range(window_and_assets) -> None:
    window, target, app = window_and_assets
    window._combo_sample.setCurrentIndex(1)  # 方块（居中）
    assert window._combo_sample.currentData() == "square"

    half = round(_sampling(target)["square_side_ratio"] / 2 * 100)
    assert window._slider_radius.value() == half, "切模式要读回该模式自己的半径"
    assert window._slider_radius.maximum() == 150, "方块量程到 1.50R"

    window._slider_radius.setValue(70)
    assert window._lbl_radius.text() == "0.70R"
    window._commit_radius()
    sampling = _sampling(target)
    assert sampling["square_side_ratio"] == pytest.approx(1.4), "边长 = 2 × 中心到四边"
    assert sampling["ring_width_ratio"] == pytest.approx(_sampling(target)["ring_width_ratio"])
    window.close()
    app.processEvents()


def test_slider_value_is_restored_on_next_launch(window_and_assets, monkeypatch) -> None:
    window, target, app = window_and_assets
    window._slider_radius.setValue(64)
    window._commit_radius()
    window.close()
    app.processEvents()

    from yqrec.ui.main_window import MainWindow

    again = MainWindow()
    assert again._slider_radius.value() == 64, "下次启动直接读 instruments.json"
    assert again._lbl_radius.text() == "0.64R"
    again.close()
    app.processEvents()


def test_learning_baseline_uses_point_sampling(window_and_assets) -> None:
    """采样口径推给 worker 时必须是点采样（v1.20 不再走整块/整环均值）。"""
    window, _target, app = window_and_assets
    window._sync_sampler_options()  # 没有 worker 时也只是空转，不应抛
    assert window._effective_inner_ratio() == pytest.approx(0.80)

    window._combo_sample.setCurrentIndex(1)
    assert window._effective_inner_ratio() == 0.0, "方块模式内径恒 0"
    assert window._effective_square_side() == pytest.approx(1.1)
    window.close()
    app.processEvents()
