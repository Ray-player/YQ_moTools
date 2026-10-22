"""播放调度（03 §3.11）：绝对时间表 + 抗漂移。

时间语义：`onset_ms` 相对曲首；播放时 `target = t0 + (onset - start_ms) / speed`。
音频与 UI 高亮共用同一条时间轴。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum

from ..logging import get_logger
from ..types import NoteEvent, Score

_log = get_logger("player.engine")


class PlayerState(StrEnum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


class Player:
    """极简播放器：后台线程按绝对时间表发事件。

    - `audio` 只需实现 `play(note)` / `stop_all()`，可为 None；
    - `on_note(event, index)` 用于编辑器高亮；
    - `speed` 只缩放时间，不改采样播放速率（音高不变）。
    """

    def __init__(
        self,
        score: Score,
        *,
        audio=None,
        speed: float = 1.0,
        note_of_key: Callable[[str], str] | None = None,
    ) -> None:
        self._score = score
        self._audio = audio
        self._note_of_key = note_of_key
        self._speed = max(0.1, float(speed))
        self._state = PlayerState.IDLE
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self.on_note: Callable[[NoteEvent, int], None] | None = None
        self.on_state: Callable[[PlayerState], None] | None = None
        self.on_progress: Callable[[float], None] | None = None

    # -- 属性 -------------------------------------------------------------- #

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def speed(self) -> float:
        return self._speed

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
            target=self._run, args=(start_ms, loop), name="yqrec-player", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        if self._state is PlayerState.PLAYING:
            self._pause.set()
            if self._audio:
                self._audio.stop_all()
            self._set_state(PlayerState.PAUSED)

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        if self._audio:
            self._audio.stop_all()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        self._set_state(PlayerState.IDLE)

    def seek(self, ms: float) -> None:
        """停止并从 ms 处重播。"""
        was_playing = self._state is PlayerState.PLAYING
        self.stop()
        if was_playing:
            self.play(start_ms=max(0.0, ms))

    def close(self) -> None:
        self.stop()
        if self._audio:
            self._audio.close()

    # -- 内部 -------------------------------------------------------------- #

    def _set_state(self, st: PlayerState) -> None:
        self._state = st
        if self.on_state:
            self.on_state(st)

    def _run(self, start_ms: float, loop: bool) -> None:
        notes = [(i, e) for i, e in enumerate(self._score.events) if isinstance(e, NoteEvent)]
        try:
            while True:
                t0 = time.perf_counter()
                for idx, ev in notes:
                    if self._stop.is_set():
                        return
                    if ev.onset_ms < start_ms:
                        # 从中间开始（试听里「从当前高亮继续」）：已经过去的音直接跳过。
                        # 不跳的话 target 在过去时间 → 这一批音会**瞬间全部放出**（爆音）。
                        continue
                    target = t0 + (ev.onset_ms - start_ms) / 1000.0 / self._speed
                    while time.perf_counter() < target:
                        if self._stop.is_set():
                            return
                        if self._pause.is_set():
                            # 暂停期间冻结时间轴（按暂停时长顺延）
                            pause_start = time.perf_counter()
                            while self._pause.is_set() and not self._stop.is_set():
                                time.sleep(0.01)
                            t0 += time.perf_counter() - pause_start
                        time.sleep(min(0.005, max(0.0, target - time.perf_counter())))
                    if self._audio:
                        for key in ev.keys:
                            note = self._note_of_key(key) if self._note_of_key else key
                            try:
                                self._audio.play(note)
                            except Exception:
                                pass
                    if self.on_note:
                        self.on_note(ev, idx)
                    if self.on_progress:
                        self.on_progress(ev.onset_ms)
                if not loop:
                    break
                start_ms = 0.0
        finally:
            self._set_state(PlayerState.IDLE)
            self._thread = None
