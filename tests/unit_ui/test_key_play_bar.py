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

    def describe(self) -> str:
        return "测试通道"

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

    # 起播倒计时默认关掉（延迟行为另有专门用例），否则每个用例都要等 3 秒。
    # 打桩打**全局读取函数**而不是 `_key_start_delay_ms()` 本身：
    # 这样"延迟从哪来"这条链路仍然被测到（M6f 改成全局参数后尤其重要）。
    from yqrec.ui import main_window as mw_mod

    monkeypatch.setattr(mw_mod, "load_key_play_delay_ms", lambda: 0)

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
    assert not hasattr(win, "_btn_key_mode_entry"), "M6e：入口处的通道胶囊已删除"
    win.close()


def test_mode_button_lives_in_play_bar(window, monkeypatch: pytest.MonkeyPatch) -> None:
    """通道胶囊只在演奏控制条里：点一下就切并落盘（不做入口预选）。"""
    win, app = window
    saved: list[int] = []
    monkeypatch.setattr(win, "_save_app_config", lambda: saved.append(1))
    monkeypatch.setattr(win, "_make_key_sink", lambda: RecordingSink())

    assert win._config.ui.key_play_mode == "background", "默认后台（M6c）"
    assert win._btn_key_mode.text() == "后台"
    assert win._btn_key_mode.isChecked() is False, "后台不是激活态"
    assert win._btn_key_mode.isVisibleTo(win) is False, "没进演奏时不显示"

    win._btn_play_keys.click()
    app.processEvents()
    assert win._btn_key_mode.isVisibleTo(win) is True

    win._btn_key_mode.click()
    app.processEvents()
    assert win._config.ui.key_play_mode == "foreground"
    assert win._btn_key_mode.text() == "前台"
    assert win._btn_key_mode.isChecked() is True, "前台 = 按键激活效果"
    assert saved, "选择要落盘，下次启动沿用"
    win._stop_playback()
    win.close()


def test_target_falls_back_to_window_below(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """没框选区域、前台又是本程序时，用 z-order 下面那个窗口（不再强制先框选）。"""
    from yqrec import inputsim

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    win = MainWindow()
    win._region = None  # 没框选
    # offscreen 平台的 winId 不是真实句柄（is_own_window 认不出来）→ 直接指定"谁是自己"
    monkeypatch.setattr(inputsim, "is_own_window", lambda h: int(h) == int(win.winId()))
    monkeypatch.setattr(inputsim, "window_at", lambda *a: 0)
    monkeypatch.setattr(inputsim, "key_target_window", lambda: 0xBEEF)
    # 模拟"用户刚点了本程序的按钮"：前台=本程序，且没有采样到外部窗口
    monkeypatch.setattr(inputsim, "foreground_window", lambda: int(win.winId()))
    win._last_external_hwnd = 0
    assert win._key_target_hwnd() == 0xBEEF, "前台是本程序时不该直接判 0 报错"

    # 有"最近用过的外部窗口"（还活着就当它）：优先于 z-order，避免投给下面那个别的程序
    monkeypatch.setattr(inputsim, "is_window", lambda h: h == 0x7777)
    win._last_external_hwnd = 0x7777
    assert win._key_target_hwnd() == 0x7777

    # 前台是外部程序 → 用它（用户此刻正在用的那个）；采样器也会把它记下来
    monkeypatch.setattr(inputsim, "foreground_window", lambda: 0x9999)
    monkeypatch.setattr(inputsim, "is_app_window", lambda h: True)
    win._sample_external_foreground()
    assert win._last_external_hwnd == 0x9999
    assert win._key_target_hwnd() == 0x9999
    win.close()


def test_foreground_mode_never_steals_focus(window, monkeypatch: pytest.MonkeyPatch) -> None:
    """前台模式：不抢焦点；当前激活是本程序时**逐拍跳过**，但演奏不停（M6d）。"""
    from yqrec import inputsim

    win, app = window
    win._config.ui.key_play_mode = "foreground"
    sink = inputsim.ForegroundKeys(0x1234)
    win._key_sink = sink
    calls: list[int] = []
    monkeypatch.setattr(inputsim, "activate_window", lambda hwnd: calls.append(hwnd) or True)
    monkeypatch.setattr(inputsim, "foreground_window", lambda: int(win.winId()))
    monkeypatch.setattr(inputsim, "is_own_window", lambda h: int(h) == int(win.winId()))
    assert sink.ready() is False, "激活的是本程序 → 不发键"
    assert win._can_inject_now() is True, "但绝不能因此暂停（否则前台模式永远起不来）"
    assert calls == [], "用户明确要求不要替人抢焦点"

    monkeypatch.setattr(inputsim, "foreground_window", lambda: 0x1234)
    monkeypatch.setattr(inputsim, "is_own_window", lambda h: False)
    assert sink.ready() is True, "切到别的窗口就能发（前台语义 = 发给当前激活窗口）"
    win._key_sink = None
    win.close()


def test_foreground_mode_requires_no_target_window(window, monkeypatch) -> None:
    """前台不需要事先知道目标窗口：句柄为 0 也照样能造出通道（不强制先框选）。"""
    from yqrec import inputsim

    win, app = window
    win._config.ui.key_play_mode = "foreground"
    monkeypatch.setattr(win, "_key_target_hwnd", lambda: 0)
    sink = win._make_key_sink()
    assert isinstance(sink, inputsim.ForegroundKeys), "前台不该因为没目标就报错"

    win._config.ui.key_play_mode = "background"
    assert win._make_key_sink() is None, "后台没目标窗口时确实投不出去 → 报错"
    win.close()


def test_start_playback_keeps_playing_when_own_window_active(
    window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点「演奏」时前台必然是本程序 —— 必须直接起播，不许暂停（M6d 用户反馈）。"""
    from yqrec import inputsim

    win, app = window
    win._config.ui.key_play_mode = "foreground"
    monkeypatch.setattr(win, "_make_key_sink", lambda: inputsim.ForegroundKeys(0x1234))
    monkeypatch.setattr(inputsim, "foreground_window", lambda: int(win.winId()))
    monkeypatch.setattr(inputsim, "is_own_window", lambda h: True)

    win._btn_play_keys.click()
    app.processEvents()
    assert win._play_mode is True
    assert win._paused is False, "前台模式不许因为'当前激活是本程序'就暂停"
    played = [c for c in win.calls if c[0] in ("play_from", "resume")]
    assert played, "必须真的起播：用户切到游戏窗口后剩下的音要接上"
    win._stop_playback()
    win.close()


def test_resume_does_not_need_rearm(window, monkeypatch: pytest.MonkeyPatch) -> None:
    """暂停后按 ▶：前台模式也要无条件继续（焦点对不对由注入层逐拍判断）。"""
    from yqrec import inputsim

    win, app = window
    win._config.ui.key_play_mode = "foreground"
    win._key_sink = inputsim.ForegroundKeys(0x1234)
    monkeypatch.setattr(inputsim, "foreground_window", lambda: int(win.winId()))
    monkeypatch.setattr(inputsim, "is_own_window", lambda h: True)

    win._play_kind = "keys"
    win._play_mode = True
    win._paused = True
    win._cursor = win._paused_at = 0
    win._toggle_pause()
    assert win._paused is False, "继续必须生效"
    win._key_sink = None
    win._play_mode = False
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

    win._btn_play_keys.click()
    app.processEvents()

    assert win._play_mode is True
    assert win._play_kind == "keys"
    assert win._play_bar.isVisibleTo(win) is True
    assert win._btn_play.isVisibleTo(win) is False
    assert win._btn_play_keys.isVisibleTo(win) is False
    assert win._btn_key_mode.isVisibleTo(win) is True, "演奏时才有注入通道开关"
    played = [c for c in win.calls if c[0] in ("play_from", "resume")]
    assert played, "进入演奏必须真的开始播（延迟为 0 时立刻开始）"
    win.close()


def test_stop_returns_to_both_entries(window, monkeypatch: pytest.MonkeyPatch) -> None:
    win, app = window
    sink = RecordingSink()
    monkeypatch.setattr(win, "_make_key_sink", lambda: sink)

    win._btn_play_keys.click()
    app.processEvents()
    win._stop_playback()
    app.processEvents()

    assert win._play_mode is False
    assert win._play_kind == "audio", "退出后回到默认（音频）语义"
    assert win._btn_play.isVisibleTo(win) is True
    # 入口是否露出来由开关决定（当前临时隐藏），退出演奏后要回到开关的状态
    from yqrec.ui.main_window import KEY_PLAY_ENTRY_VISIBLE

    assert win._btn_play_keys.isVisibleTo(win) is KEY_PLAY_ENTRY_VISIBLE
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


def test_key_play_delay_counts_down_before_sending(window, monkeypatch) -> None:
    """M6e：起播延迟 —— 倒计时期间一个键都不发。"""
    win, app = window
    monkeypatch.setattr(win, "_key_start_delay_ms", lambda: 1000)
    monkeypatch.setattr(win, "_make_key_sink", lambda: RecordingSink())

    win._btn_play_keys.click()
    app.processEvents()
    assert win._play_mode is True and win._paused is True, "倒计时期间停在起点"
    assert not [c for c in win.calls if c[0] in ("play_from", "resume")], "倒计时期间不许起播"
    assert "1 秒后开始演奏" in win._lbl_toast.text(), win._lbl_toast.text()

    win._delay_deadline = 0.0  # 把截止时刻推到过去，模拟"时间到"
    win._tick_key_delay()
    assert win._paused is False, "到点必须起播"
    assert [c for c in win.calls if c[0] in ("play_from", "resume")], "倒计时结束后要真的有起播动作"
    win._stop_playback()
    win.close()


def test_pause_resume_also_counts_down(window, monkeypatch) -> None:
    """暂停后点继续也要先倒计时（用户明确要求：暂停→播放同样延后输入）。"""
    win, app = window
    monkeypatch.setattr(win, "_make_key_sink", lambda: RecordingSink())
    win._btn_play_keys.click()
    app.processEvents()
    win._toggle_pause()  # 暂停
    assert win._paused is True

    monkeypatch.setattr(win, "_key_start_delay_ms", lambda: 3000)
    before = list(win.calls)
    win._toggle_pause()  # 继续 → 先倒计时
    assert win._paused is True, "倒计时期间仍然算暂停"
    assert win.calls == before, "不许立刻起播"
    assert "秒后开始演奏" in win._lbl_toast.text(), win._lbl_toast.text()
    win._stop_playback()
    win.close()


def test_delay_cancelled_by_stop(window, monkeypatch) -> None:
    """倒计时中按「停止」必须把计时器停掉，否则它会把播放又拉起来。"""
    win, app = window
    monkeypatch.setattr(win, "_key_start_delay_ms", lambda: 3000)
    monkeypatch.setattr(win, "_make_key_sink", lambda: RecordingSink())
    win._btn_play_keys.click()
    app.processEvents()
    assert win._delay_timer.isActive() is True

    win._stop_playback()
    app.processEvents()
    assert win._delay_timer.isActive() is False
    assert win._play_mode is False
    win.close()


def test_start_delay_reads_global_param(window, monkeypatch) -> None:
    """M6f：延迟是**全局**参数 —— 换乐器/不换都读同一个值，且不看乐器 spec。"""
    from yqrec.ui import main_window as mw

    win, _app = window
    win._play_kind = "keys"
    monkeypatch.setattr(mw, "load_key_play_delay_ms", lambda: 2500)
    assert win._key_start_delay_ms() == 2500
    assert not hasattr(win._spec, "key_play_delay_ms"), "乐器级字段已删除"

    win._play_kind = "audio"
    assert win._key_start_delay_ms() == 0, "试听不需要倒计时"

    win._play_kind = "keys"

    def boom() -> int:
        raise OSError("json 读不到")

    monkeypatch.setattr(mw, "load_key_play_delay_ms", boom)
    assert win._key_start_delay_ms() == 0, "配置读不到时不许拦下演奏"
    win.close()
