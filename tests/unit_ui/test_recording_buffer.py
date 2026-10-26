"""M2 录制缓冲 + 谱面 JSON（只保纯逻辑，不碰 UI）。

M2f 起 tick 文本记**毫秒间隔**（`A1000F` = 隔 1 秒），不再记帧间隔。
这里的 `_buffer` 仍按帧喂时间戳（模拟采集），但断言全是毫秒。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from yqrec.recording import (
    RecordedNote,
    RecordingBuffer,
    parse_tick_text,
    tick_spans,
    tick_text,
    tick_text_from_score,
)
from yqrec.scorefile import (
    DEFAULT_SCORE_FORMAT,
    RECORD_FORMAT,
    TICK_UNIT_MS,
    build_record,
    build_record_from_text,
    load_record,
    record_unit,
    save_record,
    save_record_text,
    score_from_record,
    suggested_filename,
)
from yqrec.types import InstrumentSpec, NoteEvent, PressEvent, Score

FRAME_MS = 16.0


def _buffer(spec: InstrumentSpec, frames: int) -> RecordingBuffer:
    buf = RecordingBuffer(spec)
    for index in range(frames):
        buf.push_frame(index * FRAME_MS / 1000.0)
    return buf


def _press(buf: RecordingBuffer, frame: int, *keys: str) -> None:
    ts = frame * FRAME_MS / 1000.0
    for key in keys:
        buf.push_events([PressEvent(key=key, down=True, ts=ts)])


def test_tick_text_uses_millisecond_gaps(spec_fwzs: InstrumentSpec) -> None:
    """`A48F` 语义：和弦字母连写，和弦之间写**毫秒**间隔。"""
    buf = _buffer(spec_fwzs, 13)
    _press(buf, 0, "A")
    _press(buf, 3, "F")  # 3 帧 = 48ms
    _press(buf, 12, "G")  # 再 9 帧 = 144ms

    recording = buf.finish()

    assert recording.note_count == 3
    assert [n.gap_ms for n in recording.notes] == [0, 48, 144]
    assert recording.tick_text == "A48F144G"


def test_chord_keeps_key_order_and_gap_zero(spec_fwzs: InstrumentSpec) -> None:
    buf = _buffer(spec_fwzs, 9)
    _press(buf, 0, "F", "A")  # 同帧 = 和弦，按键位顺序输出 A 在前
    _press(buf, 0, "G")  # 仍在和弦窗口内 → 并入同一和弦
    _press(buf, 8, "Q")

    recording = buf.finish()

    assert recording.notes[0].keys == ("A", "F", "G")
    assert recording.notes[0].gap_ms == 0
    assert recording.tick_text == "AFG128Q"


def test_finish_is_idempotent_and_closes(spec_fwzs: InstrumentSpec) -> None:
    buf = _buffer(spec_fwzs, 5)
    _press(buf, 0, "A")
    first = buf.finish()
    buf.close()
    buf.push_frame(9.0)
    buf.push_events([PressEvent(key="F", down=True, ts=9.0)])
    second = buf.finish()

    assert first.note_count == second.note_count == 1
    assert second.tick_text == "A"


def test_empty_recording_has_no_notes(spec_fwzs: InstrumentSpec) -> None:
    recording = _buffer(spec_fwzs, 5).finish()

    assert recording.note_count == 0
    assert recording.tick_text == ""
    assert recording.frames == 5


def test_tick_text_helper() -> None:
    notes = (
        RecordedNote(keys=("A",), onset_ms=0.0, onset_frame=0, gap_ms=0),
        RecordedNote(keys=("F",), onset_ms=960.0, onset_frame=60, gap_ms=960),
    )
    assert tick_text(notes) == "A960F"


def test_record_json_has_no_events_and_restores_from_text(
    spec_fwzs: InstrumentSpec, tmp_path
) -> None:
    """谱面只存 `tick.text`：不写 `events[]`，靠文本就能还原出可播放的 Score。"""
    buf = _buffer(spec_fwzs, 13)
    _press(buf, 0, "A")
    _press(buf, 3, "F")
    recording = buf.finish()

    record = build_record(recording, spec_fwzs, name="测试", backend="gdi")
    assert record["format"] == RECORD_FORMAT
    assert record["version"] == 5
    assert record["type"] == "tick", "顶层要标曲谱类型（v5）"
    assert list(record)[:4] == ["version", "type", "Average_Delta", "Min_Delta"], (
        "type 紧跟 version，Δ 统计紧跟 type"
    )
    assert record["tick"]["sustain"] is False, "不延音的乐器不标记延音"
    assert "events" not in record, "谱面数据只留 tick.text，不再冗余展开"
    assert record["tick"] == {"unit": TICK_UNIT_MS, "text": "A48F", "sustain": False}
    assert record["instrument"]["id"] == spec_fwzs.id
    assert record["capture"]["backend"] == "gdi"

    # 毫秒制：数字直接用，不需要 fps
    assert record_unit(record) == "ms"
    notes = list(parse_tick_text(record["tick"]["text"]))
    assert [n.keys for n in notes] == [("A",), ("F",)]
    assert [n.gap for n in notes] == [0, 48]

    score = score_from_record(record)
    assert score.note_count == 2
    onsets = [e.onset_ms for e in score.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(0.0), pytest.approx(48.0)]
    assert score.metadata["instrument"] == spec_fwzs.name

    saved = save_record(tmp_path / "谱面", recording, spec_fwzs, backend="gdi")
    assert saved.suffix == ".json"
    loaded = load_record(saved)
    assert loaded["name"] == "谱面"
    assert loaded["tick"]["text"] == "A48F"
    assert "\\u" not in saved.read_text(encoding="utf-8"), "中文应原样写盘，不转义"


def test_parse_tick_text_is_symmetric(spec_fwzs: InstrumentSpec) -> None:
    """写入端 `tick_text` 与解析端 `parse_tick_text` 必须互为逆运算。"""
    buf = _buffer(spec_fwzs, 20)
    _press(buf, 0, "A", "F")  # 和弦
    _press(buf, 5, "G")
    _press(buf, 18, "Q")
    recording = buf.finish()

    notes = parse_tick_text(recording.tick_text)

    assert [n.keys for n in notes] == [("A", "F"), ("G",), ("Q",)]
    assert [n.gap for n in notes] == [0, 80, 208]
    assert tick_text([RecordedNote(n.keys, 0.0, 0, n.gap) for n in notes]) == (
        recording.tick_text
    )


def test_tick_spans_align_with_parsed_tokens(spec_fwzs: InstrumentSpec) -> None:
    """高亮区间必须与和弦一一对应，且切片正好是字母（不含间隔数字）。"""
    text = "A35DF7G"
    spans = tick_spans(text)
    assert [text[s:e] for s, e in spans] == ["A", "DF", "G"]
    notes = parse_tick_text(text)
    assert [n.keys for n in notes] == [("A",), ("D", "F"), ("G",)]
    assert [n.gap for n in notes] == [0, 35, 7]
    assert len(spans) == len(notes)

    # 录制产出的文本同样可用
    buf = _buffer(spec_fwzs, 13)
    _press(buf, 0, "A", "F")
    _press(buf, 5, "G")
    recording = buf.finish()
    spans = tick_spans(recording.tick_text)
    assert len(spans) == recording.note_count
    assert [recording.tick_text[s:e] for s, e in spans] == ["AF", "G"]


def test_parse_tick_text_tolerates_noise() -> None:
    assert [n.gap for n in parse_tick_text("A 3 F\n9G")] == [0, 3, 9]
    assert [n.keys for n in parse_tick_text("aa2B")] == [("A",), ("B",)], "重复键去重"
    assert parse_tick_text("") == []


def test_suggested_filename_carries_score_format() -> None:
    """默认文件名带谱面格式段：`谱面_tick_20261020_172400.json`。"""
    moment = datetime(2026, 10, 20, 17, 24, 0)
    assert suggested_filename(now=moment) == "谱面_tick_20261020_172400.json"
    assert suggested_filename("bgi_midi", now=moment) == "谱面_bgi_midi_20261020_172400.json"
    assert DEFAULT_SCORE_FORMAT == "tick"


def test_record_marks_score_format(spec_fwzs: InstrumentSpec) -> None:
    buf = _buffer(spec_fwzs, 3)
    _press(buf, 0, "A")
    record = build_record(buf.finish(), spec_fwzs, score_format="bgi")
    assert record["score_format"] == "bgi"
    # 未指定时默认 tick，且不影响谱面文本
    assert build_record(buf.finish(), spec_fwzs)["score_format"] == "tick"


def test_score_from_record_of_legacy_v1_file() -> None:
    """v2 及更早的文件是**帧**单位（`tick.unit == "frame"`），仍按采集帧率换算。"""
    legacy = {
        "version": 1,
        "name": "旧文件",
        "instrument": {"id": "fwzs", "name": "风物之诗", "key_count": 21},
        "capture": {"fps": 60.0, "frames": 61, "duration_ms": 1000.0},
        "tick": {"unit": "frame", "text": "A60F"},
        "events": [{"index": 0, "keys": ["A"], "gap_frames": 0}],
    }
    score = score_from_record(legacy)
    assert score.note_count == 2
    onsets = [e.onset_ms for e in score.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(0.0), pytest.approx(1000.0)]


def test_legacy_frame_file_without_unit_is_still_readable() -> None:
    """非常老的文件连 `tick.unit` 都没有 → 按帧处理，不能当成毫秒（否则慢 60 倍）。"""
    legacy = {
        "version": 2,
        "capture": {"fps": 50.0, "frames": 0, "duration_ms": 0.0},
        "tick": {"text": "A10F"},
    }
    assert record_unit(legacy) == "frame"
    score = score_from_record(legacy)
    onsets = [e.onset_ms for e in score.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(0.0), pytest.approx(200.0)]


def test_millisecond_text_needs_no_fps() -> None:
    """毫秒制谱面：`capture` 里根本没有可用 fps 也能还原出正确时间轴。"""
    record = build_record_from_text("A1000F500G", spec_fwzs_stub())
    assert record["capture"]["fps"] == 0.0
    score = score_from_record(record)
    onsets = [e.onset_ms for e in score.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(0.0), pytest.approx(1000.0), pytest.approx(1500.0)]


def test_any_text_can_be_saved_as_record(tmp_path, spec_fwzs: InstrumentSpec) -> None:
    """手输 / 读取来的文本都能直接落盘（不需要先有一份 Recording）。"""
    text = "A48F144G"
    saved = save_record_text(tmp_path / "输入", text, spec_fwzs, backend="manual")
    record = load_record(saved)

    assert record["tick"]["text"] == text
    assert record["capture"]["duration_ms"] == 48 + 144
    assert record["capture"]["backend"] == "manual"
    assert record["name"] == "输入"
    score = score_from_record(record)
    assert score.note_count == 3


def test_leading_silence_is_preserved() -> None:
    """首和弦前的数字是前导静默，必须保留（否则转换外部谱面会整体提前）。"""
    record = build_record_from_text("500A1000F", spec_fwzs_stub())
    assert record["tick"]["text"] == "500A1000F"
    assert record["capture"]["duration_ms"] == 1500

    score = score_from_record(record)
    onsets = [e.onset_ms for e in score.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(500.0), pytest.approx(1500.0)]


def test_tick_text_from_score_roundtrips_absolute_times() -> None:
    """`Score` → 毫秒文本 → `Score`：整首误差 ≤0.5ms（差分按累计毫秒算，不累积）。"""
    score = Score(
        events=[
            NoteEvent(keys=("A",), onset_ms=0.0),
            NoteEvent(keys=("F",), onset_ms=333.4),
            NoteEvent(keys=("G",), onset_ms=1667.6),
        ]
    )
    text = tick_text_from_score(score)
    assert text == "A333F1335G"

    record = build_record_from_text(text, spec_fwzs_stub())
    back = score_from_record(record)
    onsets = [e.onset_ms for e in back.events if isinstance(e, NoteEvent)]
    assert onsets == [pytest.approx(0.0), pytest.approx(333.0), pytest.approx(1668.0)]


def spec_fwzs_stub() -> InstrumentSpec:
    """不需要真实音频资产的乐器定义（只用到 id/name/键位）。"""
    from yqrec.instruments import load_instrument

    return load_instrument("fwzs")


def test_tick_text_from_score_can_drop_sustain() -> None:
    """无延音乐器（风物之诗琴）渲染 tick 时不写 `[ms]`（M6d 用户要求）。

    外部谱（BGI midi / yuanqin）每个音都带按住时长，照搬到编辑框里就变成
    `A[500]620B` 这样一串噪音 —— 目标乐器不支持延音时它毫无意义。
    """
    score = Score(
        events=[
            NoteEvent(keys=("A",), onset_ms=0.0, sustain_ms=500.0),
            NoteEvent(keys=("B",), onset_ms=620.0),
        ]
    )
    assert tick_text_from_score(score) == "A[500]620B"
    assert tick_text_from_score(score, sustained=False) == "A620B"
