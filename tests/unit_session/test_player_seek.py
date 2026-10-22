"""`Player` 从中途开始的语义（M2q.9 试听「从当前高亮继续」依赖它）。

`play(start_ms)` 的 `start_ms` 是"绝对时刻表的起点"：
- 早于 `start_ms` 的音必须**跳过**，否则它们的 `target` 落在过去，
  会在第一帧被连续播出（听起来就是一串爆音）；
- 后于 `start_ms` 的音按相对时间正常播。
"""

from __future__ import annotations

import time

from yqrec.player.engine import Player, PlayerState
from yqrec.types import NoteEvent, Score


class _FakeAudio:
    def __init__(self) -> None:
        self.played: list[str] = []

    def play(self, note: str) -> None:
        self.played.append(note)

    def stop_all(self) -> None:
        pass


def _score() -> Score:
    # 三个音：0ms / 100ms / 200ms
    return Score(
        events=[
            NoteEvent(keys=("A",), onset_ms=0.0),
            NoteEvent(keys=("F",), onset_ms=100.0),
            NoteEvent(keys=("G",), onset_ms=200.0),
        ]
    )


def test_play_from_middle_skips_past_notes() -> None:
    audio = _FakeAudio()
    player = Player(_score(), audio=audio, note_of_key=lambda key: key.lower())
    seen: list[float] = []
    player.on_note = lambda event, _index: seen.append(event.onset_ms)
    try:
        player.play(150.0)  # 从第二个和第三个音之间开始
        time.sleep(0.25)  # 只够播 200ms 的那个
    finally:
        player.stop()

    assert seen == [200.0], f"早于起点的音必须跳过，实际 {seen}"
    assert audio.played == ["g"], "对应的按键音也只能响一次"
    assert player.state is PlayerState.IDLE


def test_play_from_zero_plays_everything() -> None:
    """从头播时不受影响（回归保护）。"""
    audio = _FakeAudio()
    player = Player(_score(), audio=audio)
    seen: list[float] = []
    player.on_note = lambda event, _index: seen.append(event.onset_ms)
    try:
        player.play(0.0)
        time.sleep(0.32)
    finally:
        player.stop()
    assert seen == [0.0, 100.0, 200.0]
    assert audio.played == ["A", "F", "G"]


def test_pause_freezes_then_resume_continues() -> None:
    audio = _FakeAudio()
    player = Player(_score(), audio=audio)
    seen: list[float] = []
    player.on_note = lambda event, _index: seen.append(event.onset_ms)
    try:
        player.play(0.0)
        time.sleep(0.03)  # 第一个音已发
        player.pause()
        assert player.state is PlayerState.PAUSED
        time.sleep(0.15)  # 暂停期间不该再发
        assert seen == [0.0]

        player.play()  # PAUSED → 继续（时间轴冻结过）
        time.sleep(0.30)
    finally:
        player.stop()
    assert seen == [0.0, 100.0, 200.0], f"暂停后应继续播剩下的，实际 {seen}"
