"""`PlaybackController` 的状态信号不能被"换播放器"污染（M2q.9 修复）。

`play_from()` 要停掉旧 `Player` 再建新的，而 `Player.stop()` 内部会 `_set_state(IDLE)`
→ 走 `on_state` → 转发成 `idle`。UI 收到 `idle` 会以为"播完了"而收起控制条，
紧接着的 `playing` 又因为已经退出试听模式被忽略 ——
用户看到的就是「点一下播放键，试听模式退出又切回来」。
"""

from __future__ import annotations

import pytest

from yqrec.formats import bgi_sheet
from yqrec.instruments import load_instrument


def _controller():
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.playback import PlaybackController

    QApplication.instance() or QApplication([])
    spec = load_instrument("fwzs")
    # 三个音：从第 2 个开始播时，后面还有第 3 个 → 不会立刻播完（避免"自然 idle"混进来）
    score = bgi_sheet.score_from_tick_text("100A2000F2600G")
    ctl = PlaybackController(audio_enabled=False)  # 不加载 mp3
    states: list[str] = []
    ctl.state_changed.connect(states.append)
    ctl.load(score, spec)
    return ctl, states


def test_play_from_does_not_leak_idle() -> None:
    ctl, states = _controller()
    try:
        assert ctl.play_from(0) is True
        assert states == ["playing"]

        assert ctl.play_from(1) is True  # 「从当前高亮继续」走的就是这条
        assert states == ["playing", "playing"], f"换播放器不能漏 idle，实际 {states}"
    finally:
        ctl.stop()
    assert states[-1] == "idle", "真正停止时仍要发 idle"


def test_pause_resume_emit_states() -> None:
    ctl, states = _controller()
    try:
        ctl.play_from(0)
        ctl.pause()
        assert states[-1] == "paused"
        assert ctl.resume() is True
        assert states[-1] == "playing"
    finally:
        ctl.stop()
    assert states[-1] == "idle"
    assert states.count("idle") == 1, "只有真正停止时才发一次 idle"


def test_stop_emits_idle_without_duplicate() -> None:
    """`Player.stop()` 自己会发一次 idle，控制器不能再补一次（否则 UI 会收到两条）。"""
    ctl, states = _controller()
    ctl.play_from(0)
    ctl.stop()
    assert states == ["playing", "idle"], f"实际 {states}"
