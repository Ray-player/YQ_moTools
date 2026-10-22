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
from dataclasses import dataclass, field

from .timeline.assembler import AssembleConfig, assemble
from .types import InstrumentSpec, NoteEvent, PressEvent, Score

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
class Recording:
    """一次录制的不可变结果。"""

    score: Score
    notes: tuple[RecordedNote, ...]
    frames: int
    duration_ms: float
    fps: float

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def tick_text(self) -> str:
        return tick_text(self.notes)


def tick_text(notes: tuple[RecordedNote, ...] | list[RecordedNote]) -> str:
    """渲染成 `A1000F` 形式的文本：和弦字母连写，和弦之间写**毫秒**间隔。

    首和弦若带前导静默（`gap_ms > 0`，一般只出现在转换外部谱面时）也会写出数字，
    这样文本是时间轴的完整表示，还原时不会把开头的休止吃掉。
    """
    parts: list[str] = []
    for note in notes:
        if note.gap_ms > 0:
            parts.append(str(note.gap_ms))
        parts.append("".join(note.keys))
    return "".join(parts)


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
    """

    keys: tuple[str, ...]
    gap: int


@dataclass(frozen=True, slots=True)
class TickToken:
    """和弦在文本里的位置（试听高亮用）+ 解析结果。"""

    keys: tuple[str, ...]
    gap: int
    start: int
    end: int
    """`text[start:end]` 正好是该和弦的字母，不含后面的间隔数字。"""


def scan_tick_tokens(text: str) -> list[TickToken]:
    """扫描 tick 文本，同时给出解析结果与字符区间。

    语法（与写入端严格对称）：
    - 连续字母 = 一个和弦（首尾同键去重）；
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
            tokens.append(
                TickToken(keys=keys, gap=pending_gap, start=start, end=index)
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


def parse_tick_text(text: str) -> list[TickNote]:
    """`tick_text` 的逆运算：`A1000F` → `[A(gap 0), F(gap 1000)]`。"""
    return [TickNote(keys=t.keys, gap=t.gap) for t in scan_tick_tokens(text)]


def tick_spans(text: str) -> list[tuple[int, int]]:
    """每个和弦在文本里的 `(start, end)`，顺序与 `parse_tick_text` 一致。"""
    return [(t.start, t.end) for t in scan_tick_tokens(text)]


def tick_text_from_score(score: Score) -> str:
    """`Score` → 毫秒间隔 tick 文本（`onset_ms` 取整后做差分）。

    用于显示**非录制来源**的谱面（如读取 BetterGI 曲谱），以及把用户手打的
    文本/外部谱面统一成同一种落盘形式。差分基于**累计已输出毫秒**，
    因此整首谱的绝对时刻误差 ≤0.5ms，不会逐音累积。
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
        parts.append("".join(event.keys))
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

    # -- 收尾（主线程） ---------------------------------------------------- #

    def finish(self) -> Recording:
        """停止录制并产出结果；重复调用返回等价快照（不会重复累加）。"""
        self._closed = True
        score, notes = self._build_notes()
        return Recording(
            score=score,
            notes=notes,
            frames=len(self._frames),
            duration_ms=self.duration_ms,
            fps=self._fps,
        )

    @property
    def _fps(self) -> float:
        span = self.duration_ms / 1000.0
        if span <= 0 or len(self._frames) < 2:
            return 0.0
        return (len(self._frames) - 1) / span

    def _build_notes(self) -> tuple[Score, tuple[RecordedNote, ...]]:
        """装配谱面 + 毫秒间隔。

        `frames` 以本次录制的首帧为 0 点，事件 ts 同步平移 —— 因此 `onset_ms`
        从 0 开始；`gap_ms` 用**累计已输出毫秒**做差分（不是每个音各自取整），
        保证整首谱的时间轴误差 ≤0.5ms、不随音数漂移。
        """
        if not self._frames or not self._presses:
            return Score(), ()
        t0 = self._frames[0]
        frames_rel = [ts - t0 for ts in self._frames]
        presses = [
            PressEvent(key=e.key, down=e.down, ts=max(0.0, float(e.ts) - t0))
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
        return score, tuple(notes)
