"""按键演奏（M5）：时间轴展开 + 注入顺序。

纯逻辑测试，**不碰真实键鼠** —— 注入通道用 `FakeSink` 记账。
"""

from __future__ import annotations

import time

from yqrec.formats.bgi_sheet import score_from_tick_text
from yqrec.player.engine import PlayerState
from yqrec.player.keys import (
    DEFAULT_KEY_HOLD_MS,
    RETRIGGER_GAP_MS,
    KeyPlayer,
    build_key_timeline,
)


def actions_of(text: str, *, hold: float = DEFAULT_KEY_HOLD_MS) -> list[tuple]:
    timeline = build_key_timeline(score_from_tick_text(text), min_hold_ms=hold)
    return [(round(a.at_ms, 3), a.down, a.key) for a in timeline.actions]


class FakeSink:
    """记账用通道：顺序即注入顺序。"""

    mode = "background"

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.ready_calls = 0

    def key_down(self, key: str) -> bool:
        self.events.append(("down", key))
        return True

    def key_up(self, key: str) -> bool:
        self.events.append(("up", key))
        return True

    def release_all(self) -> None:
        self.events.append(("release", ""))

    def force_release(self, keys) -> None:
        self.events.append(("force", "".join(sorted(keys))))

    def ready(self) -> bool:
        self.ready_calls += 1
        return True

    def pressed(self) -> list[tuple[str, str]]:
        return [item for item in self.events if item[0] not in ("release", "force")]


def wait_idle(player: KeyPlayer, limit: float = 3.0) -> None:
    deadline = time.perf_counter() + limit
    while time.perf_counter() < deadline:
        if player.state is PlayerState.IDLE:
            return
        time.sleep(0.01)
    raise AssertionError("播放线程没有在限定时间内结束")


# -- 时间轴 ---------------------------------------------------------------- #


def test_single_note_uses_min_hold() -> None:
    # 没有延音的音也必须按住一小会儿，否则游戏可能整帧跳过
    assert actions_of("A") == [(0.0, True, "A"), (DEFAULT_KEY_HOLD_MS, False, "A")]


def test_sustain_wins_over_min_hold() -> None:
    assert actions_of("A[300]") == [(0.0, True, "A"), (300.0, False, "A")]


def test_chord_presses_together() -> None:
    timeline = build_key_timeline(score_from_tick_text("AS"), min_hold_ms=60)
    downs = [a.key for a in timeline.actions if a.down]
    ups = [a.key for a in timeline.actions if not a.down]
    assert sorted(downs) == ["A", "S"]
    assert sorted(ups) == ["A", "S"]
    assert all(a.at_ms == 0.0 for a in timeline.actions if a.down)


def test_overlapping_same_key_releases_early() -> None:
    # 同键第二段在 400 开始，第一段延音到 500 → 必须提前到 400 - gap 抬起，否则重按被吞
    got = actions_of("A[500]400A")
    assert got[0] == (0.0, True, "A")
    assert got[1] == (400.0 - RETRIGGER_GAP_MS, False, "A")
    assert got[2] == (400.0, True, "A")


def test_butt_joined_notes_keep_full_sustain() -> None:
    # "刚好接到下一音"不算交叠：延音不该被白砍 30ms
    got = actions_of("A[500]500A")
    assert got[0] == (0.0, True, "A")
    assert got[1] == (500.0, False, "A")
    assert got[2] == (500.0, True, "A")


def test_up_sorts_before_down_at_same_instant() -> None:
    # 同一时刻既有抬起又有按下时，抬起必须先发（否则注入层会把 down 当成重复按下）
    timeline = build_key_timeline(score_from_tick_text("A[500]500A"), min_hold_ms=60)
    groups: dict[float, list[bool]] = {}
    for action in timeline.actions:
        groups.setdefault(round(action.at_ms, 3), []).append(action.down)
    same_instant = [flags for flags in groups.values() if len(flags) > 1]
    assert same_instant, "这份谱面应当在同一时刻既有抬起又有按下"
    for flags in same_instant:
        assert flags[0] is False


def test_duration_covers_last_hold() -> None:
    timeline = build_key_timeline(score_from_tick_text("A100B[400]"), min_hold_ms=0)
    assert timeline.duration_ms == 500.0


def test_note_count_matches_chords() -> None:
    timeline = build_key_timeline(score_from_tick_text("AS100D200F"), min_hold_ms=60)
    assert timeline.note_count == 3


# -- 播放线程 -------------------------------------------------------------- #


def test_player_injects_in_order() -> None:
    sink = FakeSink()
    player = KeyPlayer(score_from_tick_text("A100B"), sink)
    player.play()
    wait_idle(player)
    assert sink.pressed() == [("down", "A"), ("up", "A"), ("down", "B"), ("up", "B")]
    assert sink.events[-2] == ("release", "")  # 收尾一定补一次"全松开"
    assert sink.events[-1] == ("force", "AB"), "再对用到的键逐个补发抬起（防琴键卡死）"


def test_start_in_the_middle_restores_held_keys() -> None:
    sink = FakeSink()
    player = KeyPlayer(score_from_tick_text("A[400]"), sink)
    player.play(30.0)  # 30ms 处 A 正被按着 → 必须补一个 down
    wait_idle(player)
    assert sink.pressed()[0] == ("down", "A")


def test_pause_releases_keys() -> None:
    sink = FakeSink()
    player = KeyPlayer(score_from_tick_text("A[600]"), sink)
    player.play()
    time.sleep(0.1)
    player.pause()
    assert ("release", "") in sink.events
    assert player.state is PlayerState.PAUSED
    player.stop()
    assert player.state is PlayerState.IDLE


def test_stop_releases_everything() -> None:
    sink = FakeSink()
    player = KeyPlayer(score_from_tick_text("A[2000]"), sink)
    player.play()
    time.sleep(0.08)
    player.stop()
    assert ("release", "") in sink.events
    assert sink.events[-1] == ("force", "A")
    assert player.state is PlayerState.IDLE


def test_unready_sink_interrupts_with_reason() -> None:
    class DeadSink(FakeSink):
        def ready(self) -> bool:
            return False

    reasons: list[str] = []
    player = KeyPlayer(score_from_tick_text("A"), DeadSink())
    player.on_interrupt = reasons.append
    player.play()
    wait_idle(player)
    assert reasons and "已停止" in reasons[0]


def test_blocked_sink_keeps_playing_instead_of_interrupting() -> None:
    """前台通道挡住时不中断：时间轴照走，只把这一拍跳过（M6d 用户要求）。

    如果这里中断/暂停，点「演奏」的瞬间前台必然是本程序 → 前台模式永远起不来。
    """

    class BlockedSink(FakeSink):
        interrupt_when_blocked = False

        def ready(self) -> bool:
            return False

        def key_down(self, key: str) -> bool:
            return False  # 与 ForegroundKeys 同款守卫：挡住时根本不发

        def key_up(self, key: str) -> bool:
            return True

    sink = BlockedSink()
    reasons: list[str] = []
    blocked: list[bool] = []
    player = KeyPlayer(score_from_tick_text("A200S"), sink)
    player.on_interrupt = reasons.append
    player.on_blocked = blocked.append
    player.play()
    wait_idle(player, limit=4.0)

    assert not reasons, f"被挡时不该中断: {reasons}"
    assert blocked == [True], "只提示一次状态翻转"
    assert player.state is PlayerState.IDLE, "必须一路播到底"
    assert sink.pressed() == [], "全程没有真发键"
