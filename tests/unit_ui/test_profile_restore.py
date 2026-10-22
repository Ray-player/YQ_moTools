"""配置档恢复（v1.20）：逐键基线回来才恢复判按，旧档一律要求重学。

判据换代后旧档里存的 `mode` / `base_*` / `weights` 已经不可信：
- 有 `baseline` → 用**当前** spec 重算阈值（判据 / 权重 / 死区全按新 JSON）；
- 没有 `baseline`（M2p 及更早）→ 丢掉阈值，只保留键位与区域，提示重学基线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _profile(with_baseline: bool) -> dict:
    profile = {
        "name": "default",
        "instrument": "xljq",
        "backend": "auto",
        "ref_size": [800, 400],
        "created": "2026-10-21T11:00:00",
        "target": {"kind": "screen", "monitor": 0, "rect": [0, 0, 800, 400]},
        "keygrid": {
            "points": [[0.1 + 0.04 * i, 0.8, 50 / 1920] for i in range(21)],
            "key_count": 21,
        },
        "thresholds": {
            "main": {
                "min_r": 0,
                "max_r": 190,
                "mode": "max_drop",
                "base_r": 200,
                "base_g": 200,
                "base_b": 200,
                "delta": 40,
            }
        },
    }
    if with_baseline:
        profile["baseline"] = [[210, 205, 200]] * 21
    return profile


@pytest.fixture()
def make_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app, tmp_path


def _write_profile(tmp_path: Path, data: dict) -> None:
    target = tmp_path / "YQ_moTools" / "profiles" / "default.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_profile_without_baseline_is_not_restored_as_thresholds(make_window) -> None:
    app, tmp_path = make_window
    _write_profile(tmp_path, _profile(with_baseline=False))

    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    assert window._baseline is None
    assert window._thresholds == {}, "旧档的 max_drop 阈值不能带进新判据"
    assert window._grid is not None, "键位还是要恢复的（省得重新点选）"
    window.close()
    app.processEvents()


def test_profile_with_baseline_restores_per_key_judging(make_window) -> None:
    app, tmp_path = make_window
    _write_profile(tmp_path, _profile(with_baseline=True))

    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    assert window._baseline is not None and window._baseline.shape == (21, 3)
    assert set(window._thresholds) == {"main"}
    threshold = window._thresholds["main"]
    assert threshold.mode == "weighted", "阈值要用当前 JSON 的判据重算"
    assert threshold.base_r == 210, "base 取恢复出来的逐键基线均值"
    assert threshold.delta == 20, "Δ 取当前 instruments.json 里该乐器的值（M3d 统一调成 20）"
    assert window._combo_backend.currentData() == "hdr", (
        "截图方式不再从标定档恢复（M3e）：backend 是「上次用的后端」，存在 settings.json；"
        "标定档里的 auto/dxgi 只作记录，这里应落在默认 j-HDR"
    )
    window.close()
    app.processEvents()
