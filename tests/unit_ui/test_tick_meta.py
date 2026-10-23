"""tick 谱面 JSON 的 v5 元数据（M2q.11）+ 录制键位回放的数据侧。

用户四项要求：
1. `version` 后面加 `"type": "tick"`（曲谱类型）；
2. `"Average_Delta"` = 本次识别所有按下的平均 Δ；
3. 紧跟其后 `"Min_Delta"` = 最小 Δ（最接近阈值的那一下）；
4. 试听刚录的谱面时，键位灯按录制实况亮（按下/弹起/按下时 Δ），数据只在内存里。
"""

from __future__ import annotations

import numpy as np
import pytest

from yqrec.instruments import load_instrument
from yqrec.recording import KeySpan, RecordingBuffer, key_spans
from yqrec.scorefile import (
    DELTA_AVG_KEY,
    DELTA_MIN_KEY,
    RECORD_VERSION,
    SCORE_TYPE_TICK,
    build_record,
    build_record_from_text,
    record_delta_stats,
    record_score_type,
    save_record_text,
)
from yqrec.timeline.detector import PressDetector
from yqrec.types import InstrumentSpec, PressEvent
from yqrec.vision.thresholds import thresholds_from_baseline

# --------------------------------------------------------------------------- #
# 判按器：Δ 要跟着按下事件一起出来
# --------------------------------------------------------------------------- #


def _detector(spec: InstrumentSpec, delta: int = 20) -> tuple[PressDetector, np.ndarray]:
    base = np.full((spec.key_count, 3), 230.0, dtype=np.float32)
    thresholds = thresholds_from_baseline(base, spec, delta)
    return PressDetector(spec, thresholds, baseline=base), base


def _frame(base: np.ndarray, drops: dict[int, float]) -> np.ndarray:
    rgb = base.copy()
    for index, drop in drops.items():
        rgb[index] = base[index] - drop
    return rgb


def test_press_event_carries_delta(spec_fwzs: InstrumentSpec) -> None:
    detector, base = _detector(spec_fwzs)
    key = spec_fwzs.key_order[0]
    events = []
    for ts, drop in ((0.0, 0), (0.03, 45), (0.06, 45), (0.09, 0)):
        events.extend(detector.update(_frame(base, {0: drop}), ts))

    down = [e for e in events if e.down]
    up = [e for e in events if not e.down]
    assert len(down) == 1 and down[0].key == key
    assert down[0].delta > 0, "按下必须带判据量（Δ），否则谱面没法做质量校验"
    assert up and up[0].delta == 0, "抬起事件不参与 Δ 统计"


def test_flush_events_have_zero_delta(spec_fwzs: InstrumentSpec) -> None:
    detector, base = _detector(spec_fwzs)
    for ts in (0.0, 0.03, 0.06):
        detector.update(_frame(base, {0: 50}), ts)
    flushed = detector.flush(0.09)
    assert flushed and all(e.delta == 0 for e in flushed)


# --------------------------------------------------------------------------- #
# Recording：逐键区间 + 统计
# --------------------------------------------------------------------------- #


def _recording(spec: InstrumentSpec, presses: list[PressEvent], frames: int = 40):
    buf = RecordingBuffer(spec)
    for i in range(frames):
        buf.push_frame(i * 0.03)
    buf.push_events(presses)
    return buf.finish()


def test_recording_builds_spans_and_stats(spec_fwzs: InstrumentSpec) -> None:
    key0, key1 = spec_fwzs.key_order[0], spec_fwzs.key_order[1]
    rec = _recording(
        spec_fwzs,
        [
            PressEvent(key=key0, down=True, ts=0.0, delta=40.0),
            PressEvent(key=key0, down=False, ts=0.09),
            PressEvent(key=key1, down=True, ts=0.30, delta=24.0),
            PressEvent(key=key1, down=False, ts=0.36),
        ],
    )
    assert [(s.key, round(s.down_ms), round(s.up_ms)) for s in rec.spans] == [
        (key0, 0, 90),
        (key1, 300, 360),
    ]
    assert rec.spans[0].delta == 40.0 and rec.spans[1].delta == 24.0
    assert rec.avg_delta == pytest.approx(32.0)
    assert rec.min_delta == pytest.approx(24.0)
    assert rec.spans[0].hold_ms == pytest.approx(90.0)


def test_unclosed_press_still_gets_a_span(spec_fwzs: InstrumentSpec) -> None:
    """录制被强行切断时还按着键：按最后一个事件闭合（不能丢这个区间）。"""
    key = spec_fwzs.key_order[0]
    rec = _recording(spec_fwzs, [PressEvent(key=key, down=True, ts=0.0, delta=30.0)], frames=10)
    assert len(rec.spans) == 1
    assert rec.spans[0].down_ms == 0.0
    assert rec.spans[0].up_ms > 0.0


def test_key_spans_pairs_in_order() -> None:
    """同一键连按两次：先按下先配对，不被后一次覆盖。"""
    spans = key_spans(
        [
            PressEvent(key="A", down=True, ts=0.0, delta=30),
            PressEvent(key="A", down=False, ts=0.1),
            PressEvent(key="A", down=True, ts=0.2, delta=50),
            PressEvent(key="A", down=False, ts=0.4),
        ]
    )
    assert [(s.down_ms, s.up_ms) for s in spans] == [(0.0, 100.0), (200.0, 400.0)]
    assert [s.delta for s in spans] == [30.0, 50.0]


def test_no_delta_data_is_zero(spec_fwzs: InstrumentSpec) -> None:
    rec = _recording(spec_fwzs, [PressEvent(key=spec_fwzs.key_order[0], down=True, ts=0.0)])
    assert rec.avg_delta == 0.0 and rec.min_delta == 0.0


# --------------------------------------------------------------------------- #
# JSON schema
# --------------------------------------------------------------------------- #


def test_record_json_has_type_and_delta_keys(spec_fwzs: InstrumentSpec) -> None:
    key0, key1 = spec_fwzs.key_order[0], spec_fwzs.key_order[1]
    rec = _recording(
        spec_fwzs,
        [
            PressEvent(key=key0, down=True, ts=0.0, delta=40.4),
            PressEvent(key=key0, down=False, ts=0.09),
            PressEvent(key=key1, down=True, ts=0.30, delta=23.6),
            PressEvent(key=key1, down=False, ts=0.36),
        ],
    )
    record = build_record(rec, spec_fwzs)

    assert record["version"] == RECORD_VERSION == 5
    assert record["type"] == SCORE_TYPE_TICK == "tick"
    # 用户指定键名（Avage 原样保留），值是四舍五入的整数灰阶
    # 键名是 Average_Delta（v1.44 起；v1.43 那半天写出去的 Avage_Delta 只保留读取兼容）
    assert record[DELTA_AVG_KEY] == 32
    assert record[DELTA_MIN_KEY] == 24
    assert list(record)[:4] == ["version", "type", DELTA_AVG_KEY, DELTA_MIN_KEY]
    assert DELTA_AVG_KEY == "Average_Delta"

    assert record_score_type(record) == "tick"
    assert record_delta_stats(record) == (32, 24)


def test_legacy_avage_key_still_reads() -> None:
    """v1.43 的笔误键 `Avage_Delta` 只保留**读取**兼容（写盘只用 Average_Delta）。"""
    from yqrec.scorefile import DELTA_AVG_KEY_LEGACY

    legacy = {"version": 5, "type": "tick", DELTA_AVG_KEY_LEGACY: 37, DELTA_MIN_KEY: 21}
    assert record_delta_stats(legacy) == (37, 21)
    assert DELTA_AVG_KEY_LEGACY == "Avage_Delta"


def test_text_record_writes_zero_stats(spec_fwzs: InstrumentSpec) -> None:
    """手输 / 读取来的谱面没有识别数据 → 0（不是"识别得很弱"）。"""
    record = build_record_from_text("A100F", spec_fwzs)
    assert record["type"] == "tick"
    assert (record[DELTA_AVG_KEY], record[DELTA_MIN_KEY]) == (0, 0)
    assert record_delta_stats(record) == (0, 0)


def test_text_record_can_carry_stats(spec_fwzs: InstrumentSpec, tmp_path) -> None:
    """录制后手动改了几个字再存：采集信息不可信，但 Δ 统计仍属这次录制。"""
    saved = save_record_text(
        tmp_path / "谱面",
        "A100F",
        spec_fwzs,
        delta_stats=(41.6, 25.2),
    )
    import json

    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record[DELTA_AVG_KEY] == 42
    assert record[DELTA_MIN_KEY] == 25


def test_old_record_without_type_reads_as_empty() -> None:
    """v4 及更早的文件没有 `type`/Δ 键 —— 读出来是空/0，不能抛。"""
    legacy = {"version": 4, "tick": {"unit": "ms", "text": "A100F"}}
    assert record_score_type(legacy) == ""
    assert record_delta_stats(legacy) == (0, 0)


def test_spans_are_not_written_to_json(spec_fwzs: InstrumentSpec) -> None:
    """逐键区间只在内存里（用户要求减小开销），JSON 里不能出现。"""
    rec = _recording(
        spec_fwzs,
        [
            PressEvent(key=spec_fwzs.key_order[0], down=True, ts=0.0, delta=40.0),
            PressEvent(key=spec_fwzs.key_order[0], down=False, ts=0.09),
        ],
    )
    assert rec.spans, "内存里必须有"
    text = str(build_record(rec, spec_fwzs))
    assert "spans" not in text and "KeySpan" not in text
    assert isinstance(rec.spans[0], KeySpan)


def test_load_instrument_has_expected_order() -> None:
    """键区间/回放都按 `spec.key_order` 点亮（面板卡片同序），锁一下前提。"""
    spec = load_instrument("fwzs")
    assert len(spec.key_order) == spec.key_count
