"""BetterGI · AutoYuanQin 曲谱（`type: "midi"`）读写（M2c）。

格式来源（外部事实，已核对脚本源码 `AutoYuanQin/main.js` 的 `play_sheet()`）：

```jsonc
{
  "name": "卡农", "author": "MidiTrans", "instrument": "风物之诗琴",
  "bpm": "120", "ticks": 480, "type": "midi",
  "description": "", "time_signature": "4/4", "composer": "", "arranger": "",
  "notes": "DZ338|DE39|UZ326|..."          // 以 | 分隔的**事件**序列
}
```

每个 token 的语法：`^([DU])([A-Z@]+)(\\d+)$`

- `D` / `U` = 按下 / 抬起；
- 中间是**游戏键名**（可多个 = 和弦，`@` 表示休息、无实际按键）；
- 末尾整数 = **执行本事件之前先等待多少 tick**。

关键点（**最容易搞反**）：脚本是 `await sleep(n * baseTime)` **然后**才 `keyDown/keyUp`，
所以 token 里的数字是"**本事件之前**的间隔"，不是"之后"的时值。用真实谱面（`卡农`）
两种假设各跑一遍即可分辨：按"之前"解释，按键保持时长为 153–1580ms（中位 360ms）；
按"之后"解释会出现大量 0–18ms 的假保持 —— 前者才是对的。

时间基准：`ms_per_tick = 60000 / (bpm * ticks)`；脚本里的默认 `bpm=120, ticks=480`。
本项目导出时取 **bpm=125, ticks=480**，于是 **1 tick = 1ms**，即 `onset_ms` 可直接当 tick 用，
不引入量化误差（`bpm` 对 `type: midi` 只影响时间基准，不影响乐谱记号）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..types import InstrumentSpec, NoteEvent, Score

_log = get_logger("formats.bgi_sheet")

BGI_TICKS = 480
"""每拍 tick 数（PPQ）。"""

BGI_BPM = 125.0
"""导出用 BPM：125×480 = 60000 → 1 tick = 1ms（见模块说明）。"""

DEFAULT_HOLD_MS = 60.0
"""一期无延音：每键按下保持的默认时长（ms）。"""

MIN_REPRESS_GAP_MS = 30.0
"""同一键"上一次抬起"到"下一次按下"的最小间隔。

BGI 脚本里 `lowest_latency = 30`：间隔**小于** 30ms 时它会强行补到 30ms（"补足延迟"），
把节奏拉长。取正好 30ms（判断是 `>=`，走正常分支），既不被拉伸，又让保持时长损失最小。
"""

_TOKEN_RE = re.compile(r"^([DU])([A-Z@]+)(\d+)$")
_BPM_MARK_RE = re.compile(r"^\*(\d+(?:\.\d+)?)$")

BGI_INSTRUMENT_NAMES: dict[str, str] = {
    "fwzs": "风物之诗琴",
    "xlyq": "谐律键琴",
    "jhzq": "镜花之琴",
    "ljdsq": "老旧的诗琴",
    "ykq": "悠可琴",
    "yy": "余音",
}
"""本地乐器 id → BGI 曲谱 `instrument` 字段用的显示名（可选项，仅提示）。"""


class BgiSheetError(ValueError):
    """曲谱不是可解析的 BetterGI 曲谱。"""


# --------------------------------------------------------------------------- #
# 导出：Score → BGI
# --------------------------------------------------------------------------- #


def ms_per_tick(bpm: float = BGI_BPM, ticks: int = BGI_TICKS) -> float:
    """一个 tick 多少毫秒。"""
    if bpm <= 0 or ticks <= 0:
        raise BgiSheetError(f"BPM/tick 必须为正: bpm={bpm}, ticks={ticks}")
    return 60000.0 / (float(bpm) * int(ticks))


def bgi_instrument_name(spec: InstrumentSpec) -> str:
    return BGI_INSTRUMENT_NAMES.get(spec.id, spec.name)


def local_instrument_id(bgi_name: str) -> str | None:
    """BGI 曲谱里的乐器名 → 本地乐器 id（认不出返回 `None`）。"""
    target = bgi_name.strip()
    for local_id, name in BGI_INSTRUMENT_NAMES.items():
        if name == target:
            return local_id
    return None


def _holds_by_key(
    notes: list[NoteEvent], default_hold_ms: float, min_gap_ms: float
) -> list[dict[str, float]]:
    """每个和弦里每个键的保持时长：按**同键**下一次按下为上限。

    只按同键裁剪（不同键可以自由重叠）—— BetterGI 的事件流本来就允许交叠声部，
    这样往返外部曲谱时不会把"低音按住、旋律在上方走"压成单声部。
    """
    next_same: list[dict[str, float]] = []
    seen: dict[str, float] = {}
    for event in reversed(notes):
        next_same.append(dict(seen))
        for key in event.keys:
            seen[key] = float(event.onset_ms)
    next_same.reverse()

    holds: list[dict[str, float]] = []
    for index, event in enumerate(notes):
        onset = float(event.onset_ms)
        base = float(event.sustain_ms) if event.sustain_ms > 0 else float(default_hold_ms)
        per_key: dict[str, float] = {}
        for key in event.keys:
            hold = base
            following = next_same[index].get(key)
            # 该键稍后还要再按 → 必须在此之前抬起，留出最小间隔
            if following is not None:
                hold = min(hold, max(1.0, following - onset - min_gap_ms))
            per_key[key] = max(1.0, hold)
        holds.append(per_key)
    return holds


def build_events(
    score: Score,
    *,
    default_hold_ms: float = DEFAULT_HOLD_MS,
    min_gap_ms: float = MIN_REPRESS_GAP_MS,
) -> list[tuple[float, bool, str]]:
    """`Score` → `[(time_ms, is_down, key), ...]`（未分组的原始事件）。"""
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    holds = _holds_by_key(notes, default_hold_ms, min_gap_ms)
    events: list[tuple[float, bool, str]] = []
    for event, hold_of_key in zip(notes, holds, strict=True):
        onset = float(event.onset_ms)
        for key in event.keys:
            events.append((onset, True, key))
            events.append((onset + hold_of_key[key], False, key))
    events.sort(key=lambda item: (item[0], 0 if item[1] else 1, item[2]))
    return events


def notes_text(
    score: Score,
    spec: InstrumentSpec,
    *,
    bpm: float = BGI_BPM,
    ticks: int = BGI_TICKS,
    default_hold_ms: float = DEFAULT_HOLD_MS,
) -> str:
    """`Score` → `notes` 字符串（`|` 分隔的 D/U 事件，含和弦合并与帧抖动量避免）。"""
    step = ms_per_tick(bpm, ticks)
    order = {key: index for index, key in enumerate(spec.key_order)}
    events = build_events(score, default_hold_ms=default_hold_ms)

    groups: list[list[Any]] = []  # [tick, is_down, [keys]]
    for time_ms, is_down, key in events:
        tick = int(round(time_ms / step))
        if groups and groups[-1][0] == tick and groups[-1][1] is is_down:
            groups[-1][2].append(key)
        else:
            groups.append([tick, is_down, [key]])

    tokens: list[str] = []
    previous_tick = 0
    for tick, is_down, keys in groups:
        wait = max(0, tick - previous_tick)
        previous_tick = tick
        ordered = "".join(sorted(keys, key=lambda k: order.get(k, 999)))
        tokens.append(f"{'D' if is_down else 'U'}{ordered}{wait}")
    return "|".join(tokens)


def build_sheet(
    score: Score,
    spec: InstrumentSpec,
    *,
    name: str = "",
    author: str = "YQ_moTools",
    bpm: float = BGI_BPM,
    ticks: int = BGI_TICKS,
    description: str = "",
    time_signature: str = "4/4",
    composer: str = "",
    arranger: str = "",
    default_hold_ms: float = DEFAULT_HOLD_MS,
) -> dict[str, Any]:
    """组装成 BGI 曲谱 dict（可直接写 JSON）。"""
    return {
        "name": name,
        "author": author,
        "instrument": bgi_instrument_name(spec),
        "description": description,
        "type": "midi",
        "bpm": f"{bpm:g}",
        "time_signature": time_signature,
        "composer": composer,
        "arranger": arranger,
        "notes": notes_text(
            score, spec, bpm=bpm, ticks=ticks, default_hold_ms=default_hold_ms
        ),
        "ticks": int(ticks),
    }


def save_sheet(
    path: Path | str,
    score: Score,
    spec: InstrumentSpec,
    *,
    name: str = "",
    **kwargs: Any,
) -> Path:
    """写 BGI 曲谱 JSON（自动补 `.json`、建目录），返回实际路径。"""
    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet = build_sheet(score, spec, name=name or target.stem, **kwargs)
    target.write_text(
        json.dumps(sheet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


# --------------------------------------------------------------------------- #
# 转换：tick（毫秒间隔）→ BetterGI `type: "midi"` / `type: "yuanqin"`
# --------------------------------------------------------------------------- #

YQ_BPM = 300.0
"""`type: "yuanqin"` 的 BPM：`round(60000/300) = 200ms` 一拍。

一拍 200ms 时，二分/四分/八分/十六分/三十二分音符分别是 400/200/100/50/25ms，
配合附点 `-*` 后可用几千分之一秒的粒度组合出**任意毫秒间隔**
（最小档位 `[64]` = 13ms），量化误差 ≤ 半个最小档（约 6ms），且不随音数累积。
"""

YQ_TYPES = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64)
"""允许的 `[n]` 分母：1/2 全音符、四分、八分、十六分、三十二分 + 三连音族（3/6/12/24/48）。"""

YQ_MAX_HOLD_MS = 100.0
"""单个音符最多占 100ms（**非延音乐器**）。原神乐器按下即发声，时值只影响按键时长。"""

YQ_MAX_HOLD_MS_SUSTAIN = 4000.0
"""延音乐器（`sustain_ms > 0`）单个音符的时值上限。

谐律键琴这类乐器**时值就是延音**（按住越久响越久），所以不能套用 100ms 上限，
否则导出的 yuanqin 谱会把长按全压成 100ms 的短触。仍受"下一个音的起点"约束
（yuanqin 是线性格式，表达不了叠音）。
"""


def score_from_tick_text(text: str) -> Score:
    """tick 文本 → `Score`。数字是**毫秒**间隔（`A1000F` = 隔 1 秒），无需帧率。

    和弦后缀 `[ms]` 是延音（`A[800]`）：直接进 `NoteEvent.sustain_ms`，
    导出 BGI 时就是该键的 keyUp 时刻（`build_events` 会按同键下一次按下裁剪）。
    """
    from ..recording import parse_tick_text  # 延迟导入，避免 formats ↔ recording 循环

    events: list[NoteEvent] = []
    cursor = 0.0
    for note in parse_tick_text(text):
        cursor += note.gap  # 首音也加：保留开头的休止
        events.append(
            NoteEvent(keys=note.keys, onset_ms=cursor, sustain_ms=note.sustain_ms)
        )
    return Score(events=events, metadata={"source": "tick"})


# -- midi ------------------------------------------------------------------- #


def build_midi_sheet(
    score: Score,
    spec: InstrumentSpec,
    *,
    name: str = "",
    author: str = "YQ_moTools",
    description: str = "",
) -> dict[str, Any]:
    """`Score` → `type: "midi"` 曲谱（默认 `bpm=125/ticks=480` → **1 tick = 1ms**）。

    时间基准取 1ms/tick，`onset_ms` 直接四舍五入成 tick，量化误差 ≤0.5ms。
    """
    return build_sheet(
        score,
        spec,
        name=name,
        author=author,
        description=description,
    )


# -- yuanqin ---------------------------------------------------------------- #


def _chord_text(keys: tuple[str, ...]) -> str:
    """`("A",)` → `A`；`("A","S","D")` → `(ASD)`。"""
    return keys[0] if len(keys) == 1 else "(" + "".join(keys) + ")"


def _yq_ms_table(bpm: float = YQ_BPM) -> list[tuple[float, str]]:
    """可用时值表 `[(毫秒, type 文本)]`，降序（含附点 `-*`）。

    毫秒一律用 `_js_round` 计算，**与 `parse_yuanqin` / BGI 的算法逐字一致**：
    编码端必须拿"解析端会算出的时长"来推进时间轴，否则每个 token 的取整误差会累积
    （实测：按帧推进 1023 音会漂到 90ms；按毫秒推进后 ≤1 个最小档位）。
    """
    symbol_ms = _js_round(60000.0 / bpm)
    table: dict[float, str] = {}
    for note_type in YQ_TYPES:
        ms = _js_round(symbol_ms * 4.0 / note_type)
        table.setdefault(float(ms), str(note_type))
        table.setdefault(float(_js_round(ms * 1.5)), f"{note_type}-*")
    return sorted(table.items(), key=lambda item: -item[0])


def _yq_decompose_ms(
    total_ms: float, table: list[tuple[float, str]]
) -> list[tuple[float, str]]:
    """把一段时长贪心拆成若干时值（余数 < 最小档位，不跨音累积）。"""
    parts: list[tuple[float, str]] = []
    remaining = float(total_ms)
    smallest = table[-1][0]
    while remaining >= smallest - 1e-6 and len(parts) < 512:
        for ms, text in table:
            if ms <= remaining + 1e-6:
                parts.append((ms, text))
                remaining -= ms
                break
        else:
            break
    return parts


def _yq_largest_ms(
    total_ms: float, table: list[tuple[float, str]]
) -> tuple[float, str]:
    """不大于 `total_ms` 的最大时值（音符时值必须是**单个** token）。"""
    for ms, text in table:
        if ms <= total_ms + 1e-6:
            return ms, text
    return table[-1]


def _yq_nearest_ms(
    want_ms: float, table: list[tuple[float, str]], upper_ms: float
) -> tuple[float, str]:
    """在**不超过 `upper_ms`** 的时值里挑最接近 `want_ms` 的那个。

    延音用的是这个而不是 `_yq_largest_ms`：时值档是 1/1.5/2/3/4… 倍关系，长音附近
    相邻档差到 200~400ms，一律向下取整会把延音砍掉两成以上（实测 1000ms → 800ms）。
    取最近档能把误差压到相邻档的一半；同等接近时取**较小**档（宁可早抬，不要晚抬）。

    `upper_ms` 决定"不许越过"的硬边界（下一个音的起点 / 单音上限），
    因此不会出现"音符比可用跨度还长"导致后续 onset 漂移。
    """
    candidates = [(ms, text) for ms, text in table if ms <= upper_ms + 1e-6]
    if not candidates:
        return table[-1]
    return min(candidates, key=lambda item: (abs(item[0] - want_ms), item[0]))


def yuanqin_notes_text(
    score: Score,
    spec: InstrumentSpec,
    *,
    bpm: float = YQ_BPM,
    max_hold_ms: float = YQ_MAX_HOLD_MS,
) -> str:
    """`Score` → `type: "yuanqin"` 的 `notes` 文本（音符时值记号）。

    规则（对齐 `main.js` 的解析）：

    - 每个 token 的时值 = 它占用的时间；和弦写 `(ASD)[8]`，单音写 `A[8]`；
    - 两个音之间的空档用 `@[n]` 休止补齐，**休止可拆成多个 token**
      （如 417ms = `@[1-*]`×5 + `@[1]` + `@[8]`）；
    - 音符时值取自时值表且不越过下一个音的起点（上限 `max_hold_ms`）；
    - 每满 4 拍补一个 `|` 分节符（仅可读性，解析时按 `|` 切分）。

    时间轴按**绝对时刻**对齐：每个音都从自己的 `onset_ms` 重新算间隔，
    因此取整误差不累积，单音偏差 ≤ 一个最小档位（13ms）。
    """
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    if not notes:
        return ""
    order = {key: index for index, key in enumerate(spec.key_order)}
    table = _yq_ms_table(bpm)
    bar_ms = _js_round(60000.0 / bpm) * 4.0
    min_note_ms = 1.0

    targets = [max(0.0, float(e.onset_ms)) for e in notes]
    parts: list[str] = []
    elapsed = 0.0
    bars_done = 0

    def emit(text: str, ms: float) -> None:
        nonlocal elapsed, bars_done
        parts.append(text)
        elapsed += ms
        if bar_ms > 0 and elapsed >= (bars_done + 1) * bar_ms - 1e-6:
            parts.append("|")  # 一个 token 最多补一个分节符，避免出现 `||`
            bars_done = int(elapsed // bar_ms)

    cursor = 0.0
    smallest_ms = table[-1][0]
    for index, event in enumerate(notes):
        target = targets[index]
        gap = target - cursor
        if gap >= smallest_ms - 1e-6:
            for ms, text in _yq_decompose_ms(gap, table):
                emit(f"@[{text}]", ms)
                cursor += ms  # 跟"解码端实际时间"走，余数留给下一个 gap 吸收

        keys = tuple(
            sorted((k for k in event.keys if k in order), key=order.__getitem__)
        )
        sustain = float(event.sustain_ms)
        limit = max_hold_ms
        if spec.sustain_ms > 0:
            # 延音乐器：时值 = keyUp 时刻，允许长于 100ms（见 YQ_MAX_HOLD_MS_SUSTAIN）
            limit = max(max_hold_ms, YQ_MAX_HOLD_MS_SUSTAIN)
        if index + 1 < len(targets):
            span = targets[index + 1] - target
            hard_upper = span
        else:
            # 最后一个音：没有"下一个音"约束，跨度只用于"没有延音时填满"，
            # 取最近档时可以向上靠一档（否则延音只能向下取整，误差更大）
            span = max(max_hold_ms, sustain)
            hard_upper = max(span, limit)
        upper = hard_upper
        if index + 1 < len(notes) and set(event.keys) & set(notes[index + 1].keys):
            # 下一个音有共同键：必须留出抬起间隔，且**让中间多出一个 `@` 休止 token** ——
            # BGI 的 `hasCommonChar(本音, 下一 token)` 为真时会扣 30ms 提前抬起再补 60ms，
            # 净效果把节奏拖慢 30ms；中间夹了休止符就不会触发。
            upper = max(min_note_ms, span - MIN_REPRESS_GAP_MS)
        upper = min(upper, limit)
        # 有显式延音时以它为目标（取最近档）；没有就填满到下一个音（等于旧行为）
        want = min(sustain if sustain > 0 else span, upper)
        hold_ms, hold_text = _yq_nearest_ms(max(min_note_ms, want), table, upper)
        emit(f"{_chord_text(keys) if keys else '@'}[{hold_text}]", hold_ms)
        cursor += hold_ms

    return "".join(parts)


def build_yuanqin_sheet(
    score: Score,
    spec: InstrumentSpec,
    *,
    name: str = "",
    author: str = "YQ_moTools",
    description: str = "",
    bpm: float = YQ_BPM,
    time_signature: str = "4/4",
    composer: str = "",
    arranger: str = "",
) -> dict[str, Any]:
    """`Score` → `type: "yuanqin"` 曲谱（音符时值记号，字段与官方制谱器一致）。"""
    return {
        "name": name,
        "author": author,
        "instrument": bgi_instrument_name(spec),
        "description": description,
        "bpm": f"{bpm:g}",
        "time_signature": time_signature,
        "composer": composer,
        "arranger": arranger,
        "notes": yuanqin_notes_text(score, spec, bpm=bpm),
        "type": "yuanqin",
        "ticks": BGI_TICKS,
    }


# -- 统一入口 --------------------------------------------------------------- #


def build_sheet_typed(
    score: Score,
    spec: InstrumentSpec,
    *,
    sheet_type: str = "midi",
    name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """按 `sheet_type`（`"midi"` / `"yuanqin"`）生成曲谱 dict。"""
    kind = str(sheet_type).strip().lower()
    if kind == "yuanqin":
        return build_yuanqin_sheet(score, spec, name=name, **kwargs)
    if kind == "midi":
        return build_midi_sheet(score, spec, name=name, **kwargs)
    raise BgiSheetError(f"未知的 BetterGI 曲谱类型: {sheet_type!r}")


@dataclass(frozen=True, slots=True)
class SustainLoss:
    """yuanqin 导出时"延音听不出原样"的统计（供 UI 导出后提示）。"""

    count: int = 0
    max_ms: float = 0.0
    """单处最大损失（毫秒）。"""

    def __bool__(self) -> bool:
        return self.count > 0


SUSTAIN_LOSS_TOLERANCE_MS = 30.0
"""损失小于它就当没损失（远小于一个十六分音符，听不出来）。"""


def yuanqin_sustain_loss(
    score: Score, spec: InstrumentSpec, *, bpm: float = YQ_BPM
) -> SustainLoss:
    """统计 `type: "yuanqin"` 会损失多少延音（**不修改** `score`）。

    两种损失都来自格式本身，不是 bug：

    1. **叠音**：yuanqin 的 token 是顺序播放的（按下→按够时值→抬起），一个键还按着
       另一个键就响，表达不了 → 长音被下一个音的起点截断；
    2. **时值量化**：音符时值只能取 `1 / 1.5 / 2 / 3 / 4 …` 倍的基础符号时长，
       长音附近相邻档差 200~400ms，只能就近取档。

    导出仍然照做（有损但可听），只是要如实告诉用户"丢了多少、最长多少"。
    """
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    if not notes:
        return SustainLoss()
    table = _yq_ms_table(bpm)
    limit = YQ_MAX_HOLD_MS if spec.sustain_ms <= 0 else YQ_MAX_HOLD_MS_SUSTAIN

    count = 0
    worst = 0.0
    for index, event in enumerate(notes):
        want = float(event.sustain_ms)
        if want <= 0:
            continue
        if index + 1 < len(notes):
            span = notes[index + 1].onset_ms - event.onset_ms
            upper = span
            if set(event.keys) & set(notes[index + 1].keys):
                upper = max(1.0, span - MIN_REPRESS_GAP_MS)
        else:
            upper = max(YQ_MAX_HOLD_MS, want)
        upper = min(upper, limit)
        got, _ = _yq_nearest_ms(max(1.0, min(want, upper)), table, upper)
        loss = want - got
        if loss > SUSTAIN_LOSS_TOLERANCE_MS:
            count += 1
            worst = max(worst, loss)
    return SustainLoss(count=count, max_ms=worst)


def sheet_from_tick(
    text: str,
    spec: InstrumentSpec,
    *,
    sheet_type: str = "midi",
    name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """tick 文本（毫秒间隔）→ BetterGI 曲谱 dict。"""
    score = score_from_tick_text(text)
    return build_sheet_typed(score, spec, sheet_type=sheet_type, name=name, **kwargs)


def save_sheet_typed(
    path: Path | str,
    text: str,
    spec: InstrumentSpec,
    *,
    sheet_type: str = "midi",
    name: str = "",
    **kwargs: Any,
) -> Path:
    """把 tick 文本按指定类型写成 BGI 曲谱 JSON，返回实际路径。"""
    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet = sheet_from_tick(
        text, spec, sheet_type=sheet_type, name=name or target.stem, **kwargs
    )
    target.write_text(
        json.dumps(sheet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


# --------------------------------------------------------------------------- #
# 导入：BGI → Score
# --------------------------------------------------------------------------- #


def is_bgi_sheet(data: dict[str, Any]) -> bool:
    """粗判：有 `notes` 字符串且没有本项目的 `tick` 块。"""
    return isinstance(data.get("notes"), str) and "tick" not in data


# --------------------------------------------------------------------------- #
# 导入：BGI `type: "yuanqin"`（五线谱/简谱制谱器格式）
# --------------------------------------------------------------------------- #

_YQ_TOKEN_RE = re.compile(r"(@|%|\([A-Z]+\)|[A-Z])\[([^\]]*)\]")
"""`A[8]` / `(ASD)[4]` / `@[2]` / `4-*` / `16-#` / `4-8.3`。"""

_YQ_ORNAMENT_DIV = 16
"""装饰音时值 = 一拍 / 16（与 main.js 一致）。"""


def _js_round(value: float) -> int:
    """JS `Math.round`（.5 向上），Python 的 `round` 是银行家舍入，不能直接用。"""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def _yq_duration(type_text: str, symbol_time: float, symbol: int) -> tuple[float, str]:
    """解析 `4` / `4-*` / `16-#` / `4-8.3` → `(时长 ms, 特殊标记)`。"""
    head, _, spl = type_text.partition("-")
    try:
        note_type = float(head)
    except ValueError:
        note_type = 4.0
    if note_type <= 0:
        return 0.0, spl or "none"
    base = _js_round(symbol_time * (symbol / note_type))
    if spl == "*":  # 附点：1.5 倍
        return _js_round(base * 1.5), spl
    if spl == "#":  # 装饰音：固定 1/16 拍，且从前一个音的时长里扣
        return _js_round(symbol_time / _YQ_ORNAMENT_DIV), spl
    return base, (spl or "none")


def parse_yuanqin(data: dict[str, Any]) -> Score:
    """`type: "yuanqin"` 曲谱 → `Score`。

    时值规则对齐 `main.js`：`symbol_time = round(60000/bpm)`；
    每音 = `round(symbol_time * (symbol / type))`，附点 ×1.5，装饰音 1/16 拍。
    连音（`.3/.6/.$`）按"组总时长均分"近似（组内音数均摊），该类型在本项目曲库里尚未出现。
    """
    notes_text_raw = data.get("notes")
    if not isinstance(notes_text_raw, str) or not notes_text_raw.strip():
        raise BgiSheetError("曲谱里没有 notes 文本")
    try:
        bpm = float(data.get("bpm") or 120)
    except (TypeError, ValueError) as exc:
        raise BgiSheetError(f"bpm 无法解析: {data.get('bpm')!r}") from exc
    signature = str(data.get("time_signature") or "4/4")
    try:
        symbol = int(signature.split("/")[1])
    except (IndexError, ValueError):
        symbol = 4

    symbol_time = _js_round(60000.0 / bpm)
    cursor = 0.0
    events: list[NoteEvent] = []
    last_note_index: int | None = None
    tuplet: list[tuple[float, tuple[str, ...], float]] = []
    tuplet_total = 0.0

    body = notes_text_raw.replace("\n", "").replace("/n", "").replace(" ", "")
    tokens = [m for bar in body.split("|") for m in _YQ_TOKEN_RE.finditer(bar)]

    index = 0
    while index < len(tokens):
        match = tokens[index]
        note_text, type_text = match.group(1), match.group(2)

        if note_text == "%":  # BPM 变速标记
            try:
                bpm = float(type_text)
                symbol_time = _js_round(60000.0 / bpm)
            except ValueError:
                _log.warning("跳过非法 BPM 标记: %r", type_text)
            index += 1
            continue

        duration, spl = _yq_duration(type_text, symbol_time, symbol)

        # 连音：累积到 .$ 结束，按组总时长均分
        if spl.endswith((".3", ".6", ".$")):
            if not tuplet:
                tuplet_total = duration
            tuplet.append((cursor, _keys_of(note_text), duration))
            cursor += duration
            if "$" in spl or index + 1 >= len(tokens):
                count = len(tuplet)
                if count:
                    each = tuplet_total / count
                    start = tuplet[0][0]
                    for offset, (_, keys, _) in enumerate(tuplet):
                        if keys:
                            events.append(
                                NoteEvent(keys=keys, onset_ms=start + offset * each, sustain_ms=each)
                            )
                    cursor = start + tuplet_total
                tuplet = []
            index += 1
            continue

        keys = _keys_of(note_text)
        if keys:
            events.append(NoteEvent(keys=keys, onset_ms=cursor, sustain_ms=duration))
            last_note_index = len(events) - 1
        elif note_text == "@":
            pass  # 休止符：只推进时间
        elif spl == "#" and last_note_index is not None:
            # 装饰音从前一个音里"借"时长
            events[last_note_index] = NoteEvent(
                keys=events[last_note_index].keys,
                onset_ms=events[last_note_index].onset_ms,
                sustain_ms=max(0.0, events[last_note_index].sustain_ms - duration),
            )
        cursor += duration
        index += 1

    instrument = str(data.get("instrument") or "")
    return Score(
        events=events,
        metadata={
            "name": str(data.get("name") or ""),
            "instrument": instrument,
            "author": str(data.get("author") or ""),
            "composer": str(data.get("composer") or ""),
            "arranger": str(data.get("arranger") or ""),
            "bpm": f"{bpm:g}",
            "source": "bgi-yuanqin",
        },
    )


def _keys_of(note_text: str) -> tuple[str, ...]:
    """`(ASD)` → `("A","S","D")`；`@` / `%` → 空。"""
    if note_text.startswith("(") and note_text.endswith(")"):
        return tuple(dict.fromkeys(note_text[1:-1]))
    if note_text in {"@", "%"}:
        return ()
    return (note_text,)


def parse_sheet(data: dict[str, Any]) -> Score:
    """BGI 曲谱 → `Score`（支持 `type: "midi"` 与 `type: "yuanqin"`）。

    `midi`：D/U 事件按 key 配对成音符；同一时刻按下的多个键合成一个和弦。
    `yuanqin`：按音符时值累加出 onset（见 `parse_yuanqin`）。
    """
    sheet_type = str(data.get("type") or "yuanqin").strip().lower()
    if sheet_type == "yuanqin":
        return parse_yuanqin(data)
    if sheet_type != "midi":
        raise BgiSheetError(
            f"暂只支持 type=midi / yuanqin 的 BetterGI 曲谱（当前 type={sheet_type!r}）"
        )
    notes_text_raw = data.get("notes")
    if not isinstance(notes_text_raw, str) or not notes_text_raw.strip():
        raise BgiSheetError("曲谱里没有 notes 文本")

    try:
        bpm = float(data.get("bpm") or BGI_BPM)
    except (TypeError, ValueError) as exc:
        raise BgiSheetError(f"bpm 无法解析: {data.get('bpm')!r}") from exc
    ticks = int(data.get("ticks") or BGI_TICKS)
    step = ms_per_tick(bpm, ticks)

    cursor_ms = 0.0
    pending: dict[str, float] = {}
    pairs: list[tuple[float, float, str]] = []  # (onset, hold, key)
    for raw in notes_text_raw.replace("\n", "").split("|"):
        token = raw.strip()
        if not token:
            continue
        if _BPM_MARK_RE.match(token):  # 变速标记：*120
            bpm = float(token[1:])
            step = ms_per_tick(bpm, ticks)
            continue
        match = _TOKEN_RE.match(token)
        if match is None:
            _log.warning("跳过无法解析的曲谱片段: %r", token)
            continue
        status, keys, wait_ticks = match.group(1), match.group(2), int(match.group(3))
        cursor_ms += wait_ticks * step  # 脚本先 sleep 再按键
        if keys == "@":
            continue
        for key in keys:
            if status == "D":
                pending[key] = cursor_ms
            else:
                onset = pending.pop(key, None)
                if onset is None:
                    continue
                pairs.append((onset, max(0.0, cursor_ms - onset), key))

    # 同一时刻按下的键合成和弦
    pairs.sort(key=lambda item: (item[0], item[2]))
    events: list[NoteEvent] = []
    current_onset: float | None = None
    current_keys: list[str] = []
    current_hold = 0.0
    for onset, hold, key in pairs:
        if current_onset is not None and abs(onset - current_onset) < 5.0:
            current_keys.append(key)
            current_hold = max(current_hold, hold)
            continue
        if current_onset is not None:
            events.append(
                NoteEvent(
                    keys=tuple(current_keys), onset_ms=current_onset, sustain_ms=current_hold
                )
            )
        current_onset, current_keys, current_hold = onset, [key], hold
    if current_onset is not None:
        events.append(
            NoteEvent(
                keys=tuple(current_keys), onset_ms=current_onset, sustain_ms=current_hold
            )
        )

    instrument = str(data.get("instrument") or "")
    return Score(
        events=events,
        metadata={
            "name": str(data.get("name") or ""),
            "instrument": instrument,
            "author": str(data.get("author") or ""),
            "source": "bgi",
        },
    )


def load_sheet(path: Path | str) -> Score:
    """读 BGI 曲谱文件。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BgiSheetError("曲谱文件顶层必须是对象")
    return parse_sheet(raw)
