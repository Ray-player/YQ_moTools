"""谱面试听（M2b）：`Player` + `AudioEngine` 的 GUI 桥。

为什么要这一层：
- `Player` 在后台线程跑，`on_note/on_state` 回调都在播放线程里；GUI 必须经 Qt Signal
  转回主线程，否则会跨线程改控件。
- `AudioEngine.preload()` 要读 21 个 mp3（几十毫秒到几百毫秒），**延迟到第一次播放**才做，
  且同乐器只做一次（换乐器才重建）。

对外：
```python
ctl = PlaybackController(parent)          # audio_enabled=False 用于无音频环境/测试
ctl.load(score, spec)
ctl.note_played.connect(on_note)          # int: 第几个和弦（与 tick 文本 token 同序）
ctl.state_changed.connect(on_state)       # "playing" / "idle"
ctl.play()                  # = play_from(0)
ctl.play_from(ordinal)      # 从第 N 个和弦开始（试听里「从当前高亮继续」）
ctl.pause() / ctl.resume()  # 暂停 / 从暂停处继续（内部走 Player 的 pause 语义）
ctl.stop() / ctl.close()

# 进度（M2q.10）：UI 每 40ms 读一次，播放头由这里算，UI 只管画
ctl.position_ms             # 播放头绝对时刻；播放中随时间前进，暂停时冻结
ctl.duration_ms             # 全曲总长（末音 onset + 延音）
ctl.ordinal_at(ms)          # 毫秒 → 和弦下标（拖进度条跳转用）
```

**两种输出（M5）**：`load(..., mode="audio")` 走 `Player` + `AudioEngine`（软件内发声）；
`load(..., mode="keys", sink=…)` 走 `KeyPlayer` + 键鼠注入（让游戏自己发声）。
两者对外接口完全一致，所以主窗口的进度条/暂停/快进/拖动逻辑一套代码复用于两种模式。

**为什么播放头在控制器里算**：`Player` 在后台线程按绝对时间表走，主线程没有"现在几毫秒"
这个量；若 UI 自己记锚点，`pause → resume`（引擎内部按暂停时长顺延 `t0`）与 `play_from`
（重建播放器）两处都要跟着重算，容易偏。锚点只在控制器维护一份。
"""

from __future__ import annotations

import bisect
import time

from PySide6.QtCore import QObject, Signal

from ..instruments import assets_dir
from ..logging import get_logger
from ..player.audio import AudioEngine
from ..player.engine import Player, PlayerState
from ..player.keys import KeyPlayer
from ..types import InstrumentSpec, NoteEvent, Score

_log = get_logger("ui.playback")

MODE_AUDIO = "audio"
"""播放到软件内的采样引擎（试听）。"""

MODE_KEYS = "keys"
"""播放成键盘注入（演奏）：游戏自己发声。"""


class PlaybackController(QObject):
    """把 `Score` 播出来，并把进度事件转成 Qt 信号。"""

    note_played = Signal(int)
    """第 `index` 个和弦已播放（索引与 tick 文本 token 顺序一致）。"""

    state_changed = Signal(str)
    """`"playing"` / `"idle"`（播完或手动停止都回 idle）。"""

    interrupted = Signal(str)
    """演奏被迫中断（目标窗口跑焦点 / 被关掉）。参数是给人看的一句话。"""

    def __init__(self, parent: QObject | None = None, *, audio_enabled: bool = True) -> None:
        super().__init__(parent)
        self._audio_enabled = audio_enabled
        self._score: Score | None = None
        self._spec: InstrumentSpec | None = None
        self._mode = MODE_AUDIO
        self._sink = None
        """按键演奏的输出通道（`inputsim.ForegroundKeys` / `BackgroundKeys`）；
        音频模式恒为 None。"""
        self._audio: AudioEngine | None = None
        self._audio_spec_id: str | None = None
        self._player: Player | KeyPlayer | None = None
        self._suppress_state = False
        """换播放器期间屏蔽状态转发。

        `Player.stop()` 内部会 `_set_state(IDLE)` → 走 `on_state` → 转发成 `idle`。
        换播放器（`play_from`）时这个 idle 是**噪音**：UI 收到会以为"播完了"而退出试听模式，
        紧接着的 `playing` 又被它忽略掉 —— 表现就是"点一下播放键退出试听模式"。
        """
        self._ordinal_of_event: dict[int, int] = {}
        """`Score.events` 下标 → 第几个和弦。

        `Player` 回调给的是 **events 下标**（含休止），而 tick 文本 token 只数和弦，
        这里做一次换算，UI 才能直接按 token 下标高亮。
        """
        self._anchor_ms = 0.0
        """起播时刻（绝对 ms，相对曲首）。"""
        self._anchor_at = 0.0
        """起播那一刻的 `perf_counter()`；播放头 = `_anchor_ms + (now - _anchor_at) * 1000`。"""
        self._frozen_ms: float | None = None
        """暂停时冻结的播放头（`None` = 没暂停）。暂停期间不能用公式算 —— 引擎的 `t0`
        要等 resume 时才顺延，直接用 `perf_counter()` 会让进度条一直往前爬。"""

    # -- 状态 -------------------------------------------------------------- #

    @property
    def playing(self) -> bool:
        return self._player is not None and self._player.state is PlayerState.PLAYING

    @property
    def paused(self) -> bool:
        return self._player is not None and self._player.state is PlayerState.PAUSED

    @property
    def state(self) -> str:
        """`"playing"` / `"paused"` / `"idle"`（无播放器也算 idle）。"""
        if self._player is None:
            return str(PlayerState.IDLE)
        return str(self._player.state)

    def notes(self) -> list[NoteEvent]:
        """按顺序取出和弦（下标即 tick token 序号）。"""
        if self._score is None:
            return []
        return [e for e in self._score.events if isinstance(e, NoteEvent)]

    def onset_of(self, ordinal: int) -> float:
        """第 `ordinal` 个和弦的绝对时刻（ms）；越界夹到首/尾。"""
        notes = self.notes()
        if not notes:
            return 0.0
        return float(notes[max(0, min(int(ordinal), len(notes) - 1))].onset_ms)

    @property
    def ready(self) -> bool:
        return self._score is not None and self._spec is not None and bool(self._score.events)

    # -- 进度（M2q.10） ------------------------------------------------------ #

    @property
    def duration_ms(self) -> float:
        """全曲总长：末音 `onset + sustain`（末音是延音时进度条不会提前满格）。

        按键演奏还多一层"最短按住"：谱面末音没有延音时，实际要按到
        `onset + min_hold`，所以优先取 `KeyPlayer` 算好的时间轴总长。

        至少 1.0，调用方做除法不用再判零。
        """
        player = self._player
        if isinstance(player, KeyPlayer):
            return max(1.0, float(player.duration_ms))
        notes = self.notes()
        if not notes:
            return 0.0
        last = notes[-1]
        return max(1.0, float(last.onset_ms) + max(0.0, float(last.sustain_ms)))

    @property
    def position_ms(self) -> float:
        """播放头绝对时刻（ms）。播放中随时间前进；暂停冻结；停止后回起播点。"""
        if self._frozen_ms is not None:
            return self._frozen_ms
        if not self.playing:
            return self._anchor_ms
        speed = self._player.speed if self._player is not None else 1.0
        elapsed = max(0.0, time.perf_counter() - self._anchor_at)
        return min(self._anchor_ms + elapsed * 1000.0 * speed, self.duration_ms)

    def ordinal_at(self, ms: float) -> int:
        """毫秒 → 和弦下标（"最后一个已经开始的音"）；越界夹到首/尾。

        `+0.5ms` 是给浮点误差留的余量：拖到正好 `onset` 上时要落在**这个**音，不是前一个。
        """
        notes = self.notes()
        if not notes:
            return 0
        onsets = [float(n.onset_ms) for n in notes]
        index = bisect.bisect_right(onsets, float(ms) + 0.5) - 1
        return max(0, min(index, len(notes) - 1))

    def _anchor(self, ms: float) -> None:
        """把播放头锚到 `ms`（起播/seek 后调用）。"""
        self._anchor_ms = max(0.0, float(ms))
        self._anchor_at = time.perf_counter()
        self._frozen_ms = None

    # -- 装载 -------------------------------------------------------------- #

    def load(
        self,
        score: Score,
        spec: InstrumentSpec,
        *,
        mode: str = MODE_AUDIO,
        sink=None,
    ) -> None:
        """换谱面/换乐器/换输出通道：先停掉在播的，再绑定新的。

        `mode="keys"` 时 `sink` 必须给（`inputsim.make_key_sink()` 造出来的通道）——
        控制器只负责按时间表调它的 `key_down/key_up`，怎么注入由通道自己决定。
        """
        self.stop()
        self._score = score
        self._spec = spec
        self._mode = MODE_KEYS if str(mode).lower() == MODE_KEYS else MODE_AUDIO
        self._sink = sink if self._mode == MODE_KEYS else None
        self._ordinal_of_event = {}
        ordinal = 0
        for index, event in enumerate(score.events):
            if isinstance(event, NoteEvent):
                self._ordinal_of_event[index] = ordinal
                ordinal += 1

    def set_sink(self, sink) -> None:
        """换注入通道（前台 ⇄ 后台）：调用方应先 `stop()`，下一次 `play_from` 生效。"""
        self._sink = sink if self._mode == MODE_KEYS else None

    @property
    def mode(self) -> str:
        return self._mode

    def _on_player_state(self, state: PlayerState) -> None:
        """`Player.on_state` 的唯一出口（受 `_suppress_state` 控制）。"""
        if self._suppress_state:
            return
        self.state_changed.emit(str(state))

    # -- 控制 -------------------------------------------------------------- #

    def play(self) -> bool:
        """从 0 开始播放（等价 `play_from(0)`）。"""
        return self.play_from(0)

    def play_from(self, ordinal: int = 0) -> bool:
        """从第 `ordinal` 个和弦开始播放；`False` = 没有可播内容。

        为什么每次重建 `Player` 而不是 seek：`Player` 的启动基准是"绝对时刻表"，
        重建最省心；`Player._run` 里已跳过 `onset < start_ms` 的音，
        所以从中间开始不会把前面的音一次性爆出来。
        """
        if not self.ready:
            return False
        assert self._score is not None and self._spec is not None  # ready 保证
        notes = self.notes()
        if not notes:
            return False
        ordinal = max(0, min(int(ordinal), len(notes) - 1))
        self._suppress_state = True  # 换播放器期间旧 player 的 idle 不能漏给 UI
        try:
            self.stop(emit_state=False)
            if self._mode == MODE_KEYS:
                if self._sink is None:
                    _log.warning("按键演奏缺少注入通道，已取消")
                    return False
                player: Player | KeyPlayer = KeyPlayer(self._score, self._sink, speed=1.0)
                player.on_interrupt = self._on_key_interrupt
            else:
                player = Player(
                    self._score,
                    audio=self._ensure_audio(),
                    speed=1.0,
                    note_of_key=self._spec.note_of_key,
                )
            player.on_note = lambda _event, index: self._emit_note(index)
            player.on_state = self._on_player_state
            self._player = player
        finally:
            self._suppress_state = False
        player.play(float(notes[ordinal].onset_ms))  # 解除抑制后再起播，UI 才收得到 playing
        self._anchor(float(notes[ordinal].onset_ms))  # 进度条从这一帧重新起算
        return True

    def pause(self) -> None:
        """暂停（时间轴冻结，`resume()` 从原位置继续）。"""
        if self._player is not None:
            self._frozen_ms = self.position_ms  # 必须在 pause() 之前取：之后状态就不是 playing 了
            self._player.pause()

    def resume(self) -> bool:
        """从暂停处继续（不重建播放器，音频无缝）。"""
        if self._player is None or self._player.state is not PlayerState.PAUSED:
            return False
        frozen = self._frozen_ms
        self._player.play()  # Player 在 PAUSED 时走 clear pause 分支
        self._anchor(frozen if frozen is not None else self._anchor_ms)  # 接着冻结处继续算
        return True

    def stop(self, *, emit_state: bool = True) -> None:
        """停止并释放播放器。`emit_state=False` 用于"内部换播放器"（别让 UI 以为试听结束）。"""
        self._frozen_ms = None
        self._anchor_ms = 0.0
        player, self._player = self._player, None
        if player is None:
            return
        was_active = player.state is not PlayerState.IDLE
        self._suppress_state = True  # Player.stop() 自己也会发一次 idle，别重复
        try:
            player.stop()
        except Exception as exc:  # 停止失败不应影响 UI
            _log.debug("停止播放失败（已忽略）: %s", exc)
        finally:
            self._suppress_state = False
        if was_active and emit_state:
            # 播放中/暂停中都算"真的停掉了"，都要通知 UI，否则会卡在暂停态
            self.state_changed.emit(str(PlayerState.IDLE))

    def close(self) -> None:
        self.stop()
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.release_all()  # 兜底：绝不让游戏里的琴键卡在按下状态
            except Exception as exc:
                _log.debug("释放按键失败（已忽略）: %s", exc)
        audio, self._audio = self._audio, None
        if audio is not None:
            try:
                audio.close()
            except Exception as exc:
                _log.debug("关闭音频失败（已忽略）: %s", exc)
        self._audio_spec_id = None

    # -- 内部 -------------------------------------------------------------- #

    def _on_key_interrupt(self, reason: str) -> None:
        """演奏线程发现目标窗口不可注入 → 转成 Qt 信号（在播放线程里被调用）。"""
        self.interrupted.emit(str(reason))

    def _emit_note(self, event_index: int) -> None:
        """播放线程回调 → `events` 下标换成和弦序号再发信号。"""
        ordinal = self._ordinal_of_event.get(int(event_index))
        if ordinal is not None:
            self.note_played.emit(ordinal)

    def _ensure_audio(self) -> AudioEngine:
        """同乐器复用已预加载的采样（preload 是这里最贵的一步）。"""
        spec = self._spec
        assert spec is not None
        if self._audio is not None and self._audio_spec_id == spec.id:
            return self._audio
        if self._audio is not None:
            self.close()
        engine = AudioEngine(spec, assets_dir=assets_dir(), enabled=self._audio_enabled)
        engine.preload()
        self._audio = engine
        self._audio_spec_id = spec.id
        return engine
