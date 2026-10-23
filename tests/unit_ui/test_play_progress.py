"""试听进度条（M2q.10）：实时进度 + 鼠标拖动跳转。

交互约定（用户定）：
1. 进度条在试听胶囊的**图标左侧**，播放时随播放头实时前进（40ms 刷一次）；
2. 按住左键 → 暂停（不然定时器会把把手拽回去，手感是"拖不动/回弹"）；
3. 拖动只移动高亮/进度，**不发声**；松手才跳到那个和弦；
4. 本来在播就接着播（`play_from(新位置)`），本来是暂停就留在暂停态。
"""

from __future__ import annotations

import time

import pytest

# --------------------------------------------------------------------------- #
# 控件本身（不需要主窗）
# --------------------------------------------------------------------------- #


def _qapp():
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_ratio_mapping_hits_both_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """0% / 100% 都必须真能点到（轨道两端要留出把手半径）。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _qapp()
    from yqrec.ui.widgets import PROGRESS_HANDLE_R, PlaybackProgress

    bar = PlaybackProgress()
    bar.resize(200, 18)
    assert bar._ratio_at(0) == 0.0, "左边界外 = 0%"
    assert bar._ratio_at(PROGRESS_HANDLE_R) == 0.0
    assert bar._ratio_at(10_000) == 1.0, "右边界外 = 100%"
    assert bar._ratio_at(200 - PROGRESS_HANDLE_R) == 1.0
    assert abs(bar._ratio_at(100) - 0.5) < 0.02, "中点 ≈ 50%"
    bar.deleteLater()


def test_set_progress_clamps_and_keeps_tooltip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _qapp()
    from yqrec.ui.widgets import PlaybackProgress

    bar = PlaybackProgress()
    bar.set_progress(1500, 6000)
    assert abs(bar.fraction() - 0.25) < 1e-6
    assert "0:01 / 0:06" in bar.toolTip()
    bar.set_progress(99_000, 6000)  # 超过总长也不能 >1
    assert bar.fraction() == 1.0
    bar.set_progress(10, 0)  # 总长 0（空谱面）不能除零
    assert bar.fraction() == 0.0
    bar.reset()
    assert bar.fraction() == 0.0
    bar.deleteLater()


# --------------------------------------------------------------------------- #
# 控制器：播放头 / 总长 / 位置 → 和弦
# --------------------------------------------------------------------------- #


def _controller():
    _qapp()
    from yqrec.formats import bgi_sheet
    from yqrec.instruments import load_instrument
    from yqrec.ui.playback import PlaybackController

    score = bgi_sheet.score_from_tick_text("0A1000F1000G1000H3000J")
    ctl = PlaybackController(audio_enabled=False)
    ctl.load(score, load_instrument("fwzs"))
    return ctl


def test_ordinal_at_maps_ms_to_chord() -> None:
    ctl = _controller()
    assert ctl.duration_ms == 6000.0, "末音 onset 就是总长"
    cases = {0: 0, 999: 0, 1000: 1, 2500: 2, 3000: 3, 5999: 3, 6000: 4, -50: 0, 99_000: 4}
    for ms, expected in cases.items():
        assert ctl.ordinal_at(ms) == expected, f"{ms}ms 应落在第 {expected} 个和弦"


def test_position_advances_and_freezes_on_pause() -> None:
    ctl = _controller()
    try:
        assert ctl.play_from(1) is True
        time.sleep(0.15)
        moving = ctl.position_ms
        assert 1000 <= moving < 3000, f"应从第 2 个和弦（1000ms）开始往前走，实际 {moving}"

        ctl.pause()
        frozen = ctl.position_ms
        assert frozen == ctl.position_ms, "暂停后播放头必须冻结"
        time.sleep(0.05)
        assert ctl.position_ms == frozen

        assert ctl.resume() is True
        time.sleep(0.08)
        assert ctl.position_ms > frozen, "继续播放要从冻结处接着走"
    finally:
        ctl.stop()
    assert ctl.position_ms == 0.0, "停止后回起播点"


# --------------------------------------------------------------------------- #
# 主窗：拖动 → 暂停 → 跳转
# --------------------------------------------------------------------------- #


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app = _qapp()
    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    win = MainWindow()
    text = "0A1000F1000G1000H3000J"  # 和弦在 0 / 1000 / 2000 / 3000 / 6000 ms
    win._set_record_text(text)
    win._playback_score = bgi_sheet.score_from_tick_text(text)
    win._refresh_playback_enabled()

    calls: list[tuple] = []
    win._playback.play_from = lambda i=0: (calls.append(("play_from", i)), True)[1]
    win._playback.pause = lambda: calls.append(("pause",))
    win._playback.resume = lambda: (calls.append(("resume",)), True)[1]
    win._playback.stop = lambda **k: calls.append(("stop",))
    win.calls = calls  # type: ignore[attr-defined]
    try:
        yield win, app
    finally:
        win._progress_timer.stop()
        win.close()


def _drag(win, app, start_ratio: float, end_ratio: float | None = None) -> None:
    """按下 → 拖动 → 松手（直接调事件处理，offscreen 下拿不到真鼠标）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    bar = win._play_progress
    width = float(bar.width())

    def pos(ratio: float) -> QPointF:
        return QPointF(ratio * width, bar.height() / 2.0)

    def event(kind, ratio: float):  # noqa: ANN001, ANN202
        return QMouseEvent(kind, pos(ratio), QPointF(0, 0), Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    bar.mousePressEvent(event(QMouseEvent.Type.MouseButtonPress, start_ratio))
    app.processEvents()
    if end_ratio is not None:
        move = QMouseEvent(QMouseEvent.Type.MouseMove, pos(end_ratio), QPointF(0, 0),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        bar.mouseMoveEvent(move)
        app.processEvents()
    final = end_ratio if end_ratio is not None else start_ratio
    release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, pos(final), QPointF(0, 0),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    bar.mouseReleaseEvent(release)
    app.processEvents()


def test_drag_pauses_then_seeks_and_keeps_playing(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()
    assert win._play_mode is True
    before = len(win.calls)

    _drag(win, app, 0.5, 0.9)  # 50% = 3000ms → 90% 仍是 3000ms 之后

    assert ("pause",) in [c for c in win.calls[before:]], "按住进度条应先暂停"
    assert win._cursor == 3, f"90% 应落在第 4 个和弦（3000ms），实际 {win._cursor}"
    assert win.calls[-1] == ("play_from", 3), "松手后从新位置重开"
    assert win._paused is False, "本来在播 → 松手后继续播"
    assert win._play_mode is True, "拖进度条不能把试听模式踢掉"
    assert win._progress_timer.isActive() is True, "松手后恢复定时刷新"


def test_drag_to_start_and_end(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()

    _drag(win, app, 0.0)
    assert win._cursor == 0
    assert win.calls[-1] == ("play_from", 0)

    _drag(win, app, 1.0)
    assert win._cursor == 4, "拖到底 = 最后一个和弦"
    assert win.calls[-1] == ("play_from", 4)


def test_drag_while_paused_stays_paused(window) -> None:
    """暂停中拖进度条：只挪位置，不许自己播起来（用户要点播放确认）。"""
    win, app = window
    win._toggle_playback()
    win._toggle_pause()
    app.processEvents()
    before = len(win.calls)

    _drag(win, app, 0.3)  # 30% = 1800ms → 第 2 个和弦（1000ms）

    assert win._paused is True, "本来暂停 → 松手仍在暂停"
    assert win._cursor == 1
    assert len(win.calls) == before, f"暂停态拖动不该调用播放接口，实际 {win.calls[before:]}"
    # 位置动过 → 继续播放时必须 play_from，而不是 resume 到旧位置
    win._toggle_pause()
    assert win.calls[-1] == ("play_from", 1)


def test_exit_play_mode_resets_progress(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()
    assert win._play_progress.time_of(1.0) == 6000.0, "进试听模式时进度条要知道总长"

    _drag(win, app, 0.5)
    win._play_progress.set_progress(3000, 6000)  # 桩播放器不动播放头，手动给个中间值
    assert win._play_progress.fraction() == 0.5

    win._stop_playback()
    app.processEvents()
    assert win._play_mode is False
    assert win._play_progress.fraction() == 0.0, "退出后进度条清空"
    assert win._play_progress.time_of(1.0) == 0.0, "总长也清掉"
    assert win._progress_timer.isActive() is False, "退出后不再刷新"
    assert win._play_bar.isVisibleTo(win) is False
