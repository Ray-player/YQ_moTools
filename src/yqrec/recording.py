"""录制缓冲（M2）：判按事件流 → 谱面 + tick 文本。

线程约定（与 01 §4.5 一致）：

- `push_frame` / `push_events` **只在** `CaptureWorker` 线程里调用（调用方持 worker 锁）；
- `finish()` 由主线程在 worker 锁内调用，产出不可变的 `Recording` 快照，之后不再追加。

tick 文本以**毫秒间隔**为单位（`A1000F` = 按下 A，隔 1000ms 再按下 F）。

> M2f 变更：原来记"帧间隔"，帧→毫秒要靠采集 fps 换算，掉帧时会整体变速，
> 且量化到 16.7ms 网格有 ±8ms 误差。改成毫秒后误差 ≤0.5ms、与帧率无关，
> 保存 BGI 时也不用再假定 60fps。旧文件靠 `tick.unit` 区分（见 `scorefile`）。

与 `session.Recorder` 的区别：`Recorder` 自己跑 `PressDetector`（喂 RGB）；这里直接吃
`PressDetector.update()` 已经产出的事件 —— GUI 侧判按器只有一份，不能重复喂。
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field

from .timeline.assembler import AssembleConfig, assemble
from .types import (
    SUSTAIN_TEXT_MIN_MS,
    InstrumentSpec,
    NoteEvent,
    PressEvent,
    Score,
)

_EPS = 1e-6
"""帧时间戳比较容差（秒）：事件 ts 与帧 ts 同源，只需挡住浮点抖动。"""


@dataclass(frozen=True, slots=True)
class RecordedNote:
    """录制出的一个和弦（毫秒间隔是 tick 文本的显示单位）。"""

    keys: tuple[str, ...]
    onset_ms: float
    onset_frame: int
    gap_ms: int
    """与上一个和弦的毫秒间隔；首个和弦为 0。"""
    sustain_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class KeySpan:
    """一个键的**一次按下区间**（毫秒，相对录制首帧）。

    只活在内存里的 `Recording` 上（**不落盘**）：试听刚录的谱面时用它把键位状态灯
    按录制实况点亮 —— 什么时候按下、什么时候弹起、按下那一刻的 Δ。
    谱面文本/JSON 里没有这份数据，所以读取来的文件无法回放键位。
    """

    key: str
    down_ms: float
    up_ms: float
    delta: float = 0.0
    """按下时的判据量 Δ（0 = 老数据/没有判据值）。"""

    @property
    def hold_ms(self) -> float:
        return max(0.0, self.up_ms - self.down_ms)


@dataclass(frozen=True, slots=True)
class Recording:
    """一次录制的不可变结果。"""

    score: Score
    notes: tuple[RecordedNote, ...]
    frames: int
    duration_ms: float
    fps: float
    spans: tuple[KeySpan, ...] = ()
    """逐键按下区间（按 `down_ms` 升序）。给试听回放用；空 = 没有回放数据。"""

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def tick_text(self) -> str:
        return tick_text(self.notes)

    @property
    def press_deltas(self) -> tuple[float, ...]:
        """所有"有判据值"的按下（逐键，不按和弦合并）。"""
        return tuple(span.delta for span in self.spans if span.delta > 0)

    @property
    def avg_delta(self) -> float:
        """识别的平均 Δ（无数据 → 0.0）。"""
        values = self.press_deltas
        return sum(values) / len(values) if values else 0.0

    @property
    def min_delta(self) -> float:
        """识别的最小 Δ（无数据 → 0.0；最接近阈值的那一次，用来判断余量）。"""
        values = self.press_deltas
        return min(values) if values else 0.0


def tick_text(notes: tuple[RecordedNote, ...] | list[RecordedNote]) -> str:
    """渲染成 `A1000F` 形式的文本：和弦字母连写，和弦之间写**毫秒**间隔。

    延音写成和弦后缀 `[ms]`（如 `A[800]1000F` = A 按住 800ms，之后 1000ms 再按 F）；
    小于 `SUSTAIN_TEXT_MIN_MS`(50ms) 的延音不写（与 01 §7.1 对齐）。

    首和弦若带前导静默（`gap_ms > 0`，一般只出现在转换外部谱面时）也会写出数字，
    这样文本是时间轴的完整表示，还原时不会把开头的休止吃掉。
    """
    parts: list[str] = []
    for note in notes:
        if note.gap_ms > 0:
            parts.append(str(note.gap_ms))
        parts.append(_render_chord(note.keys, note.sustain_ms))
    return "".join(parts)


def _render_chord(
    keys: tuple[str, ...], sustain_ms: float, *, sustained: bool = True
) -> str:
    """`("A",) + 800` → `"A[800]"`；延音不到 50ms 时只有字母。

    `sustained=False` 用于**不支持延音的乐器**（风物之诗琴这类）：不管谱面里
    记了多长的按住时间，渲染出来都只有字母 —— 那些 `[ms]` 在游戏里没有意义，
    留在编辑框里只会让谱面变成一串 `A[500]120B` 的噪音。
    """
    token = "".join(keys)
    if not sustained:
        return token
    value = int(round(float(sustain_ms)))
    if value >= SUSTAIN_TEXT_MIN_MS:
        token += f"[{value}]"
    return token


def _frame_index(frames: list[float], ts: float) -> int:
    """按时间戳找所在帧序号（找不到取最近的前一帧）。"""
    if not frames:
        return 0
    index = bisect.bisect_left(frames, ts - _EPS)
    return min(max(index, 0), len(frames) - 1)


@dataclass(frozen=True, slots=True)
class TickNote:
    """从 tick 文本解析出的一个和弦。

    `gap` 的单位由调用方决定：新录制是**毫秒**（`tick.unit == "ms"`），
    旧文件是帧数（`unit == "frame"`，需按采集 fps 换算）。
    `sustain_ms` 是紧跟和弦的 `[ms]` 后缀（0 = 没写延音）。
    """

    keys: tuple[str, ...]
    gap: int
    sustain_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class TickToken:
    """和弦在文本里的位置（试听高亮用）+ 解析结果。"""

    keys: tuple[str, ...]
    gap: int
    start: int
    end: int
    """`text[start:end]` 正好是该和弦的字母，不含后面的间隔数字。"""
    sustain_ms: float = 0.0
    """和弦后缀 `[ms]` 的值；`end` 仍只覆盖字母（高亮/诊断不受后缀影响）。"""


def scan_tick_tokens(text: str) -> list[TickToken]:
    """扫描 tick 文本，同时给出解析结果与字符区间。

    语法（与写入端严格对称）：
    - 连续字母 = 一个和弦（首尾同键去重）；
    - 和弦后可跟 `[ms]` = 该和弦的**延音**（毫秒，`A[800]`）；方括号内非数字时忽略；
    - 连续数字 = 与**前一个**和弦的间隔（默认毫秒）；
    - 空白、其它字符忽略（容错，不清空已解析结果）。

    首和弦前的数字 = 前导静默（保留，用于还原外部谱面的开头休止）。
    """
    tokens: list[TickToken] = []
    pending_gap = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isalpha():
            start = index
            while index < length and text[index].isalpha():
                index += 1
            keys = tuple(dict.fromkeys(text[start:index].upper()))
            end = index
            sustain, index = _read_sustain(text, index)
            tokens.append(
                TickToken(
                    keys=keys,
                    gap=pending_gap,
                    start=start,
                    end=end,
                    sustain_ms=sustain,
                )
            )
            pending_gap = 0
        elif char.isdigit():
            start = index
            while index < length and text[index].isdigit():
                index += 1
            pending_gap = int(text[start:index])
        else:
            index += 1
    return tokens


def _read_sustain(text: str, index: int) -> tuple[float, int]:
    """从 `index` 起尝试读 `[ms]`，返回 `(延音毫秒, 新的 index)`。

    容错三条：
    - 允许字母与 `[` 之间有空白；
    - 方括号里不是正整数 → 延音按 0，但**整段吃掉**（否则里面的字母会被当成和弦，
      例如 `A[abc]F` 会多出一个 ABC 和弦）；
    - 没有闭合的 `]` → 原样返回，让外层按普通字符处理（不吞后面的内容）。
    """
    length = len(text)
    probe = index
    while probe < length and text[probe].isspace():
        probe += 1
    if probe >= length or text[probe] != "[":
        return 0.0, index
    close = text.find("]", probe + 1)
    if close < 0:
        return 0.0, index
    raw = text[probe + 1 : close].strip()
    if not raw.isdigit():
        return 0.0, close + 1
    return float(int(raw)), close + 1


def parse_tick_text(text: str) -> list[TickNote]:
    """`tick_text` 的逆运算：`A[800]1000F` → `[A(gap 0, sustain 800), F(gap 1000)]`。"""
    return [
        TickNote(keys=t.keys, gap=t.gap, sustain_ms=t.sustain_ms) for t in scan_tick_tokens(text)
    ]


def tick_spans(text: str) -> list[tuple[int, int]]:
    """每个和弦在文本里的 `(start, end)`，顺序与 `parse_tick_text` 一致。"""
    return [(t.start, t.end) for t in scan_tick_tokens(text)]


def tick_text_from_score(score: Score, *, sustained: bool = True) -> str:
    """`Score` → 毫秒间隔 tick 文本（`onset_ms` 取整后做差分）。

    用于显示**非录制来源**的谱面（如读取 BetterGI 曲谱），以及把用户手打的
    文本/外部谱面统一成同一种落盘形式。差分基于**累计已输出毫秒**，
    因此整首谱的绝对时刻误差 ≤0.5ms，不会逐音累积。

    `sustained=False`：目标乐器**不支持延音**（`sustain_ms == 0`，如风物之诗琴）时
    不写 `[ms]` 后缀。外部谱（midi / yuanqin）里每个音的按住时长对我们没有意义，
    照搬会让编辑框里全是 `A[500]120B` —— 见 M6d 用户反馈。
    """
    notes = [e for e in score.events if isinstance(e, NoteEvent)]
    if not notes:
        return ""
    parts: list[str] = []
    emitted = 0
    for event in notes:
        target = max(0, int(round(float(event.onset_ms))))
        gap = max(0, target - emitted)  # 首音也写：保留开头的休止
        if gap:
            parts.append(str(gap))
        parts.append(_render_chord(tuple(event.keys), event.sustain_ms, sustained=sustained))
        emitted += gap
    return "".join(parts)


@dataclass
class RecordingBuffer:
    """把一帧一帧的判按事件攒成谱面（纯逻辑，无 Qt / 无 numpy）。"""

    spec: InstrumentSpec
    chord_window_ms: float = 25.0
    _frames: list[float] = field(default_factory=list)
    _presses: list[PressEvent] = field(default_factory=list)
    _closed: bool = False

    # -- 采集（worker 线程） ---------------------------------------------- #

    def push_frame(self, ts: float) -> None:
        """登记一帧的采集时刻（同一帧只登记一次）。"""
        if self._closed:
            return
        self._frames.append(float(ts))

    def push_events(self, events: list[PressEvent] | tuple[PressEvent, ...]) -> None:
        """追加该帧判出的事件（按下/抬起都留，供配对计算时长）。"""
        if self._closed or not events:
            return
        self._presses.extend(events)

    def close(self) -> None:
        """封口：之后 `push_*` 一律丢弃（`stop_recording` 在锁内先调它，避免收尾竞态）。"""
        self._closed = True

    # -- 只读快照 ---------------------------------------------------------- #

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def is_empty(self) -> bool:
        return not self._presses

    def snapshot(self) -> dict:
        """实时预览用的轻量快照（录制中每来一个事件刷一次）。"""
        notes = self._build_notes()[1]
        return {
            "text": tick_text(notes),
            "notes": len(notes),
            "frames": len(self._frames),
            "duration_ms": self.duration_ms,
        }

    @property
    def duration_ms(self) -> float:
        if len(self._frames) < 2:
            return 0.0
        return max(0.0, self._frames[-1] - self._frames[0]) * 1000.0

    @property
    def last_frame_ts(self) -> float | None:
        """最后一帧的时间戳（秒，绝对）；没有帧时 `None`。

        停止录制时用它给"还按着的键"补抬：延音要算到**停止这一刻**，
        而不是最后一个事件所在的那一帧（否则最后按住的音会被截短）。
        """
        return self._frames[-1] if self._frames else None

    # -- 收尾（主线程） ---------------------------------------------------- #

    def finish(self, extra: Sequence[PressEvent] = ()) -> Recording:
        """停止录制并产出结果；重复调用返回等价快照（不会重复累加）。

        `extra` 是收尾补发的事件（`CaptureWorker.stop_recording` 用 `flush()`
        给"停止时还按着"的键补抬起）。它**绕过 `closed` 闸门**，因为它属于
        收尾本身而不是新采集 —— 这样延音能算到停止那一刻，且不会与仍在跑的
        分析线程抢同一个缓冲（缓冲已先封口）。
        """
        self._closed = True
        if extra:
            self._presses.extend(extra)
        score, notes, spans = self._build_notes()
        return Recording(
            score=score,
            notes=notes,
            frames=len(self._frames),
            duration_ms=self.duration_ms,
            fps=self._fps,
            spans=spans,
        )

    @property
    def _fps(self) -> float:
        span = self.duration_ms / 1000.0
        if span <= 0 or len(self._frames) < 2:
            return 0.0
        return (len(self._frames) - 1) / span

    def _build_notes(self) -> tuple[Score, tuple[RecordedNote, ...], tuple[KeySpan, ...]]:
        """装配谱面 + 毫秒间隔 + 逐键按下区间。

        `frames` 以本次录制的首帧为 0 点，事件 ts 同步平移 —— 因此 `onset_ms`
        从 0 开始；`gap_ms` 用**累计已输出毫秒**做差分（不是每个音各自取整），
        保证整首谱的时间轴误差 ≤0.5ms、不随音数漂移。
        """
        if not self._frames or not self._presses:
            return Score(), (), ()
        t0 = self._frames[0]
        frames_rel = [ts - t0 for ts in self._frames]
        presses = [
            PressEvent(key=e.key, down=e.down, ts=max(0.0, float(e.ts) - t0), delta=float(e.delta))
            for e in self._presses
        ]
        cfg = AssembleConfig(chord_window_ms=self.chord_window_ms)
        events = assemble(presses, self.spec, cfg)
        score = Score(
            events=list(events),
            metadata={"instrument": self.spec.name, "instrument_id": self.spec.id},
        )

        notes: list[RecordedNote] = []
        emitted_ms = 0
        for event in events:
            if not isinstance(event, NoteEvent):
                continue
            target = max(0, int(round(float(event.onset_ms))))
            gap = max(0, target - emitted_ms)
            emitted_ms += gap
            notes.append(
                RecordedNote(
                    keys=tuple(event.keys),
                    onset_ms=float(event.onset_ms),
                    onset_frame=_frame_index(frames_rel, event.onset_ms / 1000.0),
                    gap_ms=gap,
                    sustain_ms=float(event.sustain_ms),
                )
            )
        return score, tuple(notes), key_spans(presses, last_ts=frames_rel[-1])


def key_spans(presses: Sequence[PressEvent], *, last_ts: float | None = None) -> tuple[KeySpan, ...]:
    """按下/抬起配对成 `KeySpan`（同一套"先按下先配对"规则，与 `assembler._pair_notes` 一致）。

    - 只有按下没有抬起的键（录制被强行切断）：按 `last_ts`（一般传最后一帧时刻）闭合；
      不传就退回最后一个事件时刻（hold 可能是 0，但仍会显示一次点亮）；
    - 入参 `presses` 的 ts 必须已经是"相对录制首帧"的秒。
    """
    if not presses:
        return ()
    ordered = sorted(presses, key=lambda e: e.ts)
    closing_ts = max(ordered[-1].ts, float(last_ts or 0.0))
    pending: dict[str, list[PressEvent]] = {}
    spans: list[KeySpan] = []
    for event in ordered:
        if event.down:
            pending.setdefault(event.key, []).append(event)
            continue
        stack = pending.get(event.key)
        if not stack:
            continue
        down = stack.pop(0)
        spans.append(
            KeySpan(
                key=event.key,
                down_ms=down.ts * 1000.0,
                up_ms=max(down.ts, event.ts) * 1000.0,
                delta=float(down.delta),
            )
        )
    for key, stack in pending.items():
        for down in stack:
            spans.append(
                KeySpan(
                    key=key,
                    down_ms=down.ts * 1000.0,
                    up_ms=max(down.ts, closing_ts) * 1000.0,
                    delta=float(down.delta),
                )
            )
    spans.sort(key=lambda s: (s.down_ms, s.key))
    return tuple(spans)
