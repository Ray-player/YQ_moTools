"""tick 解析/序列化：语法、语义、23 首语料往返。"""

from __future__ import annotations

from pathlib import Path

import pytest

from yqrec.formats import tick
from yqrec.types import NoteEvent, RestEvent

KNOWN = Path(__file__).resolve().parents[1] / "data" / "known"


def _expand(score) -> list[tuple]:
    """扁平化表示：('N', onset, key, sustain) / ('R', ms)。"""
    out: list[tuple] = []
    for ev in score.events:
        if isinstance(ev, NoteEvent):
            for key in ev.keys:
                out.append(("N", round(ev.onset_ms, 3), key, round(ev.sustain_ms, 3)))
        else:
            out.append(("R", round(ev.ms, 3)))
    return out


def test_parse_rest_and_note():
    score = tick.parse("A 100 B 200")
    assert [type(e).__name__ for e in score.events] == [
        "NoteEvent", "RestEvent", "NoteEvent", "RestEvent",
    ]
    assert score.events[0].keys == ("A",)
    assert score.events[1].ms == 100
    assert score.events[2].onset_ms == 100
    assert score.events[3].ms == 200


def test_parse_concat_is_chord():
    score = tick.parse("AS")
    assert len(score.events) == 1
    assert isinstance(score.events[0], NoteEvent)
    assert score.events[0].keys == ("A", "S")


def test_parse_sustain_variant():
    score = tick.parse("A.500 S.500 300")
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    rests = [e for e in score.events if isinstance(e, RestEvent)]
    assert len(notes) == 2
    assert all(n.sustain_ms == 500 for n in notes)
    assert [r.ms for r in rests] == [300]


def test_parse_sustain_takes_max():
    score = tick.parse("A.100 B.300")
    assert isinstance(score.events[0], NoteEvent)
    # 同一 token 内取最大值；不同 token 各自独立
    assert len(score.events) == 2
    assert score.events[0].sustain_ms == 100
    assert score.events[1].sustain_ms == 300


def test_metadata_only_before_events():
    text = "/*\n @name 测试曲\n @instrument 风物之诗\n*/\nA 100\n/* @name 后续 */\n"
    score = tick.parse(text)
    assert score.metadata["name"] == "测试曲"
    assert score.metadata["instrument"] == "风物之诗"
    assert score.metadata["name"] != "后续"


def test_unknown_chars_warn():
    score = tick.parse("A % 100")
    assert any(d.level == "warning" for d in score.diagnostics)
    assert any(isinstance(e, RestEvent) for e in score.events)


def test_format_canonical():
    score = tick.parse("A 100")
    text = tick.format(score)
    assert "A" in text
    # 无延音和弦连写
    s2 = tick.parse("AS")
    assert tick.format(s2).strip().endswith("AS")


def test_format_sustain_space_separated():
    from yqrec.types import NoteEvent, Score

    score = Score(events=[NoteEvent(keys=("A", "S"), onset_ms=0, sustain_ms=500)])
    assert "A.500 S.500" in tick.format(score)


@pytest.mark.parametrize("path", sorted(KNOWN.glob("*.txt")), ids=lambda p: p.name)
def test_corpus_roundtrip(path: Path):
    text = path.read_text(encoding="utf-8")
    score = tick.parse(text)
    assert not [d for d in score.diagnostics if d.is_error]
    assert score.note_count > 0

    again = tick.parse(tick.format(score))
    assert _expand(again) == _expand(score)
    assert again.metadata == score.metadata
