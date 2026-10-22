"""数据模型与乐器参数的不变式测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from yqrec.instruments import load_instrument
from yqrec.types import (
    KeyGrid,
    KeyPoint,
    KeyThreshold,
    NoteEvent,
    Profile,
    Rect,
    RestEvent,
    ScreenTarget,
    WindowTarget,
    target_from_dict,
)


def test_rect_basic():
    r = Rect(10, 20, 1600, 900)
    assert (r.right, r.bottom) == (1610, 920)
    assert r.almost_16x9()


def test_rect_rejects_non_positive():
    with pytest.raises(ValueError):
        Rect(0, 0, 0, 900)


def test_screen_target_and_window_target_validation():
    st = ScreenTarget(monitor=0, rect=Rect(0, 0, 1600, 900))
    assert st.monitor == 0
    with pytest.raises(ValueError):
        WindowTarget(hwnd=0, norm=(0, 0, 1, 1))
    with pytest.raises(ValueError):
        WindowTarget(hwnd=1, norm=(0.5, 0.5, 0.4, 0.9))


def test_target_dict_roundtrip():
    st = ScreenTarget(0, Rect(1, 2, 1600, 900))
    data = {"kind": "screen", "monitor": 0, "rect": [1, 2, 1600, 900]}
    assert target_from_dict(data) == st
    wt = WindowTarget(123, (0.1, 0.2, 0.9, 0.95), title="原神")
    data2 = {"kind": "window", "hwnd": 123, "norm": [0.1, 0.2, 0.9, 0.95]}
    parsed = target_from_dict(data2)
    assert isinstance(parsed, WindowTarget)
    assert (parsed.hwnd, parsed.norm) == (wt.hwnd, wt.norm)
    assert parsed.title is None  # 标题不参与持久化（运行时按标题查找）


def test_key_threshold_and_semantics():
    thr = KeyThreshold(min_r=0, max_r=190)
    assert thr.accepts((100, 0, 0), ("R",))
    assert not thr.accepts((200, 0, 0), ("R",))
    # G/B 未启用 → 不参与判定
    assert thr.accepts((10, 255, 255), ("R",))
    # 无有效通道 → False
    assert not KeyThreshold().accepts((0, 0, 0), ("R",))


def test_instrument_specs_loaded(all_specs):
    assert set(all_specs) == {"fwzs", "xlyq", "jhzq", "ljdsq", "ykq", "yy"}
    for spec in all_specs.values():
        assert spec.key_count == 21
        assert len(spec.note_to_key) == 21
        assert len(spec.note_to_audio) == 21
        assert spec.note_order[:3] == ("+1", "+2", "+3")
        assert spec.key_order[:3] == ("Q", "W", "E")


def test_yy_has_chord_area():
    yy = load_instrument("yy")
    assert [a.code for a in yy.areas] == ["chord", "main"]
    assert yy.area_of_key("Q").code == "chord"
    assert yy.area_of_key("A").code == "main"
    assert yy.threshold_of_key("Q").max_r == 50
    assert yy.threshold_of_key("A").max_r == 190


def test_note_event_validation():
    with pytest.raises(ValueError):
        NoteEvent(keys=(), onset_ms=0)
    with pytest.raises(ValueError):
        NoteEvent(keys=("A", "A"), onset_ms=0)
    with pytest.raises(ValueError):
        RestEvent(ms=-1)


def test_key_grid_validation():
    p = KeyPoint(0.5, 0.5, 47 / 1920)
    with pytest.raises(ValueError):
        KeyGrid(points=(p,), key_count=21)
    with pytest.raises(ValueError):
        KeyPoint(-0.1, 0.5, 0.02)


def test_profile_roundtrip_dict():
    spec = load_instrument("fwzs")
    grid = KeyGrid(points=tuple(KeyPoint(0.1, 0.6, 47 / 1920) for _ in range(21)), key_count=21)
    prof = Profile(
        name="game_fwzs",
        target=ScreenTarget(0, Rect(0, 0, 1600, 900)),
        instrument=spec.id,
        keygrid=grid,
        thresholds={"main": KeyThreshold(max_r=180)},
    )
    d = prof.to_dict()
    assert d["name"] == "game_fwzs"
    assert d["target"]["kind"] == "screen"
    assert len(d["keygrid"]["points"]) == 21
    assert d["thresholds"]["main"]["max_r"] == 180


def test_sustain_flag_replace():
    spec = load_instrument("fwzs")
    assert not spec.sustain
    s2 = replace(spec, sustain=True)
    assert s2.sustain
