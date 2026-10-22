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
```
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..instruments import assets_dir
from ..logging import get_logger
from ..player.audio import AudioEngine
from ..player.engine import Player, PlayerState
from ..types import InstrumentSpec, NoteEvent, Score

_log = get_logger("ui.playback")


class PlaybackController(QObject):
    """把 `Score` 播出来，并把进度事件转成 Qt 信号。"""

    note_played = Signal(int)
    """第 `index` 个和弦已播放（索引与 tick 文本 token 顺序一致）。"""

    state_changed = Signal(str)
    """`"playing"` / `"idle"`（播完或手动停止都回 idle）。"""

    def __init__(self, parent: QObject | None = None, *, audio_enabled: bool = True) -> None:
        super().__init__(parent)
        self._audio_enabled = audio_enabled
        self._score: Score | None = None
        self._spec: InstrumentSpec | None = None
        self._audio: AudioEngine | None = None
        self._audio_spec_id: str | None = None
        self._player: Player | None = None
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

    # -- 装载 -------------------------------------------------------------- #

    def load(self, score: Score, spec: InstrumentSpec) -> None:
        """换谱面/换乐器：先停掉在播的，再绑定新的。"""
        self.stop()
        self._score = score
        self._spec = spec
        self._ordinal_of_event = {}
        ordinal = 0
        for index, event in enumerate(score.events):
            if isinstance(event, NoteEvent):
                self._ordinal_of_event[index] = ordinal
                ordinal += 1

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
            audio = self._ensure_audio()
            player = Player(
                self._score,
                audio=audio,
                speed=1.0,
                note_of_key=self._spec.note_of_key,
            )
            player.on_note = lambda _event, index: self._emit_note(index)
            player.on_state = self._on_player_state
            self._player = player
        finally:
            self._suppress_state = False
        player.play(float(notes[ordinal].onset_ms))  # 解除抑制后再起播，UI 才收得到 playing
        return True

    def pause(self) -> None:
        """暂停（时间轴冻结，`resume()` 从原位置继续）。"""
        if self._player is not None:
            self._player.pause()

    def resume(self) -> bool:
        """从暂停处继续（不重建播放器，音频无缝）。"""
        if self._player is None or self._player.state is not PlayerState.PAUSED:
            return False
        self._player.play()  # Player 在 PAUSED 时走 clear pause 分支
        return True

    def stop(self, *, emit_state: bool = True) -> None:
        """停止并释放播放器。`emit_state=False` 用于"内部换播放器"（别让 UI 以为试听结束）。"""
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
        audio, self._audio = self._audio, None
        if audio is not None:
            try:
                audio.close()
            except Exception as exc:
                _log.debug("关闭音频失败（已忽略）: %s", exc)
        self._audio_spec_id = None

    # -- 内部 -------------------------------------------------------------- #

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
