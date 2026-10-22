"""事件装配（03 §3.5）：按下/抬起 → 音符 → 和弦 + 休止。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..types import InstrumentSpec, NoteEvent, PressEvent, RestEvent, ScoreEvent


@dataclass(frozen=True, slots=True)
class AssembleConfig:
    chord_window_ms: float = 25.0
    """同一和弦内允许的最大 onset 差。"""

    min_rest_ms: float = 1.0
    """两个不同 onset 之间至少写入的休止（防止 tick token 粘连）。"""

    scene_guard: bool = True
    """开启场景切换保护：一拍内 >= key_count-5 个键同时按下 → 丢弃。"""


@dataclass(frozen=True, slots=True)
class _Note:
    key: str
    onset_ms: float
    hold_ms: float


def assemble(
    presses: Sequence[PressEvent],
    spec: InstrumentSpec,
    cfg: AssembleConfig | None = None,
) -> list[ScoreEvent]:
    """把事件流装配成 NoteEvent/RestEvent 序列。

    - sustain：乐器 `sustain=True` 时取和弦内最大 hold；否则恒 0（一期）；
    - rest_i = onset_{i+1} - onset_i；最后一个和弦后不加休止；
    - 若两 onset 差 < `min_rest_ms`，按 `min_rest_ms` 写出（避免粘连）。
    """
    cfg = cfg or AssembleConfig()
    notes = _pair_notes(presses)
    if not notes:
        return []

    notes.sort(key=lambda n: n.onset_ms)
    chords = _group_chords(notes, cfg.chord_window_ms)

    if cfg.scene_guard:
        chords = [
            ch
            for ch in chords
            if len({n.key for n in ch}) < max(1, spec.key_count - 5)
        ]
    if not chords:
        return []

    order = {k: i for i, k in enumerate(spec.key_order)}
    events: list[ScoreEvent] = []
    for idx, chord in enumerate(chords):
        keys = tuple(sorted({n.key for n in chord}, key=lambda k: order.get(k, 10_000)))
        onset = min(n.onset_ms for n in chord)
        sustain = max((n.hold_ms for n in chord), default=0.0) if spec.sustain else 0.0
        events.append(NoteEvent(keys=keys, onset_ms=onset, sustain_ms=sustain))
        if idx + 1 < len(chords):
            next_onset = min(n.onset_ms for n in chords[idx + 1])
            gap = next_onset - onset
            events.append(RestEvent(ms=max(gap, cfg.min_rest_ms)))
    return events


def _pair_notes(presses: Sequence[PressEvent]) -> list[_Note]:
    """按下/抬起配对；未闭合的按下按最后事件时间闭合（hold=0）。"""
    if not presses:
        return []
    sorted_events = sorted(presses, key=lambda e: e.ts)
    pending: dict[str, list[float]] = {}
    notes: list[_Note] = []
    last_ts = sorted_events[-1].ts
    for ev in sorted_events:
        if ev.down:
            pending.setdefault(ev.key, []).append(ev.ts)
        else:
            stack = pending.get(ev.key)
            if not stack:
                continue
            down_ts = stack.pop(0)
            notes.append(
                _Note(key=ev.key, onset_ms=down_ts * 1000.0, hold_ms=max(0.0, (ev.ts - down_ts) * 1000.0))
            )
    for key, stack in pending.items():
        for down_ts in stack:
            notes.append(_Note(key=key, onset_ms=down_ts * 1000.0, hold_ms=max(0.0, (last_ts - down_ts) * 1000.0)))
    return notes


def _group_chords(notes: list[_Note], window_ms: float) -> list[list[_Note]]:
    chords: list[list[_Note]] = []
    current: list[_Note] = []
    anchor = 0.0
    for n in notes:
        if not current:
            current, anchor = [n], n.onset_ms
        elif n.onset_ms - anchor <= window_ms:
            current.append(n)
        else:
            chords.append(current)
            current, anchor = [n], n.onset_ms
    if current:
        chords.append(current)
    return chords
