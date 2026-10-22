"""BetterGI（AutoYuanQin）曲谱互转（M2c）。

约定来源见 `src/yqrec/formats/bgi_sheet.py` 的模块说明：
token = `[D|U]<keys><先等待的 tick 数>`，脚本先 sleep 再按键。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from yqrec.formats import bgi_sheet
from yqrec.formats.bgi_sheet import BgiSheetError
from yqrec.types import InstrumentSpec, NoteEvent, Score

CANON = Path(__file__).resolve().parents[1] / "data" / "bgi" / "canon_7BRsrJmI.json"
PU_GONG_YING_YQ = (
    Path(__file__).resolve().parents[1] / "data" / "bgi" / "pugongying_yuanqin.json"
)
PU_GONG_YING_TICK = (
    Path(__file__).resolve().parents[1] / "data" / "tick" / "pugongying_tick.txt"
)
TOKEN_RE = re.compile(r"^([DU])([A-Z@]+)(\d+)$")


def _score(*chords: tuple[float, tuple[str, ...]]) -> Score:
    return Score(events=[NoteEvent(keys=keys, onset_ms=t) for t, keys in chords])


def test_default_time_base_is_one_tick_per_ms() -> None:
    assert bgi_sheet.ms_per_tick(bgi_sheet.BGI_BPM, bgi_sheet.BGI_TICKS) == 1.0


def test_export_token_grammar_and_gaps(spec_fwzs: InstrumentSpec) -> None:
    score = _score((0.0, ("A",)), (500.0, ("D",)), (500.0, ("F",)), (1000.0, ("G",)))
    text = bgi_sheet.notes_text(score, spec_fwzs, default_hold_ms=60.0)
    tokens = text.split("|")

    assert all(TOKEN_RE.match(t) for t in tokens), tokens
    # 每 token = "先等 N tick 再动作"；同刻同状态合并成一个 token（和弦），键按 key_order 排
    assert tokens == ["DA0", "UA60", "DDF440", "UDF60", "DG440", "UG60"]


def test_export_roundtrip_keeps_onsets(spec_fwzs: InstrumentSpec) -> None:
    score = _score(
        (0.0, ("A",)),
        (123.0, ("D",)),
        (123.0, ("F",)),  # 与上一个同刻 → 导出应合并成和弦
        (900.5, ("G", "H")),
    )
    sheet = bgi_sheet.build_sheet(score, spec_fwzs, name="往返")
    assert sheet["type"] == "midi"
    assert sheet["bpm"] == "125"
    assert sheet["ticks"] == 480
    assert sheet["instrument"] == "风物之诗琴"

    back = bgi_sheet.parse_sheet(sheet)
    notes = [e for e in back.events if isinstance(e, NoteEvent)]
    assert [n.keys for n in notes] == [("A",), ("D", "F"), ("G", "H")]
    assert [n.onset_ms for n in notes] == pytest.approx([0, 123, 900.5], abs=1.0)


def test_parse_real_bgi_sheet() -> None:
    """真实曲谱（AutoYuanQin 仓库的卡农）：653 个和弦，约 2:13。"""
    score = bgi_sheet.load_sheet(CANON)
    notes = [e for e in score.events if isinstance(e, NoteEvent)]

    assert len(notes) == 653
    assert notes[0].onset_ms == pytest.approx(352.0, abs=1.0)
    assert notes[-1].onset_ms == pytest.approx(132840, abs=2)
    assert score.metadata["instrument"] == "风物之诗琴"
    assert bgi_sheet.local_instrument_id("风物之诗琴") == "fwzs"
    assert bgi_sheet.local_instrument_id("不存在的琴") is None


def test_roundtrip_real_sheet_is_lossless_enough(spec_fwzs: InstrumentSpec) -> None:
    """外部曲谱（多声部、有保持）导出再解析：音数/时刻/保持时长都基本不变。"""
    source = bgi_sheet.load_sheet(CANON)
    exported = bgi_sheet.build_sheet(source, spec_fwzs, name="卡农")
    back = bgi_sheet.parse_sheet(exported)

    src = [e for e in source.events if isinstance(e, NoteEvent)]
    dst = [e for e in back.events if isinstance(e, NoteEvent)]
    assert len(src) == len(dst)
    assert max(abs(a.onset_ms - b.onset_ms) for a, b in zip(src, dst, strict=True)) <= 1.0
    # 保持时长：外谱里有"同键快速重复"的地方必须留出 30ms 抬起间隔（BGI 的 lowest_latency），
    # 因此这类音的保持会被裁短，最大损失 ≈ 30ms；其余音误差 <1ms。
    assert max(abs(a.sustain_ms - b.sustain_ms) for a, b in zip(src, dst, strict=True)) <= 30.0


def test_save_sheet_writes_utf8_json(tmp_path, spec_fwzs: InstrumentSpec) -> None:
    score = _score((0.0, ("A",)))
    path = bgi_sheet.save_sheet(tmp_path / "测试曲", score, spec_fwzs, name="测试曲")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "测试曲.json"
    assert raw["name"] == "测试曲"
    assert raw["notes"].startswith("DA0")
    assert "\\u" not in path.read_text(encoding="utf-8")


def test_parse_rejects_unsupported_type() -> None:
    """`keyboard`（网络键盘谱）暂不支持，要给出明确错误而不是静默出错。"""
    with pytest.raises(BgiSheetError, match="midi / yuanqin"):
        bgi_sheet.parse_sheet({"type": "keyboard", "notes": "(NW) C", "bpm": "460"})


def test_parse_rejects_empty_notes() -> None:
    with pytest.raises(BgiSheetError, match="notes"):
        bgi_sheet.parse_sheet({"type": "midi", "notes": "  "})


def test_is_bgi_sheet_detection() -> None:
    assert bgi_sheet.is_bgi_sheet({"notes": "DA0", "type": "midi"}) is True
    assert bgi_sheet.is_bgi_sheet({"tick": {"text": "A3F"}, "format": "yqrec.recording"}) is False


# --------------------------------------------------------------------------- #
# yuanqin（五线谱制谱器）解析 + 真实曲谱互校
# --------------------------------------------------------------------------- #


def test_parse_yuanqin_real_sheet() -> None:
    """真实 `type: yuanqin` 曲谱（蒲公英的约定，bpm 66 / 4/4）。"""
    score = bgi_sheet.load_sheet(PU_GONG_YING_YQ)
    notes = [e for e in score.events if isinstance(e, NoteEvent)]

    assert len(notes) == 459
    assert score.metadata["bpm"] == "66"
    # 开头的 @[2] = 半休止 = 2 拍（909ms/拍）→ 第一个音在 1818ms
    assert notes[0].onset_ms == 1818
    assert notes[0].sustain_ms == pytest.approx(455, abs=1)  # 八分音符
    # 附点四分 `D[4-*]` = 909 × 1.5 = 1364ms
    assert any(n.sustain_ms == pytest.approx(1364, abs=1) for n in notes)


def test_yuanqin_bars_line_up_with_meter() -> None:
    """时值模型自洽性：70 个 4/4 小节，平均每小节应≈4 拍（3636ms）。"""
    score = bgi_sheet.load_sheet(PU_GONG_YING_YQ)
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    total = max(n.onset_ms + n.sustain_ms for n in notes)
    bars = 70

    assert abs(total / bars - 3636) < 5, f"每小节平均 {total / bars:.1f}ms"


@pytest.mark.parametrize("path", [PU_GONG_YING_YQ, PU_GONG_YING_TICK, CANON])
def test_roundtrip_through_bgi_midi_is_lossless(path: Path, spec_fwzs: InstrumentSpec) -> None:
    """三份真实谱面（yuanqin / tick / midi）→ Score → BGI(midi) → Score。

    这条是"转换准确率"的硬指标：时刻误差应 ≤1ms、和弦键集合完全一致。
    """
    from yqrec.formats import tick

    if path.suffix == ".txt":
        source = tick.parse(path.read_text(encoding="utf-8"))
    else:
        source = bgi_sheet.load_sheet(path)
    src = [e for e in source.events if isinstance(e, NoteEvent)]

    back = bgi_sheet.parse_sheet(bgi_sheet.build_sheet(source, spec_fwzs, name="rt"))
    dst = [e for e in back.events if isinstance(e, NoteEvent)]

    assert len(src) == len(dst)
    assert max(abs(a.onset_ms - b.onset_ms) for a, b in zip(src, dst, strict=True)) <= 1.0
    assert all(set(a.keys) == set(b.keys) for a, b in zip(src, dst, strict=True))


# --------------------------------------------------------------------------- #
# tick（1 秒 60 帧规格）→ BetterGI midi / yuanqin
# --------------------------------------------------------------------------- #

YQ_TOKEN_RE = re.compile(r"(@|%|\([A-Z]+\)|[A-Z])\[([^\]]*)\]")


def _notes(score: Score) -> list[NoteEvent]:
    return [e for e in score.events if isinstance(e, NoteEvent)]


def _max_onset_error(text: str, sheet: dict, spec: InstrumentSpec) -> float:
    src = _notes(bgi_sheet.score_from_tick_text(text))
    dst = _notes(bgi_sheet.parse_sheet(sheet))
    assert len(src) == len(dst), f"{len(src)} -> {len(dst)}"
    return max(abs(a.onset_ms - b.onset_ms) for a, b in zip(src, dst, strict=True))


def test_tick_text_is_millisecond_based() -> None:
    """tick 数字是**毫秒**：`A100F` → F 在 100ms，与帧率无关。"""
    notes = _notes(bgi_sheet.score_from_tick_text("A100F"))
    assert [round(n.onset_ms, 3) for n in notes] == [0.0, 100.0]


def test_tick_to_midi_one_tick_per_millisecond(spec_fwzs: InstrumentSpec) -> None:
    """`type: midi` 取 bpm=125/ticks=480 → 1 tick = 1ms，token 数字就是毫秒。"""
    sheet = bgi_sheet.sheet_from_tick("A1000F250G", spec_fwzs, sheet_type="midi", name="t")
    assert sheet["type"] == "midi"
    assert (sheet["bpm"], sheet["ticks"]) == ("125", 480)
    tokens = sheet["notes"].split("|")
    assert all(TOKEN_RE.match(t) for t in tokens), tokens
    assert tokens[0] == "DA0"  # A 在 0ms
    assert "DF940" in tokens  # F 在 1000ms，A 在 60ms 抬起 → 等 940
    assert _max_onset_error("A1000F250G", sheet, spec_fwzs) <= 1.0


def test_tick_to_midi_is_exact_on_same_key_runs(spec_fwzs: InstrumentSpec) -> None:
    """同键连奏（最容易被抬键逻辑破坏）也必须 ≤1ms。"""
    text = "A100A100A150A"
    sheet = bgi_sheet.sheet_from_tick(text, spec_fwzs, sheet_type="midi")
    assert _max_onset_error(text, sheet, spec_fwzs) <= 1.0


def test_tick_to_yuanqin_fields_and_grammar(spec_fwzs: InstrumentSpec) -> None:
    """`type: yuanqin` 必须完全符合 BGI 的 notes 记号（A[8] / (ASD)[4] / @[2]）。"""
    sheet = bgi_sheet.sheet_from_tick("A600DF120G", spec_fwzs, sheet_type="yuanqin", name="t")
    assert sheet["type"] == "yuanqin"
    assert (sheet["bpm"], sheet["ticks"]) == ("300", 480)
    assert sheet["time_signature"] == "4/4"
    assert sheet["instrument"] == "风物之诗琴"

    body = sheet["notes"].replace("|", "")
    rebuilt = "".join(f"{note}[{spl}]" for note, spl in YQ_TOKEN_RE.findall(body))
    assert rebuilt == body, body  # 没有游离字符
    assert "(" in body and "@[" in body


def test_tick_to_yuanqin_roundtrip_within_one_tier(spec_fwzs: InstrumentSpec) -> None:
    """yuanqin 往返：绝对时刻误差 ≤ 最小档位（13ms），不随音数累积。"""
    for text in (
        "A600F",
        "A100A100A150A",
        "A2000B500A2000B",
        "A1000B500C",
        "A17F33G49H",
    ):
        sheet = bgi_sheet.sheet_from_tick(text, spec_fwzs, sheet_type="yuanqin")
        assert _max_onset_error(text, sheet, spec_fwzs) <= 13.5


def test_yuanqin_same_key_run_gets_rest_token(spec_fwzs: InstrumentSpec) -> None:
    """同键连奏必须在两个音之间夹一个 `@` 休止。

    BGI 的 `hasCommonChar(本音, 下一个 token)` 为真时会扣 30ms 提前抬起再补 60ms，
    净拖慢 30ms；中间有休止符就不会触发。
    """
    sheet = bgi_sheet.sheet_from_tick("A100A100A150A", spec_fwzs, sheet_type="yuanqin")
    body = sheet["notes"]
    assert body.startswith("A[")
    assert body.count("@[") >= 3  # 前三个音后面都有休止
    assert _max_onset_error("A100A100A150A", sheet, spec_fwzs) <= 13.5


def test_yuanqin_dense_recording_stays_within_one_tier(spec_fwzs: InstrumentSpec) -> None:
    """模拟一段真实录制（多音、和弦、长短间隔混合），误差不累积。"""
    text = "DA0UG200DF50U80G330H80J150A500WF90UD200"
    sheet = bgi_sheet.sheet_from_tick(text, spec_fwzs, sheet_type="yuanqin")
    assert _max_onset_error(text, sheet, spec_fwzs) <= 13.5


def test_save_sheet_typed_writes_both_types(tmp_path, spec_fwzs: InstrumentSpec) -> None:
    for kind in ("midi", "yuanqin"):
        path = bgi_sheet.save_sheet_typed(
            tmp_path / f"t_{kind}", "A6F", spec_fwzs, sheet_type=kind, name=f"t_{kind}"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["type"] == kind
        assert raw["notes"]
        assert _notes(bgi_sheet.parse_sheet(raw))


def test_build_sheet_typed_rejects_unknown_type(spec_fwzs: InstrumentSpec) -> None:
    with pytest.raises(BgiSheetError, match="未知"):
        bgi_sheet.build_sheet_typed(
            _score((0.0, ("A",))), spec_fwzs, sheet_type="keyboard"
        )
