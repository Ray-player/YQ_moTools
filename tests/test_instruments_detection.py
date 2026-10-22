"""乐器识别规范（`assets/instruments.json` v3）加载与容错。

v3 起三处配置各有归属：
- 顶层 `sampling`：**全局**采样几何（环宽 / 方块边长），所有乐器共用，改完运行时即时生效；
- 乐器层级 `delta`：该乐器的灵敏度 Δ（原来藏在 `detection.default_delta`，提升到显眼位置）；
- `detection` 段：判据（metric/channels）与参考色（refs）。

M2i：`fwzs` 与 `xlyq` 的参考色分家 —— 风物之诗用 1069x598 截图重测（青绿按下），
谐律键琴保留原先那张 1696x957 的棕盘参数（之前误记成风物之诗）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from yqrec.instruments import (
    instrument_deltas,
    load_instruments,
    load_sampling,
    parse_detection,
    parse_sampling,
)

ASSETS = Path(__file__).parents[1] / "assets" / "instruments.json"


def test_sampling_block_is_global_and_sane() -> None:
    """全局 sampling（M2q.3）：内环 0.80R（环宽 0.18）+ 方块半边长 0.55R（边长 1.1）+ 8/4 个点。"""
    sampling = load_sampling()
    assert sampling.ring_width_ratio == pytest.approx(0.18)
    assert sampling.ring_inner_ratio == pytest.approx(0.80), "采样点落在这条内环上"
    assert sampling.square_side_ratio == pytest.approx(1.1)
    assert sampling.square_half_ratio == pytest.approx(0.55), "半边长 = 边长 / 2"
    assert (sampling.ring_points, sampling.square_points) == (8, 4)


def test_sampling_falls_back_on_bad_values() -> None:
    default = parse_sampling(None)
    assert default.ring_width_ratio == pytest.approx(0.18), "默认内环 0.80R"
    assert default.square_side_ratio == pytest.approx(1.1), "默认半边长 0.55R"
    assert default.ring_points == 8 and default.square_points == 4, "点采样分段数默认值"
    assert default.point_patch_ratio == pytest.approx(0.06)

    bad = parse_sampling({"ring_width_ratio": 5.0, "square_side_ratio": 0.0})
    assert bad.ring_width_ratio == pytest.approx(0.18), "越界环宽必须回落"
    assert bad.square_side_ratio == pytest.approx(1.1), "越界边长必须回落"

    # 环宽不能宽到和外径重合（否则退化成实心圆），保留 0.02 余量
    edge = parse_sampling({"ring_width_ratio": 0.98, "square_side_ratio": 2.0})
    assert edge.ring_width_ratio == pytest.approx(0.18)
    assert edge.square_side_ratio == pytest.approx(2.0), "2R 是合法值（旧行为）"


def test_detection_spec_loaded_and_falls_back() -> None:
    instruments = load_instruments()
    assert len(instruments) == 6, f"应加载 6 个乐器，实际 {sorted(instruments)}"

    # fwzs 两张截图（红树/花海）联合标定：v1.20 起判据 = weighted（点采样 + 加权 + 死区），Δ=20
    fwzs = instruments["fwzs"].detection
    assert fwzs.metric == "weighted"
    assert fwzs.default_delta == 20
    assert fwzs.tolerance == 8
    assert sum(fwzs.weights or ()) == pytest.approx(1.0, abs=0.01), "权重应归一化"
    assert fwzs.weights[0] > 0.5, "风物之诗按下时 R 跌得最多，R 权重应最大"
    assert fwzs.unpressed_ref == (246, 242, 231)
    assert fwzs.pressed_ref == (163, 228, 206)
    assert fwzs.channels == ("R", "G", "B")

    # 采样几何不再挂在乐器上（由全局 sampling 段统一管理）
    assert not hasattr(fwzs, "sample_inner_ratio")

    # xlyq（谐律键琴）沿用原先那张棕盘截图（白 247,242,230 → 棕 218,196,155）
    xlyq = instruments["xlyq"].detection
    assert xlyq.metric == "weighted"
    assert xlyq.weights[2] > xlyq.weights[0], "白→棕时 B 跌得最多，B 权重应最大"
    assert xlyq.unpressed_ref == (247, 242, 230)
    assert xlyq.pressed_ref == (218, 196, 155)
    assert instruments["xlyq"].name == "谐律键琴"
    assert instruments["xlyq"].sustain is True, "谐律键琴有延音"

    for inst_id, spec in instruments.items():
        detection = spec.detection
        assert detection.metric in ("max_drop", "chroma", "euclid", "weighted", "channel"), inst_id
        assert 0 <= detection.tolerance <= 120, inst_id
        assert detection.channels, inst_id
        assert 1 <= detection.default_delta <= 255, inst_id

    # `delta` 写在乐器层级，且能通过 instrument_deltas() 一眼看全
    deltas = instrument_deltas()
    assert deltas == {i: s.detection.default_delta for i, s in instruments.items()}
    assert deltas["fwzs"] == 20

    # 缺段 / 非法值 → 回落默认（不抛异常）
    fallback = parse_detection(None, "demo")
    assert fallback.metric == "max_drop"
    assert fallback.default_delta == 40
    assert fallback.unpressed_ref is None and fallback.pressed_ref is None

    bogus = parse_detection({"metric": "nope", "channels": []}, "demo", delta=999)
    assert bogus.metric == "max_drop"
    assert bogus.channels == ("R", "G", "B")
    assert bogus.default_delta == 40, "Δ 越界回落 40"


def test_confirm_frames_parsed_and_clamped() -> None:
    """`detection.confirm_frames`：缺省 2（M4 起至少两帧），合法值透传，越界回落 2。"""
    assert parse_detection(None, "demo").confirm_frames == 2, "缺段 → 默认两帧确认"
    assert parse_detection({"confirm_frames": 3}, "demo").confirm_frames == 3
    assert parse_detection({"confirm_frames": 1}, "demo").confirm_frames == 1, "1 仍是合法值"
    assert parse_detection({"confirm_frames": 0}, "demo").confirm_frames == 2, "0 越界 → 回落"
    assert parse_detection({"confirm_frames": 99}, "demo").confirm_frames == 2, "过大 → 回落"
    assert parse_detection({"confirm_frames": "2"}, "demo").confirm_frames == 2, "字符串数字也吃"


def test_every_instrument_declares_confirm_frames() -> None:
    """真实配置里每个乐器都要显式写出 confirm_frames，方便开发者直接改 JSON 调试。"""
    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    for inst in data["instruments"]:
        assert "confirm_frames" in inst["detection"], f"{inst['id']} 缺 detection.confirm_frames"
        assert inst["detection"]["confirm_frames"] >= 2, "M4 起默认至少两帧（延音要求）"


def test_confirm_frames_parsed_and_clamped() -> None:
    """`detection.confirm_frames`：缺省 2（M4 起至少两帧），合法值透传，越界回落 2。"""
    assert parse_detection(None, "demo").confirm_frames == 2, "缺段 → 默认两帧确认"
    assert parse_detection({"confirm_frames": 3}, "demo").confirm_frames == 3
    assert parse_detection({"confirm_frames": 1}, "demo").confirm_frames == 1, "1 仍是合法值"
    assert parse_detection({"confirm_frames": 0}, "demo").confirm_frames == 2, "0 越界 → 回落"
    assert parse_detection({"confirm_frames": 99}, "demo").confirm_frames == 2, "过大 → 回落"
    assert parse_detection({"confirm_frames": "2"}, "demo").confirm_frames == 2, "字符串数字也吃"


def test_every_instrument_declares_confirm_frames() -> None:
    """真实配置里每个乐器都要显式写出 confirm_frames，方便开发者直接改 JSON 调试。"""
    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    for inst in data["instruments"]:
        assert "confirm_frames" in inst["detection"], f"{inst['id']} 缺 detection.confirm_frames"
        assert inst["detection"]["confirm_frames"] >= 2, "M4 起默认至少两帧（延音要求）"


def _rewrite(path: Path, data: dict) -> None:
    """写回 JSON 并**显式推进 mtime** —— 同一毫秒内两次写入可能拿到相同时间戳，
    而真实编辑器保存一定会变；测试里手动 utime 才能稳定验证"热重载"。"""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


def test_delta_lives_on_instrument_level(tmp_path) -> None:
    """v3：乐器层级的 `delta` 优先生效；v2 老文件把 Δ 放在 detection 里仍兼容。"""
    base = json.loads(ASSETS.read_text(encoding="utf-8"))
    inst = base["instruments"][0]
    path = tmp_path / "v3.json"

    inst["delta"] = 77
    _rewrite(path, base)
    assert load_instruments(path)[inst["id"]].detection.default_delta == 77

    inst.pop("delta")
    inst["detection"]["default_delta"] = 55
    _rewrite(path, base)
    assert load_instruments(path)[inst["id"]].detection.default_delta == 55


def test_json_reload_is_picked_up_by_mtime(tmp_path) -> None:
    """改文件（mtime 变化）后重新 load 必须拿到新值 —— UI 的"实时读取"就靠这条。"""
    target = tmp_path / "instruments.json"
    _rewrite(target, json.loads(ASSETS.read_text(encoding="utf-8")))

    assert load_sampling(target).square_side_ratio == pytest.approx(1.1)

    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    data["sampling"]["square_side_ratio"] = 1.5
    data["sampling"]["ring_width_ratio"] = 0.3
    _rewrite(target, data)

    updated = load_sampling(target)
    assert updated.square_side_ratio == pytest.approx(1.5)
    assert updated.ring_inner_ratio == pytest.approx(0.68)


def test_array_format_order_and_legacy_object_compat(tmp_path) -> None:
    """v2 数组格式按数组顺序加载；v1 对象格式仍可加载（兼容）；非法条目跳过。"""

    def _entry(inst_id: str, name: str, key_count: int) -> dict:
        return {
            "id": inst_id,
            "name": name,
            "key_count": key_count,
            "sustain": False,
            "button_radius": 47,
            "areas": [
                {
                    "code": "main",
                    "channels": ["R"],
                    "keys": [f"K{i}" for i in range(key_count)],
                }
            ],
            "note_to_key": {f"n{i}": f"K{i}" for i in range(key_count)},
            "note_to_audio": {f"n{i}": f"a{i}.mp3" for i in range(key_count)},
            "hough": {
                "dp": 2.0,
                "min_dist": 80,
                "param1": 20,
                "param2": 40,
                "min_radius": 40,
                "max_radius": 53,
                "upper_bound": 239,
            },
            "thresholds": {"main": {"min_r": 0, "max_r": 190}},
        }

    # 顺序故意与 id 字典序相反，确认"数组顺序即下拉顺序"
    array_data = {
        "version": 2,
        "instruments": [
            _entry("zzz", "甲乐器", 21),
            _entry("aaa", "乙乐器", 21),
            _entry("bad", "非法键数", 20),  # 不是 7 的倍数 → 跳过
        ],
    }
    array_path = tmp_path / "v2.json"
    array_path.write_text(json.dumps(array_data, ensure_ascii=False), encoding="utf-8")
    loaded = load_instruments(array_path)

    assert list(loaded) == ["zzz", "aaa"], f"顺序/过滤不对: {list(loaded)}"
    assert loaded["zzz"].name == "甲乐器" and loaded["zzz"].key_count == 21

    # v1 对象格式（向后兼容）
    legacy = {
        "version": 1,
        "instruments": {
            e["id"]: {k: v for k, v in e.items() if k != "id"}
            for e in array_data["instruments"][:2]
        },
    }
    legacy_path = tmp_path / "v1.json"
    legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert list(load_instruments(legacy_path)) == ["zzz", "aaa"]
