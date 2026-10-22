"""配置读取策略（M2q.2）：启动读一次 + 按需重读，运行期**不再定时轮询**。

用户诉求：`instruments.json` 以前每 1.5s 看一眼有没有改，想改成"启动加载一次、运行时不查"，
以减少开销（也避免录制途中突然生效）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from yqrec.instruments import instruments_path


@pytest.fixture()
def window_and_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
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


def _patch_ring_width(target: Path, value: float) -> None:
    data = json.loads(target.read_text(encoding="utf-8"))
    data["sampling"]["ring_width_ratio"] = value
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_no_polling_timer_exists(window_and_json) -> None:
    """M2q.2：不再有定时器轮询 instruments.json。"""
    window, _target, app = window_and_json
    assert not hasattr(window, "_sampling_timer"), "旧的轮询定时器应已删除"
    window.close()
    app.processEvents()


def test_file_change_alone_does_not_apply(window_and_json) -> None:
    """只改文件、不触发重读 → 内存里的口径不变（这正是用户要的"运行时不查"）。"""
    window, target, app = window_and_json
    before = window._sampling.ring_inner_ratio

    _patch_ring_width(target, 0.30)  # → 内环 0.68R
    for _ in range(3):
        app.processEvents()

    assert window._sampling.ring_inner_ratio == pytest.approx(before), "没触发重读就不该变"
    window.close()
    app.processEvents()


def test_manual_reload_picks_up_file_change(window_and_json) -> None:
    """F5 / `_reload_instruments` 才会真正读盘（并同步滑条）。"""
    window, target, app = window_and_json
    _patch_ring_width(target, 0.30)

    assert window._reload_instruments(source="F5 手动重读", manual=True) is True
    assert window._sampling.ring_inner_ratio == pytest.approx(0.68)
    assert window._slider_radius.value() == 68, "半径滑条要跟着刷新"
    assert window._lbl_radius.text() == "0.68R"

    # 再按一次：文件没变 → 返回 False（不重复干活）
    assert window._reload_instruments(source="F5 手动重读", manual=True) is False
    window.close()
    app.processEvents()


def test_reload_reports_broken_json_without_crashing(window_and_json) -> None:
    """文件被改坏 → 沿用旧参数，不抛。"""
    window, target, app = window_and_json
    before = window._sampling.ring_inner_ratio
    target.write_text('{"instruments": [oops', encoding="utf-8")

    assert window._reload_instruments(source="F5 手动重读", manual=True) is False
    assert window._sampling.ring_inner_ratio == pytest.approx(before)
    window.close()
    app.processEvents()


def test_reload_pending_radius_is_flushed_first(window_and_json) -> None:
    """滑条还在 400ms 防抖里没落盘时重读 → 先把半径写掉，不能丢。"""
    window, target, app = window_and_json
    window._slider_radius.setValue(64)
    assert window._radius_timer is not None and window._radius_timer.isActive()

    window._reload_instruments(source="F5 手动重读", manual=True)

    sampling = json.loads(target.read_text(encoding="utf-8"))["sampling"]
    assert sampling["ring_width_ratio"] == pytest.approx(0.34), "0.98 - 0.64"
    assert window._sampling.ring_inner_ratio == pytest.approx(0.64)
    window.close()
    app.processEvents()
