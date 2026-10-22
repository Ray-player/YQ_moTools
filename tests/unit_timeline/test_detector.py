"""判按状态机：确认延迟、去抖、滞回、最小间隔。"""

from __future__ import annotations

from yqrec.timeline.detector import DetectorConfig, PressDetector


def _rgb(value: int, k: int = 21) -> list[tuple[int, int, int]]:
    return [(value, 0, 0) for _ in range(k)]


def test_basic_press_and_release(spec_fwzs):
    d = PressDetector(spec_fwzs)  # 阈值 max_r=190
    events = []
    ts = 0.0
    for _ in range(5):  # 静默
        events += d.update(_rgb(240), ts)
        ts += 0.02
    for _ in range(5):  # 按下
        events += d.update(_rgb(100), ts)
        ts += 0.02
    for _ in range(3):  # 抬起
        events += d.update(_rgb(240), ts)
        ts += 0.02

    downs = [e for e in events if e.down]
    ups = [e for e in events if not e.down]
    assert len(downs) == 21
    assert len(ups) == 21
    # 事件时间戳 = 首次满足条件的时刻（第 6 帧, ts=0.10）
    assert all(abs(e.ts - 0.10) < 1e-6 for e in downs)


def test_single_frame_press_needs_two_frames_by_default(spec_fwzs):
    """M4：默认 `confirm_frames=2` —— 只亮一帧不算按下。

    延音要求"按住态确实持续"，单帧闪一下（动画回弹/压缩噪声）不该被当成按下，
    否则延音起点会算早、还会多出幽灵音。代价是极轻的单击会漏 → 该乐器调回 1 即可。
    """
    d = PressDetector(spec_fwzs)
    events = []
    ts = 0.0
    events += d.update(_rgb(240), ts)
    ts += 0.02
    events += d.update(_rgb(100), ts)  # 单帧按下 → 不够两帧
    ts += 0.02
    events += d.update(_rgb(240), ts)

    assert [e for e in events if e.down] == [], "单帧按下不确认（默认两帧）"


def test_single_frame_press_is_kept_when_confirm_frames_1(spec_fwzs_single_frame):
    """把该乐器调回 1 帧确认时，单帧轻按仍能救回来（M2q.4 的老行为）。"""
    d = PressDetector(spec_fwzs_single_frame)
    events = []
    ts = 0.0
    events += d.update(_rgb(240), ts)
    ts += 0.02
    events += d.update(_rgb(100), ts)  # 单帧按下
    down_ts = ts
    ts += 0.02
    events += d.update(_rgb(240), ts)

    downs = [e for e in events if e.down]
    ups = [e for e in events if not e.down]
    assert len(downs) == 21, "单帧按下必须被确认"
    assert all(abs(e.ts - down_ts) < 1e-6 for e in downs), "事件时间戳取首帧"
    assert len(ups) == 21
    assert d.short_presses == 21, "计数用于诊断轻按"


def test_min_hold_still_filters_when_configured(spec_fwzs):
    """把 min_hold 调回去，单帧闪一下仍会被滤掉（特效误触时的手动保险）。"""
    d = PressDetector(spec_fwzs, cfg=DetectorConfig(min_hold_ms=25))
    events = []
    ts = 0.0
    events += d.update(_rgb(240), ts)
    ts += 0.02
    events += d.update(_rgb(100), ts)
    ts += 0.02
    events += d.update(_rgb(240), ts)
    assert events == []
    assert d.short_presses == 0


def test_hysteresis_keeps_down(spec_fwzs_single_frame):
    cfg = DetectorConfig(min_hold_ms=0, hysteresis=0.15)
    d = PressDetector(spec_fwzs_single_frame, cfg=cfg)
    # enter=190, exit=218.5；210 在滞回带内 → 仍算按下
    events = d.update(_rgb(100), 0.0)
    assert any(e.down for e in events)
    events = d.update(_rgb(210), 0.02)
    assert events == []  # 仍在滞回带内，不抬起
    events = d.update(_rgb(230), 0.04)
    assert events and not events[0].down


def test_min_gap_suppresses_fast_retrigger(spec_fwzs_single_frame):
    d = PressDetector(spec_fwzs_single_frame, cfg=DetectorConfig(min_hold_ms=0, min_gap_ms=100))
    ts = 0.0
    e1 = d.update(_rgb(100), ts)
    ts += 0.01
    e2 = d.update(_rgb(240), ts)
    ts += 0.01
    e3 = d.update(_rgb(100), ts)  # 距上次抬起 10ms < 100ms → 不触发
    assert any(e.down for e in e1)
    assert e2 and not e2[0].down
    assert e3 == []


def test_flush_releases_held_keys(spec_fwzs_single_frame):
    d = PressDetector(spec_fwzs_single_frame, cfg=DetectorConfig(min_hold_ms=0))
    d.update(_rgb(100), 0.0)
    out = d.flush(1.0)
    assert len(out) == 21
    assert all(not e.down for e in out)


def test_reset(spec_fwzs):
    d = PressDetector(spec_fwzs, cfg=DetectorConfig(min_hold_ms=0))
    d.update(_rgb(100), 0.0)
    d.reset()
    out = d.update(_rgb(240), 0.1)
    assert out == []


def test_chroma_mode_triggers_press(spec_fwzs):
    """`chroma` 判据必须被状态机识别（M2n：此前漏了分支，实机永远判不出按下）。"""
    from yqrec.types import KeyThreshold

    threshold = KeyThreshold(
        mode="chroma",
        channels=("R", "G", "B"),
        base_r=246,
        base_g=242,
        base_b=231,
        delta=30,
        shift_from=0,  # R → G：按下时"绿相对红"变强
        shift_to=1,
    )
    plan = {area.code: threshold for area in spec_fwzs.areas}
    d = PressDetector(spec_fwzs, plan)

    idle = [(246, 242, 231)] * 21
    pressed = [(163, 228, 206)] + [(246, 242, 231)] * 20

    events = []
    ts = 0.0
    for _ in range(5):
        events += d.update(idle, ts)
        ts += 0.02
    for _ in range(5):
        events += d.update(pressed, ts)
        ts += 0.02

    downs = [e for e in events if e.down]
    assert len(downs) == 1, [e.key for e in downs]
    assert downs[0].key == spec_fwzs.key_order[0]


def test_chroma_mode_ignores_global_brightness_change(spec_fwzs):
    """整体变暗（三通道同降）不应判按下：这正是 chroma 相对 max_drop 的优势。"""
    from yqrec.types import KeyThreshold

    threshold = KeyThreshold(
        mode="chroma",
        channels=("R", "G", "B"),
        base_r=246,
        base_g=242,
        base_b=231,
        delta=30,
        shift_from=0,
        shift_to=1,
    )
    plan = {area.code: threshold for area in spec_fwzs.areas}
    d = PressDetector(spec_fwzs, plan)

    events = []
    ts = 0.0
    for _ in range(10):
        events += d.update([(196, 192, 181)] * 21, ts)  # 整体 -50，色偏不变
        ts += 0.02
    assert not [e for e in events if e.down]


def _with_confirm(spec, frames: int):
    """造一个"确认帧数 = frames"的乐器副本（真实配置走 instruments.json，这里只测状态机）。"""
    from dataclasses import replace

    return replace(spec, detection=replace(spec.detection, confirm_frames=frames))


def test_confirm_frames_requires_consecutive_hits(spec_fwzs):
    """`confirm_frames=2`：第 1 帧不确认，第 2 帧才确认，时间戳仍取首帧。

    这是 M2q.4「单帧即确认」的参数化版本：默认 1 时行为与以前完全一致，
    调大只是"多看几帧再认"，不会把确认时刻写进事件时间戳（谱面时序不受影响）。
    """
    d = PressDetector(_with_confirm(spec_fwzs, 2))
    assert d.confirm_frames == 2

    ts = 0.0
    assert d.update(_rgb(240), ts) == []
    ts += 0.02
    first_hit = ts
    assert d.update(_rgb(100), first_hit) == [], "只 1 帧还不够（confirm_frames=2）"
    ts += 0.02
    downs = [e for e in d.update(_rgb(100), ts) if e.down]
    assert len(downs) == 21, "连续第 2 帧应确认"
    assert all(abs(e.ts - first_hit) < 1e-6 for e in downs), "事件时间戳取首帧，不是确认帧"
    assert d.short_presses == 21, "刚好用满 2 帧 → 计入\"最轻的按下\""


def test_confirm_frames_counts_consecutive_not_total(spec_fwzs):
    """中间断一帧就重新数（不是"累计看过几帧"）。"""
    d = PressDetector(_with_confirm(spec_fwzs, 3))
    ts = 0.0

    d.update(_rgb(240), ts)
    ts += 0.02
    d.update(_rgb(100), ts)  # 第 1 帧
    ts += 0.02
    d.update(_rgb(100), ts)  # 第 2 帧
    ts += 0.02
    assert d.update(_rgb(240), ts) == [], "中间松开一帧 → 计数归零"
    ts += 0.02
    assert d.update(_rgb(100), ts) == [], "重新第 1 帧"
    ts += 0.02
    assert d.update(_rgb(100), ts) == [], "重新第 2 帧"
    ts += 0.02
    downs = [e for e in d.update(_rgb(100), ts) if e.down]
    assert len(downs) == 21, "重新数到第 3 帧才确认"


def test_confirm_frames_one_keeps_single_frame(spec_fwzs):
    """默认 `confirm_frames=1` 时，只占 1 帧的按下仍然确认（不能因为参数化退回去）。"""
    d = PressDetector(_with_confirm(spec_fwzs, 1))
    ts = 0.0
    d.update(_rgb(240), ts)
    ts += 0.02
    assert len([e for e in d.update(_rgb(100), ts) if e.down]) == 21
    assert d.short_presses == 21


def test_fast_retrigger_is_delayed_not_dropped(spec_fwzs):
    """距上次抬起不足 `min_gap` 时不能"永远卡死"：等够间隔要补认这次按下。

    旧实现只在条件里检查 `pending_since - last_up_ts`，一旦不满足就整段跳过，
    而这两个值随后都不再变化 → 只要一直按着，这个键**永远**判不出按下
    （快速连击同一个键时第二个音整个丢失）。现在改为把计时起点推到当前帧重新数。
    """
    d = PressDetector(
        _with_confirm(spec_fwzs, 1), cfg=DetectorConfig(min_gap_ms=100)
    )
    ts = 0.0
    assert any(e.down for e in d.update(_rgb(100), ts)), "第一次按下正常确认"
    ts += 0.01
    d.update(_rgb(240), ts)  # 抬起 → last_up_ts = 0.01
    ts += 0.01
    d.update(_rgb(100), ts)  # 距抬起只有 10ms < 100ms → 本次先不认

    downs = []
    for _ in range(15):  # 继续按住 150ms
        ts += 0.01
        downs += [e for e in d.update(_rgb(100), ts) if e.down]

    assert downs, "间隔满足后必须补认，而不是把这次按下整段丢弃"
    assert downs[0].ts >= 0.10, f"确认时刻必须在 min_gap 之后，实际 {downs[0].ts:.3f}"
