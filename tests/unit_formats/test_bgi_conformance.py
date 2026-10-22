"""BetterGI 曲谱格式「合规性」回归：用 **BGI 自己的解析规则** 验我们的导出。

规则来源（都要引用，别凭印象）：

- `repo/js/AutoYuanQin/README.md`
  · 字段说明：`type` 合法值 `yuanqin` / `midi` / `keyboard`；`bpm` 必要；
    `time_signature` 仅 `yuanqin` 必要；`notes` 必要
  · `Notes 解析规则`：`A[4]`=四分音符、`(ASD)[4]`=和弦、`@[4]`=休止、`[4-*]`=附点、
    `[16-#]`=装饰音、`[4-8.3]`/`.$`=连音；音符**必须大写**
- `repo/js/AutoYuanQin/main.js`
  · midi：`const regex = /^([A-Z])([A-Z@]+)(\\d+)$/`；
    `base_time = 60000 / (bpm * ticks)`；`wait_time = Math.round(note_ticks * base_time)`；
    `D` → 按下、`U` → 抬起；`const lowest_latency = 30`
  · yuanqin：`symbol = parseInt(ts.split("/")[1])`、`symbol_time = Math.round(60000 / bpm)`、
    音符时长 `Math.round(symbol_time * (symbol / note_type))`；
    播放是 `for` 循环里 `down → await playbackSleep(时长) → up`（顺序、不可叠音）

这里**不复用** `yqrec.formats.bgi_sheet` 的解析器，而是把这些规则**重新实现一遍**，
再用它去解析我们的产物 —— 否则只是"自己验自己"。
"""

from __future__ import annotations

import math
import re

import pytest

from yqrec.formats import bgi_sheet
from yqrec.types import InstrumentSpec, NoteEvent

# --------------------------------------------------------------------------- #
# BGI 侧规则的独立实现
# --------------------------------------------------------------------------- #

BGI_MIDI_RE = re.compile(r"^([A-Z])([A-Z@]+)(\d+)$")
r"""`main.js`：`const regex = /^([A-Z])([A-Z@]+)(\d+)$/;`（状态 / 键 / tick）。"""

BGI_YQ_RE = re.compile(r"(@|%|\([A-Z]+\)|[A-Z])\[([^\]]*)\]")
"""README 的 token 形状：`A[4]` / `(ASD)[4]` / `@[4]` / `%[bpm]`。"""

BGI_YQ_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ@()[]|-*.$%0123456789|")
"""README 明确要求大写字母 + 这些记号；出现别的字符就说明我们自己发明了语法。"""

YQ_ORNAMENT_DIV = 16
"""`main.js`：`let ornament_time = Math.round(symbol_time / 16)`。"""


def js_round(value: float) -> int:
    """JS `Math.round`（.5 向上），与 `main.js` 的取整口径一致。"""
    return math.floor(value + 0.5)


def bgi_midi_events(sheet: dict) -> list[tuple[float, str, str]]:
    """按 `main.js` 的 midi 分支解析：`[(绝对毫秒, "D"/"U", 单键)]`。"""
    bpm = float(sheet["bpm"])
    ticks = int(sheet["ticks"])
    base_time = 60000.0 / (bpm * ticks)  # ms / tick
    clock = 0.0
    out: list[tuple[float, str, str]] = []
    for token in sheet["notes"].split("|"):
        match = BGI_MIDI_RE.match(token)
        assert match, f"BGI 的 midi 正则吃了这个 token: {token!r}"
        status, keys, note_ticks = match.group(1), match.group(2), int(match.group(3))
        clock += js_round(note_ticks * base_time)
        for key in keys:
            out.append((clock, status, key))
    return out


def _yq_token_ms(type_text: str, symbol_time: int, symbol: int) -> float:
    """README/main.js 的时值规则：`round(symbol_time * symbol / type)`，附点 ×1.5。"""
    head, _, spl = type_text.partition("-")
    note_type = int(head)
    base = js_round(symbol_time * (symbol / note_type))
    if spl == "*":
        return js_round(base * 1.5)
    if spl == "#":
        return js_round(symbol_time / YQ_ORNAMENT_DIV)
    return base


def bgi_yuanqin_notes(sheet: dict) -> list[tuple[float, float, str]]:
    """按 `main.js` 的 yuanqin 分支解析：`[(起始毫秒, 时值, 键串)]`（休止符不返回）。"""
    bpm = float(sheet["bpm"])
    symbol = int(str(sheet["time_signature"]).split("/")[1])
    symbol_time = js_round(60000.0 / bpm)
    clock = 0.0
    out: list[tuple[float, float, str]] = []
    body = str(sheet["notes"]).replace("\n", "").replace("/n", "")
    for bar in body.split("|"):
        for match in BGI_YQ_RE.finditer(bar):
            note, type_text = match.group(1), match.group(2)
            duration = _yq_token_ms(type_text, symbol_time, symbol)
            if note == "@":
                clock += duration
                continue
            if note == "%":
                bpm = float(type_text)
                symbol_time = js_round(60000.0 / bpm)
                continue
            out.append((clock, duration, note.strip("()")))
            clock += duration
    return out


def _leaves(sheet: dict) -> str:
    """`notes` 去掉分隔符后剩下的字符（用于"没发明新语法"的检查）。"""
    return str(sheet["notes"]).replace("|", "").replace("\n", "")


# --------------------------------------------------------------------------- #
# fixtures / 工具
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def yq(spec_xljq: InstrumentSpec) -> InstrumentSpec:
    return spec_xljq


@pytest.fixture(scope="module")
def midi(spec_xljq: InstrumentSpec) -> InstrumentSpec:
    """midi 与乐器无关（都是 1 tick = 1ms），用谐律键琴只是顺手。"""
    return spec_xljq


def _score_notes(text: str) -> list[NoteEvent]:
    score = bgi_sheet.score_from_tick_text(text)
    return [e for e in score.events if isinstance(e, NoteEvent)]


# --------------------------------------------------------------------------- #
# 1. midi：正则 / 时间基准 / 事件语义
# --------------------------------------------------------------------------- #


def test_midi_fields_match_readme(midi: InstrumentSpec) -> None:
    """README 字段说明：`type` / `bpm` / `notes` 必要；`bpm*ticks` 决定每 tick 毫秒。"""
    sheet = bgi_sheet.sheet_from_tick("A100DF", midi, sheet_type="midi", name="t")
    assert sheet["type"] == "midi"
    assert sheet["bpm"] and sheet["ticks"] and sheet["notes"]
    # 1 tick = 1ms：60000 / (bpm * ticks) == 1
    assert 60000.0 / (float(sheet["bpm"]) * int(sheet["ticks"])) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "text",
    [
        "A100F",           # 单音
        "ADF100GS",        # 和弦
        "A100A100A150A",   # 同键连奏（最容易被补偿延迟破坏）
        "A[800]1000DF",    # 带延音
        "A[1200]304DF[992]",
    ],
)
def test_midi_tokens_pass_bgi_regex(midi: InstrumentSpec, text: str) -> None:
    """每一个 token 都必须能被 BGI 的 `/^([A-Z])([A-Z@]+)(\\d+)$/` 吃下。"""
    sheet = bgi_sheet.sheet_from_tick(text, midi, sheet_type="midi")
    tokens = sheet["notes"].split("|")
    assert tokens and all(t for t in tokens), f"出现空 token：{tokens}"
    for token in tokens:
        assert BGI_MIDI_RE.match(token), token
    # 反解一遍（不等式不过会在 assert 里炸）
    assert bgi_midi_events(sheet)


def test_midi_absolute_times_match_source(midi: InstrumentSpec) -> None:
    """按 BGI 的累加规则还原绝对时刻：按下=onset，抬起=onset+sustain。"""
    text = "A[800]1000DF"
    sheet = bgi_sheet.sheet_from_tick(text, midi, sheet_type="midi")
    events = bgi_midi_events(sheet)

    assert (0.0, "D", "A") in events
    assert (800.0, "U", "A") in events, f"延音没到 BGI：{events}"
    assert (1000.0, "D", "D") in events and (1000.0, "D", "F") in events
    # 没写延音的音按 DEFAULT_HOLD_MS=60 抬起
    assert (1060.0, "U", "D") in events and (1060.0, "U", "F") in events


def test_midi_same_key_gap_is_at_least_30ms(midi: InstrumentSpec) -> None:
    """同键必须留 >=30ms（`lowest_latency`）：先抬起再按下，否则 BGI 会吞音。"""
    sheet = bgi_sheet.sheet_from_tick("A[1200]100A200DF", midi, sheet_type="midi")
    events = bgi_midi_events(sheet)
    ups = [t for t, status, key in events if key == "A" and status == "U"]
    downs = [t for t, status, key in events if key == "A" and status == "D"]
    assert len(downs) == 2
    assert ups[0] <= downs[1] - bgi_sheet.MIN_REPRESS_GAP_MS + 1e-6, events
    assert downs[0] < ups[0] < downs[1]


def test_midi_allows_overlap_between_different_keys(midi: InstrumentSpec) -> None:
    """不同键可以同时按住（BGI midi 是事件流）：A 的 U 晚于 D/F 的 D。"""
    sheet = bgi_sheet.sheet_from_tick("A[1200]304DF[992]", midi, sheet_type="midi")
    events = bgi_midi_events(sheet)
    assert (1200.0, "U", "A") in events
    assert (304.0, "D", "D") in events and (304.0, "D", "F") in events


# --------------------------------------------------------------------------- #
# 2. yuanqin：token 语法 / 时值公式 / 顺序性
# --------------------------------------------------------------------------- #


def test_yuanqin_fields_match_readme(yq: InstrumentSpec) -> None:
    """README：`type`/`bpm`/`notes` 必要，`time_signature` 对 yuanqin 必要。"""
    sheet = bgi_sheet.sheet_from_tick("A100DF", yq, sheet_type="yuanqin", name="t")
    assert sheet["type"] == "yuanqin"
    assert sheet["bpm"] and sheet["notes"] and sheet["time_signature"]
    assert "/" in sheet["time_signature"]


@pytest.mark.parametrize(
    "text",
    [
        "A100F",
        "ADF100GS",
        "A[800]2000DF",
        "A[1500]",
        "A[300]300D",
    ],
)
def test_yuanqin_syntax_is_only_what_readme_documents(yq: InstrumentSpec, text: str) -> None:
    """只用 README 写明的记号（大写字母 / `()` / `[]` / `@` / `|` / `-` / `*` / 数字）。"""
    sheet = bgi_sheet.sheet_from_tick(text, yq, sheet_type="yuanqin")
    leaves = _leaves(sheet)
    unknown = set(leaves) - BGI_YQ_ALLOWED
    assert not unknown, f"出现了 BGI 认不出的字符：{unknown}"

    # 每个 token 都能被 token 形状正则匹配（`|` 只作小节分隔）
    for bar in sheet["notes"].split("|"):
        rebuilt = "".join(f"{n}[{t}]" for n, t in BGI_YQ_RE.findall(bar))
        assert rebuilt == bar, f"有游离字符：{bar!r}"


def test_yuanqin_duration_formula_matches_main_js(yq: InstrumentSpec) -> None:
    """按 `main.js` 的公式算时值：`round(symbol_time * symbol / type)`，附点 ×1.5。"""
    sheet = bgi_sheet.sheet_from_tick("A[800]2000D", yq, sheet_type="yuanqin")
    notes = bgi_yuanqin_notes(sheet)

    assert [keys for _, _, keys in notes] == ["A", "D"]
    onset_a, dur_a, _ = notes[0]
    onset_d, dur_d, _ = notes[1]
    assert (onset_a, dur_a) == (0.0, 800.0), "A[1] 在 bpm=300/4:4 下应是整音符 800ms"
    assert onset_d == pytest.approx(2000, abs=1)
    assert dur_d == 100  # 八分音符 @ bpm300/4:4


def test_yuanqin_is_sequential_by_construction(yq: InstrumentSpec) -> None:
    """yuanqin 的 token 是"一个接一个"的：后一个音的起点 = 前面所有 token 时长之和。

    这正是 BGI `main.js` 里 `for` + `down→sleep→up` 的直接后果 —— 所以"按住一个音
    同时让另一个音开始"在 yuanqin 里表达不了，长音会被下一个音的起点截断。
    """
    text = "A[1200]304DF[992]"
    sheet = bgi_sheet.sheet_from_tick(text, yq, sheet_type="yuanqin")
    notes = bgi_yuanqin_notes(sheet)

    # 第二个音的起点 = 第一个音的起点 + 第一个音的时值（不可能重合/叠音）
    assert notes[1][0] == pytest.approx(notes[0][0] + notes[0][1], abs=1e-6)
    # 源谱面里 A 要按 1200ms、DF 在 304ms 就响 → yuanqin 只能把 A 压到 304 左右
    source = _score_notes(text)
    assert source[0].sustain_ms == pytest.approx(1200)
    assert notes[0][1] < source[0].sustain_ms, "yuanqin 必然截断被后音压住的长音"


def test_yuanqin_report_matches_what_converter_does(yq: InstrumentSpec) -> None:
    """`yuanqin_sustain_loss` 报的损失要和"BGI 公式算出来的实际时值"对得上。"""
    text = "A[1200]304DF[992]"
    score = bgi_sheet.score_from_tick_text(text)
    loss = bgi_sheet.yuanqin_sustain_loss(score, yq)

    sheet = bgi_sheet.sheet_from_tick(text, yq, sheet_type="yuanqin")
    notes = bgi_yuanqin_notes(sheet)
    real_loss = [
        max(0.0, src.sustain_ms - dur)
        for src, (_, dur, _) in zip(_score_notes(text), notes, strict=False)
    ]
    assert loss.count == sum(1 for v in real_loss if v > bgi_sheet.SUSTAIN_LOSS_TOLERANCE_MS)
    assert loss.max_ms == pytest.approx(max(real_loss), abs=1)


# --------------------------------------------------------------------------- #
# 3. 两种格式都必须能被 BGI 的规则"读回来"（不是我们自己读）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sheet_type", ["midi", "yuanqin"])
def test_export_roundtrips_through_bgi_rules(sheet_type: str, yq: InstrumentSpec) -> None:
    """用 BGI 的规则解析我们的产物，onset 与源 Score 的差距要在格式允许范围内。"""
    text = "A[800]2000DF"
    sheet = bgi_sheet.sheet_from_tick(text, yq, sheet_type=sheet_type)
    source = _score_notes(text)

    if sheet_type == "midi":
        events = bgi_midi_events(sheet)
        downs = sorted({t for t, status, _ in events if status == "D"})
    else:
        downs = [t for t, _, _ in bgi_yuanqin_notes(sheet)]

    src = [e.onset_ms for e in source]
    assert len(downs) == len(src)
    assert max(abs(a - b) for a, b in zip(src, downs, strict=True)) <= 1.0
