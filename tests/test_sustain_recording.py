"""M4 · 延音录制（谐律键琴）全链路回归。

覆盖四层，一层都不许"看起来对"：
1. 配置：`sustain_ms`（0 = 不延音 / >0 = 阈值毫秒）+ 旧 `sustain: true` 兼容；
2. 装配：按下时长是否达到阈值 → `NoteEvent.sustain_ms`（值为**完整**按住时长）；
3. 文本：`tick_text` ↔ `scan_tick_tokens` 的 `[ms]` 后缀（往返、阈值、容错）；
4. 落盘/导出：谱面 JSON 往返、tick 文件 → BetterGI（midi 精确到 1ms；yuanqin 给延音乐器放宽时值上限）。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from yqrec.formats import bgi_sheet, tick
from yqrec.instruments import load_instruments, parse_detection
from yqrec.recording import (
    RecordedNote,
    RecordingBuffer,
    parse_tick_text,
    scan_tick_tokens,
    tick_text,
)
from yqrec.scorefile import build_record, build_record_from_text, score_from_record
from yqrec.timeline.assembler import AssembleConfig, assemble
from yqrec.timeline.detector import PressDetector
from yqrec.types import (
    LEGACY_SUSTAIN_MS,
    SUSTAIN_TEXT_MIN_MS,
    InstrumentSpec,
    NoteEvent,
    PressEvent,
)
from yqrec.vision.thresholds import thresholds_from_baseline

FRAME_S = 0.016
"""模拟 60fps 采集（16ms/帧）。"""

UNPRESSED = (252, 247, 234)
"""谐律键琴未按下色（refTex 实测）。"""

PRESSED = (223, 199, 156)
"""谐律键琴按下色（refTex 实测）。"""


# --------------------------------------------------------------------------- #
# 1. 配置：sustain_ms
# --------------------------------------------------------------------------- #


def test_instruments_sustain_ms_is_the_single_source() -> None:
    """每个乐器都有 `sustain_ms`；只有谐律键琴 > 0。"""
    specs = load_instruments()
    for inst_id, spec in specs.items():
        assert spec.sustain_ms >= 0, f"{inst_id} 的 sustain_ms 不能为负"
        assert spec.sustain == (spec.sustain_ms > 0), f"{inst_id} 布尔值与阈值不一致"
    assert specs["xlyq"].sustain_ms > 0, "谐律键琴必须声明延音阈值"
    assert specs["xlyq"].sustain is True
    for inst_id in ("fwzs", "jhzq", "ljdsq", "ykq", "yy"):
        assert specs[inst_id].sustain_ms == 0, f"{inst_id} 不该有延音"


def test_legacy_sustain_flag_still_reads(tmp_path) -> None:
    """只写旧 `sustain: true`（没有 sustain_ms）→ 按 LEGACY_SUSTAIN_MS 兜底。"""
    data = json.loads((_assets() / "instruments.json").read_text(encoding="utf-8"))
    for inst in data["instruments"]:
        inst.pop("sustain_ms", None)
        inst["sustain"] = inst["id"] == "xlyq"
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    specs = load_instruments(path)
    assert specs["xlyq"].sustain_ms == LEGACY_SUSTAIN_MS
    assert specs["xlyq"].sustain is True
    assert specs["fwzs"].sustain_ms == 0


def test_invalid_sustain_ms_falls_back_to_zero(tmp_path) -> None:
    """非整数/负数/超上限 → 0（不延音）+ warning，绝不抛。"""
    data = json.loads((_assets() / "instruments.json").read_text(encoding="utf-8"))
    for inst in data["instruments"]:
        if inst["id"] == "xlyq":
            inst["sustain_ms"] = "abc"
        if inst["id"] == "jhzq":
            inst["sustain_ms"] = -5
        if inst["id"] == "ljdsq":
            inst["sustain_ms"] = 999_999
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    specs = load_instruments(path)
    assert specs["xlyq"].sustain_ms == 0
    assert specs["jhzq"].sustain_ms == 0
    assert specs["ljdsq"].sustain_ms == 0


def test_sustain_ms_wins_over_legacy_flag(spec_xlyq: InstrumentSpec) -> None:
    """两个字段同时存在时以 `sustain_ms` 为准（旧布尔不能把延音关掉）。"""
    from dataclasses import replace

    spec = replace(spec_xlyq, sustain=False)
    assert spec.sustain is True, "sustain_ms > 0 时布尔应被归一化为 True"
    assert spec.sustain_ms == spec_xlyq.sustain_ms


def test_confirm_frames_default_is_two() -> None:
    """M4 起确认帧数默认 2：延音需要"按住态持续"的证据。"""
    assert parse_detection(None, "demo").confirm_frames == 2


# --------------------------------------------------------------------------- #
# 2. 装配：阈值
# --------------------------------------------------------------------------- #


def _spec_with_threshold(spec_xlyq: InstrumentSpec, threshold: int) -> InstrumentSpec:
    from dataclasses import replace

    return replace(spec_xlyq, sustain_ms=threshold)


def _presses(pairs: list[tuple[str, bool, float]]) -> list[PressEvent]:
    return [PressEvent(key=k, down=d, ts=t) for k, d, t in pairs]


def test_hold_below_threshold_is_not_sustain(spec_xlyq: InstrumentSpec) -> None:
    """按下 299ms < 阈值 300ms → 不记延音（谱面里不该出现 `[299]`）。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    events = assemble(_presses([("A", True, 0.0), ("A", False, 0.299)]), spec)
    notes = [e for e in events if isinstance(e, NoteEvent)]
    assert notes and notes[0].sustain_ms == 0.0


def test_hold_reaching_threshold_records_full_hold(spec_xlyq: InstrumentSpec) -> None:
    """达到阈值就按**完整**按住时长记（不是"超出阈值的部分"）。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    events = assemble(_presses([("A", True, 0.0), ("A", False, 0.8)]), spec)
    notes = [e for e in events if isinstance(e, NoteEvent)]
    assert notes[0].sustain_ms == pytest.approx(800.0, abs=0.1)


def test_non_sustain_instrument_never_records_sustain(spec_fwzs: InstrumentSpec) -> None:
    """`sustain_ms = 0` 的乐器即使按住很久也不记延音（风物之诗按住不是延音）。"""
    events = assemble(_presses([("A", True, 0.0), ("A", False, 2.0)]), spec_fwzs)
    notes = [e for e in events if isinstance(e, NoteEvent)]
    assert notes[0].sustain_ms == 0.0


def test_chord_sustain_takes_max_of_qualifying_keys(spec_xlyq: InstrumentSpec) -> None:
    """和弦里只有达到阈值的键参与取最大；没达到的键不把延音拉长/拉短。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    events = assemble(
        _presses(
            [
                ("A", True, 0.0),
                ("D", True, 0.0),
                ("A", False, 0.9),  # A 按住 900ms（够阈值）
                ("D", False, 0.1),  # D 只按 100ms（不够阈值）
            ]
        ),
        spec,
        AssembleConfig(chord_window_ms=25.0),
    )
    notes = [e for e in events if isinstance(e, NoteEvent)]
    assert notes[0].keys == ("A", "D")
    assert notes[0].sustain_ms == pytest.approx(900.0, abs=1.0), "取最长且达阈值的那个键"


def test_sustain_does_not_affect_onsets(spec_xlyq: InstrumentSpec) -> None:
    """延音是"独立于 onset 的 keyUp 时刻"，不该改变 rest/onset 语义。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    events = assemble(
        _presses(
            [
                ("A", True, 0.0),
                ("A", False, 0.6),
                ("F", True, 0.5),  # 在 A 还按着的时候按下 F（叠音）
                ("F", False, 0.55),
            ]
        ),
        spec,
    )
    notes = [e for e in events if isinstance(e, NoteEvent)]
    assert [round(n.onset_ms) for n in notes] == [0, 500]
    assert notes[0].sustain_ms == pytest.approx(600.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 3. 文本：[ms] 后缀
# --------------------------------------------------------------------------- #


def _notes(*items: tuple[tuple[str, ...], int, float]) -> list[RecordedNote]:
    return [
        RecordedNote(keys=keys, onset_ms=0.0, onset_frame=0, gap_ms=gap, sustain_ms=sus)
        for keys, gap, sus in items
    ]


def test_tick_text_renders_sustain_suffix() -> None:
    text = tick_text(_notes((("A",), 0, 800), (("D", "F"), 1000, 0)))
    assert text == "A[800]1000DF"


def test_tick_text_skips_tiny_sustain() -> None:
    """小于 50ms 的延音不写（噪声级别，写进文本只会让人困惑）。"""
    assert tick_text(_notes((("A",), 0, SUSTAIN_TEXT_MIN_MS - 1))) == "A"
    assert tick_text(_notes((("A",), 0, SUSTAIN_TEXT_MIN_MS))) == f"A[{SUSTAIN_TEXT_MIN_MS}]"


def test_tick_text_roundtrip_with_sustain() -> None:
    text = "A[800]1000DF[0]"
    notes = parse_tick_text(text)
    assert [(n.keys, n.gap, n.sustain_ms) for n in notes] == [
        (("A",), 0, 800.0),
        (("D", "F"), 1000, 0.0),
    ]
    assert tick_text(_notes((("A",), 0, 800), (("D", "F"), 1000, 0))) == "A[800]1000DF"


def test_scan_tokens_keeps_spans_on_letters_only() -> None:
    """高亮/诊断用的 `start:end` 只覆盖字母，`[ms]` 不算进选中范围。"""
    token = scan_tick_tokens("A[800]1000F")[0]
    assert (token.start, token.end) == (0, 1)
    assert token.sustain_ms == 800.0


@pytest.mark.parametrize(
    "text",
    ["A[abc]F", "A[]F", "A[F", "A[ 800 ]F", "A [800] F"],
)
def test_sustain_syntax_is_forgiving(text: str) -> None:
    """非法/半截的方括号不许抛异常，也不许把里面的字母当成和弦。"""
    notes = parse_tick_text(text)
    assert [n.keys for n in notes] == [("A",), ("F",)]
    assert notes[0].gap == 0


def test_unterminated_bracket_does_not_raise() -> None:
    """没有闭合的 `]`：不当延音，也不吞后面的内容（数字仍按间隔处理）。"""
    notes = parse_tick_text("A[800 F")
    assert [n.keys for n in notes] == [("A",), ("F",)]
    assert notes[0].sustain_ms == 0.0
    assert notes[1].gap == 800


def test_legacy_text_without_sustain_still_parses() -> None:
    """老文本（没有方括号）行为完全不变。"""
    notes = parse_tick_text("A1000F")
    assert [(n.keys, n.gap, n.sustain_ms) for n in notes] == [
        (("A",), 0, 0.0),
        (("F",), 1000, 0.0),
    ]


# --------------------------------------------------------------------------- #
# 4. 录制收尾（停止识谱时按已按下毫秒记延音）
# --------------------------------------------------------------------------- #


def _rgb_pressed(keys: str, k: int = 21) -> np.ndarray:
    order = ["Q", "W", "E", "R", "T", "Y", "U", "A", "S", "D", "F", "G", "H", "J",
             "Z", "X", "C", "V", "B", "N", "M"]
    out = np.tile(np.array(UNPRESSED, dtype=np.uint8), (k, 1))
    for key in keys:
        out[order.index(key)] = PRESSED
    return out


def _detector(spec: InstrumentSpec) -> PressDetector:
    baseline = np.tile(np.array(UNPRESSED, dtype=float), (spec.key_count, 1))
    thresholds = thresholds_from_baseline(baseline, spec, spec.detection.default_delta)
    return PressDetector(spec, thresholds=thresholds, baseline=baseline)


def test_recording_buffer_reports_last_frame_ts(spec_xlyq: InstrumentSpec) -> None:
    buf = RecordingBuffer(spec_xlyq)
    assert buf.last_frame_ts is None
    buf.push_frame(0.5)
    buf.push_frame(0.75)
    assert buf.last_frame_ts == pytest.approx(0.75)


def test_stop_while_held_records_elapsed_sustain(spec_xlyq: InstrumentSpec) -> None:
    """按住不放 → 停止录制：延音 = 到**最后一帧**为止的按下毫秒数。

    这里直接模拟 `CaptureWorker.stop_recording()` 的收尾动作
    （`PressDetector.flush(最后一帧时间戳)` + 追加事件 + close），
    不依赖真实采集线程。
    """
    spec = _spec_with_threshold(spec_xlyq, 300)
    buf = RecordingBuffer(spec)
    det = _detector(spec)

    frames = 40  # 39 × 16ms = 624ms
    for index in range(frames):
        ts = index * FRAME_S
        buf.push_frame(ts)
        buf.push_events(det.update(_rgb_pressed("A"), ts))

    # 收尾：用最后一帧时间戳补抬起（worker.stop_recording 的实现）
    buf.push_events(det.flush(buf.last_frame_ts))
    recording = buf.finish()

    notes = [n for n in recording.notes if n.keys == ("A",)]
    assert notes, "A 必须被录到"
    assert notes[0].sustain_ms == pytest.approx((frames - 1) * FRAME_S * 1000.0, abs=1.0)
    assert "[624]" in recording.tick_text or "[623]" in recording.tick_text


def test_short_tap_has_no_sustain_in_text(spec_xlyq: InstrumentSpec) -> None:
    """轻点（< 阈值）录出来的文本没有 `[ms]`。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    buf = RecordingBuffer(spec)
    det = _detector(spec)
    for index in range(30):
        ts = index * FRAME_S
        buf.push_frame(ts)
        rgb = _rgb_pressed("A") if index < 8 else _rgb_pressed("")  # 按下 7 帧 ≈112ms
        buf.push_events(det.update(rgb, ts))
    recording = buf.finish()
    assert "[" not in recording.tick_text


# --------------------------------------------------------------------------- #
# 5. 落盘 + 转 BetterGI
# --------------------------------------------------------------------------- #


def test_score_json_marks_sustain_and_restores_it(spec_xlyq: InstrumentSpec) -> None:
    """`build_record_from_text` → JSON；`score_from_record` 读回来延音还在（文本即真相）。"""
    record = build_record_from_text("A[800]1000F", spec_xlyq, fps=60.0)
    assert record["tick"]["sustain"] is True
    assert record["tick"]["text"] == "A[800]1000F"

    score = score_from_record(record)
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    assert [(round(n.onset_ms), n.sustain_ms) for n in notes] == [(0, 800.0), (1000, 0.0)]


def test_record_from_recording_carries_sustain(spec_xlyq: InstrumentSpec) -> None:
    """录制结果 → JSON：`tick.sustain` 标记与文本里的 `[ms]` 一致。"""
    spec = _spec_with_threshold(spec_xlyq, 300)
    buf = RecordingBuffer(spec)
    det = _detector(spec)
    for index in range(40):
        ts = index * FRAME_S
        buf.push_frame(ts)
        buf.push_events(det.update(_rgb_pressed("A"), ts))
    buf.push_events(det.flush(buf.last_frame_ts))
    recording = buf.finish()

    record = build_record(recording, spec, name="延音")
    assert record["tick"]["sustain"] is True
    assert record["tick"]["text"].startswith("A[")



def _bgi_events(sheet: dict) -> list[tuple[int, str, str]]:
    """把 BGI `notes` 拆成 `[(绝对毫秒, "D"/"U", 键)]`。

    BGI 的数字是**相对上一个 token 的增量**（不是绝对时刻），所以这里累加。
    """
    events: list[tuple[int, str, str]] = []
    clock = 0
    for token in sheet["notes"].split("|"):
        kind, rest = token[0], token[1:]
        split = 1
        while split < len(rest) and rest[split].isalpha():
            split += 1
        keys, delta = rest[:split], int(rest[split:])
        clock += delta
        events.append((clock, kind, keys))
    return events


def test_tick_text_to_bgi_midi_keeps_keyup(spec_xlyq: InstrumentSpec) -> None:
    """tick 文本（含延音）→ BGI midi：`UA` 必须正好落在 onset+800ms（1 tick = 1ms）。"""
    sheet = bgi_sheet.sheet_from_tick("A[800]1000DF", spec_xlyq, sheet_type="midi")
    events = _bgi_events(sheet)
    assert events[0] == (0, "D", "A")
    assert events[1] == (800, "U", "A"), f"延音被吃掉或算错：{events}"
    assert events[2] == (1000, "D", "DF"), "下一个和弦仍在 1000ms"

    # 反解回来：keyUp 时刻 = onset + sustain
    back = bgi_sheet.parse_sheet(sheet)
    notes = [e for e in back.events if isinstance(e, NoteEvent)]
    assert notes[0].onset_ms == pytest.approx(0, abs=1)
    assert notes[0].sustain_ms == pytest.approx(800, abs=1)


def test_tick_text_to_bgi_yuanqin_keeps_long_hold(spec_xlyq: InstrumentSpec) -> None:
    """`type: yuanqin`：延音乐器的音符时值可以长于 100ms（时值就是延音）。"""
    sheet = bgi_sheet.sheet_from_tick("A[800]1000DF", spec_xlyq, sheet_type="yuanqin")
    body = sheet["notes"].replace("|", "")
    assert body.startswith("A[1]"), f"800ms 应写成整音符 A[1]，实际 {body}"

    back = bgi_sheet.parse_sheet(sheet)
    notes = [e for e in back.events if isinstance(e, NoteEvent)]
    assert notes[0].sustain_ms == pytest.approx(800, abs=1)
    assert notes[1].onset_ms == pytest.approx(1000, abs=1)


def test_tick_file_with_sustain_converts_to_bgi(spec_xlyq: InstrumentSpec) -> None:
    """「数字 Tick 谱」文件（`A.800 200 F`）→ BGI：文件格式的延音也要带过去。"""
    # tick 谱里休止数字是"相对上一个音的起点"，所以 A 在 0 按住 800、F 在 200，
    # 形成"低音按住、旋律在上方走"的叠音 —— BGI 事件流本来就允许这样。
    source = tick.TickFormat().parse("A.800 200 F")
    sheet = bgi_sheet.build_sheet(source, spec_xlyq, name="tick文件")
    events = _bgi_events(sheet)
    assert (0, "D", "A") in events
    assert (800, "U", "A") in events, f"延音没带到 BGI：{events}"
    assert (200, "D", "F") in events, f"F 应在 200ms：{events}"

    back = bgi_sheet.parse_sheet(sheet)
    notes = [e for e in back.events if isinstance(e, NoteEvent)]
    assert [round(n.onset_ms) for n in notes] == [0, 200]
    assert notes[0].sustain_ms == pytest.approx(800, abs=1)


def test_non_sustain_instrument_bgi_uses_default_hold(spec_fwzs: InstrumentSpec) -> None:
    """不延音的乐器行为不变：文本没有 `[ms]` 时按默认 60ms 抬键。"""
    sheet = bgi_sheet.sheet_from_tick("A100DF", spec_fwzs, sheet_type="midi")
    tokens = sheet["notes"].split("|")
    assert tokens[0] == "DA0"
    assert tokens[1] == "UA60"


def _assets():
    from yqrec.instruments import assets_dir

    return assets_dir()


# --------------------------------------------------------------------------- #
# 4b. 真实调用点：CaptureWorker.stop_recording（锁 + flush + finish(extra)）
# --------------------------------------------------------------------------- #


def test_worker_stop_recording_flushes_held_keys(spec_xlyq: InstrumentSpec) -> None:
    """`CaptureWorker.stop_recording()` 自己负责补抬：不靠调用方记得 flush。"""
    from yqrec.types import KeyGrid, KeyPoint
    from yqrec.ui.capture_worker import CaptureWorker

    spec = _spec_with_threshold(spec_xlyq, 300)
    worker = CaptureWorker()
    worker._spec = spec
    worker._grid = KeyGrid(
        points=tuple(KeyPoint(u=0.1 + 0.01 * i, v=0.8, r=0.02) for i in range(21)),
        key_count=21,
    )
    det = _detector(spec)
    worker._detector = det
    assert worker.start_recording()

    frames = 40
    for index in range(frames):
        ts = index * FRAME_S
        worker._rec.push_frame(ts)
        worker._rec.push_events(det.update(_rgb_pressed("A"), ts))

    recording = worker.stop_recording()
    assert recording is not None
    notes = [n for n in recording.notes if n.keys == ("A",)]
    assert notes and notes[0].sustain_ms == pytest.approx((frames - 1) * FRAME_S * 1000.0, abs=1.0)
    assert worker.recording is False, "收尾后缓冲要摘干净"
