"""试听刚录的谱面时，键位灯要按录制实况亮（M2q.11 第 4 条）。

用户要求：**只有本次录制的谱面**能在试听时看到"何时按下 / 何时弹起 / 按下时的 Δ"，
读取进来的文件不支持；数据从内存取（`Recording.spans`），不重新解析文件。
"""

from __future__ import annotations

import pytest

from yqrec.recording import RecordingBuffer
from yqrec.types import PressEvent


@pytest.fixture()
def recording(spec_fwzs):
    """两个和弦：Q(0~90ms)、W(300~360ms)，Δ 分别是 40 / 24。"""
    key0, key1 = spec_fwzs.key_order[0], spec_fwzs.key_order[1]
    buf = RecordingBuffer(spec_fwzs)
    for i in range(30):
        buf.push_frame(i * 0.03)
    buf.push_events(
        [
            PressEvent(key=key0, down=True, ts=0.0, delta=40.0),
            PressEvent(key=key0, down=False, ts=0.09),
            PressEvent(key=key1, down=True, ts=0.30, delta=24.0),
            PressEvent(key=key1, down=False, ts=0.36),
        ]
    )
    return buf.finish()


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path, recording):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win._playback._audio_enabled = False
    # 假装"刚录完"：谱面文本 + 内存里的 Recording + 同一个 Score 对象
    win._set_record_text(recording.tick_text)
    win._record_result = recording
    win._playback_score = recording.score
    win._refresh_playback_enabled()

    calls: list[tuple] = []
    win._playback.play_from = lambda i=0: (calls.append(("play_from", i)), True)[1]
    win._playback.pause = lambda: calls.append(("pause",))
    win._playback.stop = lambda **k: calls.append(("stop",))
    try:
        yield win, app
    finally:
        win._progress_timer.stop()
        win.close()


def _scrub(win, ratio: float, *, release: bool = True) -> None:
    """按下 → 拖到 ratio（→ 松手）。松手会 seek+续播，位置会被拽回和弦起点，
    所以"看某一刻的键位"只调 `_scrub(..., release=False)`。"""
    win._on_scrub_started()
    win._on_scrub_move(ratio)
    if release:
        win._on_scrub_released(ratio)


def test_spans_only_for_current_recording(window, recording) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()
    assert win._play_spans == recording.spans, "刚录的谱面要带上逐键区间"

    win._stop_playback()
    # 换成"读取的文件"：`_record_result` 被清空 → 没有回放数据
    win._record_result = None
    win._toggle_playback()
    app.processEvents()
    assert win._play_spans == (), "读取的谱面不支持键位回放"
    win._stop_playback()


def test_keys_light_up_with_recorded_delta(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()
    cards = win._keys_panel._cards

    _scrub(win, 0.05, release=False)  # 15ms：落在第一个键的按住区间（0~90ms）里
    assert win._shown_pressed == {win._spec.key_order[0]: pytest.approx(40.0)}
    assert cards[0]._pressed is True, "卡片必须真的点亮"
    assert cards[0]._delta.text() == "Δ40", "第三行要显示录制时记下的 Δ"
    assert cards[1]._pressed is False

    _scrub(win, 0.95, release=False)  # 285ms：第一个已弹起、第二个还没按下
    assert win._shown_pressed == {}

    _scrub(win, 1.0, release=False)  # 300ms：第二个键按下
    assert win._shown_pressed == {win._spec.key_order[1]: pytest.approx(24.0)}
    assert cards[1]._pressed is True
    assert cards[0]._pressed is False, "第一个键这时必须已经弹起"


def test_release_clears_keys(window) -> None:
    """按下窗口一过就要熄灭 —— '何时弹起'就是靠这个表达。"""
    win, app = window
    win._toggle_playback()
    app.processEvents()
    _scrub(win, 0.03, release=False)  # 9ms
    assert win._shown_pressed
    _scrub(win, 0.3, release=False)  # 90ms 之后
    assert win._shown_pressed == {}, "松手后键位必须熄灭"


def test_stop_playback_dims_keys(window) -> None:
    win, app = window
    win._toggle_playback()
    app.processEvents()
    _scrub(win, 0.05)
    win._stop_playback()
    assert win._play_spans == ()
    assert win._shown_pressed == {}
    assert all(card._pressed is False for card in win._keys_panel._cards)
    assert win._keys_panel._cards[0]._delta.text() == "Δ—", "Δ 复位，别留着冒充实时值"


def test_editing_text_drops_spans(window) -> None:
    """改了文本框，键位下标就和谱面对不上了 —— 必须断开回放。"""
    win, app = window
    win._toggle_playback()
    app.processEvents()
    win._record_view.setPlainText("100A")
    app.processEvents()
    assert win._play_mode is False
    assert win._play_spans == ()
