"""按键演奏（M5）：谱面 → 按键 down/up 事件表 → 按绝对时间表注入。

与 `engine.Player` 的关系：**接口一致**（`play`/`pause`/`stop`/`state`/`speed` +
`on_note`/`on_state`/`on_progress`），区别只在"输出去哪" —— `Player` 把音符丢给音频引擎，
`KeyPlayer` 把字母键的按下/抬起交给注入通道（`inputsim.ForegroundKeys` /
`BackgroundKeys`），于是听到的是**游戏自己弹出来的声音**。

时间轴由 `build_key_timeline()` 从 `Score` 展开：

- 每个和弦的每个键 → 一对 down/up，按住时长 = `max(sustain_ms, min_hold_ms)`。
  无延音的音也必须按住一小会儿（默认 60ms）：0ms 的按下在 60fps 的游戏里可能整帧跳过。
- 同一个键的相邻两段**不许交叠**：交叠时把前一段的抬起提前到后一段按下之前
  （留 `RETRIGGER_GAP_MS`）。注入层会忽略"已经按着的键"的重复 down，
  不提前抬起的后果是重复音发不出声、键还被一直按住（BetterGI 踩过同一个坑）。
- 在同一时刻既有抬起又有按下时，**抬起排前面**（稳定排序 + `down` 作次序键）。

暂停/停止/中断一律 `release_all()`：游戏不会因为"我们程序不发了"而松开按键，
漏发抬起会把琴键一直按死在游戏里。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logging import get_logger
from ..types import NoteEvent, Score
from .engine import PlayerState

_log = get_logger("player.keys")

DEFAULT_KEY_HOLD_MS = 60.0
"""无延音音符的按住时长（毫秒）：游戏按帧采样，太短的按下会被整帧跳过。"""

RETRIGGER_GAP_MS = 30.0
"""同一键"交叠重按"时提前抬起多少毫秒（抄 BetterGI 的 RetriggerGap）。"""


@dataclass(frozen=True, slots=True)
class KeyAction:
    """时间轴上的一个按键动作。"""

    at_ms: float
    """相对曲首的绝对时刻（毫秒）。"""

    down: bool
    """`True` = 按下，`False` = 抬起。"""

    key: str
    """琴键（A-Z 单个大写字母）。"""

    event_index: int
    """所属 `Score.events` 下标（播放回调换算和弦序号用）。"""


@dataclass(frozen=True, slots=True)
class KeyTimeline:
    actions: tuple[KeyAction, ...]
    duration_ms: float
    note_count: int

    @property
    def key_count(self) -> int:
        return len({action.key for action in self.actions})


def build_key_timeline(
    score: Score, *, min_hold_ms: float = DEFAULT_KEY_HOLD_MS
) -> KeyTimeline:
    """把 `Score` 展开成按键事件表（已排序、同键不交叠）。

    纯函数，不碰任何系统调用 —— 单测直接验它。
    """
    notes = [(index, event) for index, event in enumerate(score.events) if isinstance(event, NoteEvent)]
    hold = max(0.0, float(min_hold_ms))

    # 先把每个键的区间收齐（同键必须串行处理，见模块注释）
    spans: dict[str, list[list[float]]] = {}
    for index, event in notes:
        length = max(float(event.sustain_ms), hold)
        for key in event.keys:
            spans.setdefault(key, []).append(
                [float(event.onset_ms), float(event.onset_ms) + length, index]
            )

    actions: list[KeyAction] = []
    for key, items in spans.items():
        items.sort(key=lambda item: item[0])
        for position, item in enumerate(items):
            start, end, event_index = item
            if position + 1 < len(items):
                next_start = items[position + 1][0]
                if end > next_start:
                    # 真正**交叠**才让位（刚好相接不算）：前一段先松手，否则重按会被注入层吞掉。
                    # 不能写成 `end > next_start - GAP`：那会把"延音正好接到下一音"也白砍 30ms。
                    end = max(start, next_start - RETRIGGER_GAP_MS)
            actions.append(KeyAction(at_ms=start, down=True, key=key, event_index=int(event_index)))
            actions.append(KeyAction(at_ms=end, down=False, key=key, event_index=int(event_index)))

    # 稳定排序：同一时刻抬起排前面（`False < True`），但**不按类型整表重排**
    actions.sort(key=lambda action: (action.at_ms, action.down))
    duration = max((action.at_ms for action in actions), default=0.0)
    return KeyTimeline(actions=tuple(actions), duration_ms=duration, note_count=len(notes))


class KeyPlayer:
    """把 `KeyTimeline` 按绝对时间表打到键盘上（后台线程，抗漂移）。

    - `sink` 需实现 `key_down(key)` / `key_up(key)` / `release_all()` / `ready()`；
    - `on_note(event, index)` 在每次**按下**时回调（编辑器高亮用，与 `Player` 同语义）；
    - `on_interrupt(reason)` 在"目标窗口不可注入"时回调一次，随后自动停止。
    """

    def __init__(
        self,
        score: Score,
        sink,
        *,
        speed: float = 1.0,
        min_hold_ms: float = DEFAULT_KEY_HOLD_MS,
    ) -> None:
        self._score = score
        self._sink = sink
        self._timeline = build_key_timeline(score, min_hold_ms=min_hold_ms)
        self._speed = max(0.1, float(speed))
        self._state = PlayerState.IDLE
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._last_at_ms = 0.0
        self.on_note: Callable[[NoteEvent, int], None] | None = None
        self.on_state: Callable[[PlayerState], None] | None = None
        self.on_progress: Callable[[float], None] | None = None
        self.on_interrupt: Callable[[str], None] | None = None

    # -- 属性 -------------------------------------------------------------- #

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def duration_ms(self) -> float:
        return self._timeline.duration_ms

    @property
    def timeline(self) -> KeyTimeline:
        return self._timeline

    def keys_used(self) -> tuple[str, ...]:
        """这条时间轴用到的键（收尾时逐个补发抬起）。"""
        return tuple(sorted({action.key for action in self._timeline.actions}))

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.1, float(speed))

    # -- 控制 -------------------------------------------------------------- #

    def play(self, start_ms: float = 0.0, *, loop: bool = False) -> None:
        if self._state is PlayerState.PLAYING:
            return
        if self._state is PlayerState.PAUSED:
            self._pause.clear()
            self._set_state(PlayerState.PLAYING)
            return
        self._stop.clear()
        self._pause.clear()
        self._set_state(PlayerState.PLAYING)
        self._thread = threading.Thread(
            target=self._run, args=(start_ms, loop), name="yqrec-keyplayer", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        """暂停：立刻松开所有键（游戏里的按键不会自己弹起）。"""
        if self._state is not PlayerState.PLAYING:
            return
        self._pause.set()
        self._sink.release_all()
        self._set_state(PlayerState.PAUSED)

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        self._sink.release_all()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        self._set_state(PlayerState.IDLE)

    def close(self) -> None:
        self.stop()

    # -- 内部 -------------------------------------------------------------- #

    def _set_state(self, state: PlayerState) -> None:
        self._state = state
        if self.on_state:
            self.on_state(state)

    def _interrupt(self, reason: str) -> None:
        """目标窗口不能再注入：停表并通知 UI（UI 会退出演奏模式）。"""
        _log.warning("演奏中断: %s", reason)
        self._stop.set()
        if self.on_interrupt:
            try:
                self.on_interrupt(reason)
            except Exception:  # 回调只是提示，绝不能反过来打断线程收尾
                pass

    def _held_at(self, at_ms: float, *, strict: bool = False) -> tuple[str, ...]:
        """`at_ms` 时刻仍被按住的键（起播/暂停恢复时补按，否则延音段中间起播不响）。

        `strict=True` 只算**严格早于** `at_ms` 的动作 —— 起播时必须这样：
        与起播同刻的那个 down 由主循环自己发，补按一次就会变成重复按下。
        暂停恢复反过来要走 `strict=False`：`at_ms` 就是刚刚执行完的那个动作。
        """
        state: dict[str, bool] = {}
        for action in self._timeline.actions:
            if action.at_ms > at_ms or (strict and action.at_ms == at_ms):
                break
            state[action.key] = action.down
        return tuple(key for key, down in state.items() if down)

    def _restore_held(self, at_ms: float, *, strict: bool = False) -> None:
        for key in self._held_at(at_ms, strict=strict):
            self._sink.key_down(key)

    def _run(self, start_ms: float, loop: bool) -> None:
        actions = self._timeline.actions
        try:
            while True:
                t0 = time.perf_counter()
                self._restore_held(start_ms, strict=True)
                for action in actions:
                    if self._stop.is_set():
                        return
                    if action.at_ms < start_ms:
                        continue
                    target = t0 + (action.at_ms - start_ms) / 1000.0 / self._speed
                    while time.perf_counter() < target:
                        if self._stop.is_set():
                            return
                        if self._pause.is_set():
                            pause_start = time.perf_counter()
                            self._sink.release_all()
                            while self._pause.is_set() and not self._stop.is_set():
                                time.sleep(0.01)
                            # 暂停期间时间轴冻结（按暂停时长顺延），恢复后补按"此刻该按着的键"
                            t0 += time.perf_counter() - pause_start
                            self._restore_held(self._last_at_ms)
                        time.sleep(min(0.005, max(0.0, target - time.perf_counter())))
                    if not self._sink.ready():
                        self._interrupt("目标窗口不在前台（或已关闭）—— 演奏已停止")
                        return
                    if action.down:
                        self._sink.key_down(action.key)
                    else:
                        self._sink.key_up(action.key)
                    self._last_at_ms = action.at_ms
                    if action.down and self.on_note:
                        self.on_note(self._score.events[action.event_index], action.event_index)
                    if self.on_progress:
                        self.on_progress(action.at_ms)
                if not loop:
                    break
                start_ms = 0.0
        finally:
            self._sink.release_all()
            # 收尾再对**用到的每个键**补发一次抬起：真实游戏里漏一个 up 就是琴键卡死，
            # 而多余的 up 完全无害。注入通道没实现这个方法时跳过。
            force = getattr(self._sink, "force_release", None)
            if force is not None:
                try:
                    force(self.keys_used())
                except Exception as exc:  # 收尾动作失败不该掩盖真正的中断原因
                    _log.debug("补发抬起失败（已忽略）: %s", exc)
            self._set_state(PlayerState.IDLE)
            self._thread = None
