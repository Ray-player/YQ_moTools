"""按键演奏（M5）的界面行为：入口在「试听」左侧、原地换成同一条控制条。

注入层全部打桩（`RecordingSink`），测试**不碰真实键鼠**；
真注入的顺序由 `tests/test_key_play_timeline.py` 覆盖。
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")


class RecordingSink:
    """记账用注入通道。"""

    mode = "background"

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def key_down(self, key: str) -> bool:
        self.events.append(("down", key))
        return True

    def key_up(self, key: str) -> bool:
        self.events.append(("up", key))
        return True

    def release_all(self) -> None:
        self.events.append(("release", ""))

    def ready(self) -> bool:
        return True


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    text = "A100S"
    win._set_record_text(text)
    win._playback_score = bgi_sheet.score_from_tick_text(text)
    win._refresh_playback_enabled()

    calls: list[tuple] = []
    win._playback.play_from = lambda i=0: (calls.append(("play_from", i)), True)[1]
    win._playback.pause = lambda: calls.append(("pause",))
    win._playback.resume = lambda: (calls.append(("resume",)), True)[1]
    win._playback.stop = lambda **k: calls.append(("stop",))
    win.calls = calls  # type: ignore[attr-defined]
    return win, app


def test_entry_sits_left_of_preview(window) -> None:
    win, _app = window
    row = win._actions_row
    order = [row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
    assert order.index(win._btn_play_keys) < order.index(win._btn_play), "「演奏」在「试听」左侧"
    assert order.index(win._btn_key_mode_entry) < order.index(
        win._btn_play_keys
    ), "通道胶囊在「演奏」左侧"
    win.close()


def test_entry_mode_toggle_sets_saved_mode(window, monkeypatch: pytest.MonkeyPatch) -> None:
    """入口胶囊决定"进去走哪条通道"，点一下就切并落盘。"""
    win, app = window
    saved: list[int] = []
    monkeypatch.setattr(win, "_save_app_config", lambda: saved.append(1))

    assert win._config.ui.key_play_mode == "foreground", "默认前台"
    assert win._btn_key_mode_entry.text() == "前台"

    win._btn_key_mode_entry.click()
    app.processEvents()
    assert win._config.ui.key_play_mode == "background"
    assert win._btn_key_mode_entry.text() == "后台"
    assert win._btn_key_mode.text() == "后台", "播放条里那个胶囊跟着同步（同一个开关）"
    assert saved, "选择要落盘，下次启动沿用"

    win._btn_key_mode_entry.click()
    app.processEvents()
    assert win._config.ui.key_play_mode == "foreground"
    assert win._btn_key_mode_entry.text() == "前台"
    win.close()


def test_entry_mode_button_hidden_while_playing(window, monkeypatch: pytest.MonkeyPatch) -> None:
    win, app = window
    monkeypatch.setattr(win, "_make_key_sink", RecordingSink)
    monkeypatch.setattr(win, "_rearm_key_sink", lambda: True)

    win._btn_play_keys.click()
    app.processEvents()
    assert win._btn_key_mode_entry.isVisibleTo(win) is False, "演奏中由控制条里那个接管"
    win._stop_playback()
    app.processEvents()
    assert win._btn_key_mode_entry.isVisibleTo(win) is True
    win.close()


def test_entry_mode_uses_background_sink(window, monkeypatch: pytest.MonkeyPatch) -> None:
    """选了后台，点「演奏」就造后台通道（不经过前台抢焦点那条路）。"""
    win, app = window
    win._config.ui.key_play_mode = "background"
    win._sync_key_mode_button()
    made: list[str] = []

    def fake_sink():
        made.append(win._config.ui.key_play_mode)
        return RecordingSink()

    monkeypatch.setattr(win, "_make_key_sink", fake_sink)
    monkeypatch.setattr(win, "_rearm_key_sink", lambda: True)
    win._btn_play_keys.click()
    app.processEvents()
    assert made == ["background"]
    assert win._play_kind == "keys"
    win._stop_playback()
    win.close()


def test_entry_needs_score(window) -> None:
    win, _app = window
    assert win._btn_play_keys.isEnabled() is True
    win._set_record_text("")
    assert win._btn_play_keys.isEnabled() is False
    win.close()


def test_click_swaps_entry_for_play_bar(window, monkeypatch: pytest.MonkeyPatch) -> None:
    win, app = window
    sink = RecordingSink()
    monkeypatch.setattr(win, "_make_key_sink", lambda: sink)
    monkeypatch.setattr(win, "_rearm_key_sink", lambda: True)

    win._btn_play_keys.click()
    app.processEvents()

    assert win._play_mode is True
    assert win._play_kind == "keys"
    assert win._play_bar.isVisibleTo(win) is True
    assert win._btn_play.isVisibleTo(win) is False
    assert win._btn_play_keys.isVisibleTo(win) is False
    assert win._btn_key_mode.isVisibleTo(win) is True, "演奏时才有注入通道开关"
    assert win.calls[-1] == ("play_from", 0)
    win.close()


def test_stop_returns_to_both_entries(window, monkeypatch: pytest.MonkeyPatch) -> None:
    win, app = window
    sink = RecordingSink()
    monkeypatch.setattr(win, "_make_key_sink", lambda: sink)
    monkeypatch.setattr(win, "_rearm_key_sink", lambda: True)

    win._btn_play_keys.click()
    app.processEvents()
    win._stop_playback()
    app.processEvents()

    assert win._play_mode is False
    assert win._play_kind == "audio", "退出后回到默认（音频）语义"
    assert win._btn_play.isVisibleTo(win) is True
    assert win._btn_play_keys.isVisibleTo(win) is True
    assert win._btn_key_mode.isVisibleTo(win) is False
    assert sink.events[-1] == ("release", ""), "退出时兜底松开所有键"
    win.close()


def test_audio_mode_keeps_mode_switch_hidden(window) -> None:
    win, app = window
    win._toggle_playback()  # 试听：控制条里不该出现通道开关
    app.processEvents()
    assert win._play_kind == "audio"
    assert win._btn_key_mode.isVisibleTo(win) is False
    win.close()


def test_controller_keys_mode_injects(spec_fwzs) -> None:
    """控制器在 keys 模式下真的调 sink（不打桩、真跑线程）。"""
    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.player.engine import PlayerState
    from yqrec.ui.playback import MODE_KEYS, PlaybackController

    QApplication.instance() or QApplication([])
    sink = RecordingSink()
    controller = PlaybackController(audio_enabled=False)
    controller.load(
        bgi_sheet.score_from_tick_text("A"), spec_fwzs, mode=MODE_KEYS, sink=sink
    )
    assert controller.mode == MODE_KEYS
    assert controller.play_from(0) is True

    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        QApplication.processEvents()
        if not controller._player or controller._player.state is PlayerState.IDLE:
            break
        time.sleep(0.01)

    assert sink.events[0] == ("down", "A")
    assert ("up", "A") in sink.events
    assert sink.events[-1] == ("release", "")
    controller.close()
