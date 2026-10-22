"""试听控制条（M2q.9）：播放/暂停/停止、快退快进只在暂停时可用、高亮不发声。

交互约定（用户定）：
1. 点「播放试听」→ **原地**换成「⏪ ⏸/▶ ⏩ 停止试听」，并立刻开始播放；
2. 播放中 ⏪/⏩ 禁用；暂停后才可用，且只移动高亮、不发声；
3. 暂停后继续：位置没动就无缝 resume，动过就从新高亮处重开；
4. 「停止试听」→ 停播 + 控制条换回入口按钮。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()

    # 塞一份 5 个和弦的谱面（A F G (DGH) J）
    text = "300A400F500G600(DGH)700J"
    win._set_record_text(text)
    win._playback_score = bgi_sheet.score_from_tick_text(text)
    win._refresh_playback_enabled()

    # 换成桩：不真播音频/不开线程，只记录调用
    calls: list[tuple] = []
    win._playback.play_from = lambda i=0: (calls.append(("play_from", i)), True)[1]
    win._playback.pause = lambda: calls.append(("pause",))
    win._playback.resume = lambda: (calls.append(("resume",)), True)[1]
    win._playback.stop = lambda **k: calls.append(("stop",))
    win.calls = calls  # type: ignore[attr-defined]
    return win, app


def test_enter_play_mode_swaps_button_in_place(window) -> None:
    win, app = window
    assert win._btn_play.isVisibleTo(win) is True
    assert win._play_bar.isVisibleTo(win) is False

    win._toggle_playback()
    app.processEvents()

    assert win._play_mode is True
    assert win._paused is False, "进入试听模式默认就是播放状态"
    assert win._play_bar.isVisibleTo(win) is True, "控制条应显示"
    assert win._btn_play.isVisibleTo(win) is False, "入口按钮应被替换掉"
    assert win.calls[-1] == ("play_from", 0), "应从头开始播（load() 里的 stop 是换谱面用的）"
    win.close()


def test_step_buttons_disabled_while_playing(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()

    assert win._btn_rewind.isEnabled() is False, "播放中不能快退"
    assert win._btn_forward.isEnabled() is False, "播放中不能快进"

    win._toggle_pause()
    app.processEvents()
    assert win._paused is True
    assert win._btn_rewind.isEnabled() is True, "暂停后才放开快退"
    assert win._btn_forward.isEnabled() is True, "暂停后才放开快进"
    win.close()


def test_step_moves_highlight_without_playing(window) -> None:
    """快进/快退只动高亮，不调用任何播放接口。"""
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    app.processEvents()
    before = len(win.calls)

    for _ in range(3):
        win._btn_forward.click()
    assert win._cursor == 3, "前进 3 个和弦"
    for _ in range(2):
        win._btn_rewind.click()
    assert win._cursor == 1
    assert len(win.calls) == before, "移动高亮不能触发播放（不发声）"

    # 高亮真的画在对应 token 上
    selections = win._record_view.extraSelections()
    assert len(selections) == 1
    start, end = win._tick_spans[win._cursor]
    assert (selections[0].cursor.selectionStart(), selections[0].cursor.selectionEnd()) == (
        start,
        end,
    )
    win.close()


def test_step_clamps_at_both_ends(window) -> None:
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    for _ in range(20):
        win._btn_forward.click()
    assert win._cursor == len(win._tick_spans) - 1, "到最后一个和弦就停住"
    for _ in range(20):
        win._btn_rewind.click()
    assert win._cursor == 0, "到第一个和弦就停住"
    win.close()


def test_resume_is_seamless_when_position_unchanged(window) -> None:
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    win._toggle_pause()  # 直接继续，没动位置
    assert win.calls[-1] == ("resume",), "位置没动 → 无缝 resume（不重新 seek）"
    assert win._paused is False
    win.close()


def test_resume_seeks_when_position_moved(window) -> None:
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    for _ in range(2):
        win._btn_forward.click()  # 移到第 3 个和弦
    win._toggle_pause()
    assert win.calls[-1] == ("play_from", 2), "位置动过 → 从新高亮处重开"
    assert win._paused is False
    win.close()


def test_stop_restores_entry_button(window) -> None:
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    win._btn_play_stop.click()
    app.processEvents()

    assert win._play_mode is False
    assert win.calls[-1] == ("stop",)
    assert win._play_bar.isVisibleTo(win) is False, "控制条收起"
    assert win._btn_play.isVisibleTo(win) is True, "入口按钮回来"
    assert win._record_view.extraSelections() == [], "高亮要清掉"
    win.close()


def test_natural_finish_exits_play_mode(window) -> None:
    """播完（idle）也要退回入口按钮，否则控制条会卡住。"""
    win, app = window
    win._toggle_playback()
    win._on_playback_state("idle")
    app.processEvents()
    assert win._play_mode is False
    assert win._btn_play.isVisibleTo(win) is True
    win.close()


def test_playback_callback_ignored_outside_play_mode(window) -> None:
    win, app = window
    win._highlight_note(2)  # 没进试听模式
    assert win._record_view.extraSelections() == []
    assert win._cursor == 0
    win.close()


def test_editing_text_stops_playback(window) -> None:
    """文本框一改，原来的高亮下标就失效了 —— 必须停掉试听。"""
    win, app = window
    win._toggle_playback()
    assert win._play_mode is True
    win._record_view.setPlainText("100A")  # 会触发 textChanged
    app.processEvents()
    assert win._play_mode is False
    win.close()


# --------------------------------------------------------------------------- #
# 回归：点播放键不能把试听模式踢掉（M2q.9 实机反馈）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def real_window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """用**真实** PlaybackController（只是不出声）—— 上面的 fixture 把方法都换成了桩，
    测不出"换播放器漏 idle"这类信号问题。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win._playback._audio_enabled = False  # 不加载 mp3

    text = "100A2000F2600G"  # 三个音拉开距离，测试期间不会播完
    win._set_record_text(text)
    win._playback_score = bgi_sheet.score_from_tick_text(text)
    win._refresh_playback_enabled()
    try:
        yield win, app
    finally:
        win._stop_playback()
        win.close()
        app.processEvents()


def test_resume_after_seek_keeps_play_mode(real_window) -> None:
    """暂停 → 快进 → 继续：这一路会 `play_from()` 换播放器，绝不能让试听模式退出。

    旧实现里 `Player.stop()` 自带的 idle 会漏到 UI，`_exit_play_mode()` 把控制条收掉，
    紧接着的 `playing` 又被忽略 —— 用户看到的就是"点播放键退出试听模式"。
    """
    win, app = real_window
    win._toggle_playback()
    assert win._play_mode is True

    win._toggle_pause()
    assert win._paused is True
    win._step_cursor(1)  # 位置动过 → 继续时必须 play_from(1)
    win._toggle_pause()
    app.processEvents()

    assert win._play_mode is True, "继续播放不能把试听模式踢掉"
    assert win._paused is False
    assert win._play_bar.isVisibleTo(win) is True, "控制条不能消失"
    assert win._btn_play.isVisibleTo(win) is False, "入口按钮不该回来"


def test_plain_pause_resume_keeps_play_mode(real_window) -> None:
    win, app = real_window
    win._toggle_playback()
    win._toggle_pause()
    win._toggle_pause()  # 位置没动 → 走 resume()
    app.processEvents()
    assert win._play_mode is True
    assert win._paused is False


def test_play_pause_button_does_not_auto_repeat(window) -> None:
    """播放/暂停键**不能**自动重复：按住 >420ms 会连发 clicked，反复播放↔暂停。

    快退/快进才需要"按住连续走"。
    """
    win, _ = window
    assert win._btn_toggle_play.autoRepeat() is False
    assert win._btn_rewind.autoRepeat() is True
    assert win._btn_forward.autoRepeat() is True
    assert win._btn_rewind.autoRepeatInterval() <= 120, "按住时移动要跟得上手"
    win.close()


# --------------------------------------------------------------------------- #
# 回归：点播放键不能把试听模式踢掉（M2q.9 实机反馈）
# --------------------------------------------------------------------------- #


@pytest.fixture()
def real_window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """用**真实** PlaybackController（只是不出声）—— 上面的 fixture 把方法都换成了桩，
    测不出"换播放器漏 idle"这类信号问题。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win._playback._audio_enabled = False  # 不加载 mp3

    text = "100A2000F2600G"  # 三个音拉开距离，测试期间不会播完
    win._set_record_text(text)
    win._playback_score = bgi_sheet.score_from_tick_text(text)
    win._refresh_playback_enabled()
    try:
        yield win, app
    finally:
        win._stop_playback()
        win.close()
        app.processEvents()


def test_resume_after_seek_keeps_play_mode(real_window) -> None:
    """暂停 → 快进 → 继续：这一路会 `play_from()` 换播放器，绝不能让试听模式退出。

    旧实现里 `Player.stop()` 自带的 idle 会漏到 UI，`_exit_play_mode()` 把控制条收掉，
    紧接着的 `playing` 又被忽略 —— 用户看到的就是"点播放键退出试听模式"。
    """
    win, app = real_window
    win._toggle_playback()
    assert win._play_mode is True

    win._toggle_pause()
    assert win._paused is True
    win._step_cursor(1)  # 位置动过 → 继续时必须 play_from(1)
    win._toggle_pause()
    app.processEvents()

    assert win._play_mode is True, "继续播放不能把试听模式踢掉"
    assert win._paused is False
    assert win._play_bar.isVisibleTo(win) is True, "控制条不能消失"
    assert win._btn_play.isVisibleTo(win) is False, "入口按钮不该回来"


def test_plain_pause_resume_keeps_play_mode(real_window) -> None:
    win, app = real_window
    win._toggle_playback()
    win._toggle_pause()
    win._toggle_pause()  # 位置没动 → 走 resume()
    app.processEvents()
    assert win._play_mode is True
    assert win._paused is False


def test_play_pause_button_does_not_auto_repeat(window) -> None:
    """播放/暂停键**不能**自动重复：按住 >420ms 会连发 clicked，反复播放↔暂停。

    快退/快进才需要"按住连续走"。
    """
    win, _ = window
    assert win._btn_toggle_play.autoRepeat() is False
    assert win._btn_rewind.autoRepeat() is True
    assert win._btn_forward.autoRepeat() is True
    assert win._btn_rewind.autoRepeatInterval() <= 120, "按住时移动要跟得上手"
    win.close()
