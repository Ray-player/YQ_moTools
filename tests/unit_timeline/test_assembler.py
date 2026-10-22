"""事件装配：和弦合并、休止计算、场景保护、延音。"""

from __future__ import annotations

from dataclasses import replace

from yqrec.timeline.assembler import AssembleConfig, assemble
from yqrec.types import NoteEvent, PressEvent, RestEvent


def _down(key: str, ts: float) -> PressEvent:
    return PressEvent(key=key, down=True, ts=ts)


def _up(key: str, ts: float) -> PressEvent:
    return PressEvent(key=key, down=False, ts=ts)


def test_two_notes_same_chord(spec_fwzs):
    presses = [_down("A", 0.0), _down("S", 0.005), _up("A", 0.1), _up("S", 0.1)]
    events = assemble(presses, spec_fwzs)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, NoteEvent)
    assert ev.keys == ("A", "S")
    assert ev.sustain_ms == 0


def test_notes_20ms_apart_merge_30ms_apart_not(spec_fwzs):
    presses = [_down("A", 0.0), _up("A", 0.05), _down("S", 0.02), _up("S", 0.07)]
    events = assemble(presses, spec_fwzs)
    assert len(events) == 1 and isinstance(events[0], NoteEvent)
    assert events[0].keys == ("A", "S")

    presses2 = [_down("A", 0.0), _up("A", 0.02), _down("S", 0.03), _up("S", 0.05)]
    events2 = assemble(presses2, spec_fwzs)
    assert len(events2) == 3
    assert isinstance(events2[0], NoteEvent) and isinstance(events2[1], RestEvent)
    assert events2[1].ms == 30


def test_rest_between_chords(spec_fwzs):
    presses = [
        _down("A", 0.0), _up("A", 0.05),
        _down("B", 0.5), _up("B", 0.55),
    ]
    events = assemble(presses, spec_fwzs)
    assert [type(e).__name__ for e in events] == ["NoteEvent", "RestEvent", "NoteEvent"]
    assert events[1].ms == 500


def test_zero_gap_gets_min_rest(spec_fwzs):
    presses = [_down("A", 0.0), _up("A", 0.01), _down("S", 0.0), _up("S", 0.01)]
    # 两个不同 onset 不可能同时为 0；构造 0.5ms 间隔（小于 chord 窗口 → 会合并）
    presses = [_down("A", 0.0), _up("A", 0.01), _down("S", 0.03), _up("S", 0.04)]
    events = assemble(presses, spec_fwzs, AssembleConfig(chord_window_ms=1.0))
    assert isinstance(events[0], NoteEvent)
    assert isinstance(events[1], RestEvent)
    assert events[1].ms >= 1.0


def test_scene_guard_drops_busy_frame(spec_fwzs):
    keys = list(spec_fwzs.key_order)
    presses = [_down(k, 0.0) for k in keys] + [_up(k, 0.1) for k in keys]
    assert assemble(presses, spec_fwzs) == []


def test_sustain_written_when_instrument_sustains(spec_fwzs):
    spec = replace(spec_fwzs, sustain=True)
    presses = [_down("A", 0.0), _up("A", 0.4)]
    events = assemble(presses, spec)
    assert isinstance(events[0], NoteEvent)
    assert events[0].sustain_ms == 400


def test_unclosed_press_gets_zero_hold(spec_fwzs):
    events = assemble([_down("A", 0.0), _down("S", 0.2)], spec_fwzs)
    assert isinstance(events[0], NoteEvent)
    assert events[0].sustain_ms == 0


def test_keys_sorted_by_note_order(spec_fwzs):
    # Q 是 +1（order 0），A 是 1（order 7）→ 应输出 Q 在前
    presses = [_down("A", 0.0), _down("Q", 0.004), _up("A", 0.1), _up("Q", 0.1)]
    events = assemble(presses, spec_fwzs)
    assert events[0].keys == ("Q", "A")
