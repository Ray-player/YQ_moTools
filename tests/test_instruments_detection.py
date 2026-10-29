"""乐器识别规范（`assets/instruments.json` v3）加载与容错。

v3 起三处配置各有归属：
- 顶层 `sampling`：**全局**采样几何（环宽 / 方块边长），所有乐器共用，改完运行时即时生效；
- 乐器层级 `delta`：该乐器的灵敏度 Δ（原来藏在 `detection.default_delta`，提升到显眼位置）；
- `detection` 段：判据（metric/channels）与参考色（refs）。

M2i：`fwzs` 与 `xljq` 的参考色分家 —— 风物之诗用 1069x598 截图重测（青绿按下），
谐律键琴保留原先那张 1696x957 的棕盘参数（之前误记成风物之诗）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from yqrec.instruments import (
    instrument_deltas,
    load_instrument,
    load_instruments,
    load_key_play_delay_ms,
    load_key_play_entry_visible,
    load_sampling,
    parse_detection,
    parse_sampling,
    resolve_instrument_id,
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

    # xljq（谐律键琴）沿用原先那张棕盘截图（白 247,242,230 → 棕 218,196,155）
    xljq = instruments["xljq"].detection
    assert xljq.metric == "weighted"
    assert xljq.weights[2] > xljq.weights[0], "白→棕时 B 跌得最多，B 权重应最大"
    assert xljq.unpressed_ref == (247, 242, 230)
    assert xljq.pressed_ref == (218, 196, 155)
    assert instruments["xljq"].name == "谐律键琴"
    assert instruments["xljq"].sustain is True, "谐律键琴有延音"

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


def test_legacy_instrument_id_resolves_to_new_one() -> None:
    """2026-10-22 谐律键琴 id 由笔误 `xlyq` 更正为 `xljq`：旧档必须还能读。"""
    assert resolve_instrument_id("xlyq") == "xljq"
    assert resolve_instrument_id("xljq") == "xljq"
    assert resolve_instrument_id("nope") == "nope", "未知 id 原样返回，交给上层报错"

    spec = load_instrument("xlyq")  # 旧标定档 / 旧谱面里可能写的是老 id
    assert spec.id == "xljq"
    assert spec.name == "谐律键琴"

    assert "xlyq" not in load_instruments(), "旧 id 不能出现在下拉里"


def test_key_play_delay_is_global_and_defaults_to_3000() -> None:
    """M6f：起播延迟是**全局**参数（所有乐器共用），缺省 3000ms。"""
    from yqrec.types import DEFAULT_KEY_PLAY_DELAY_MS

    assert DEFAULT_KEY_PLAY_DELAY_MS == 3000
    assert load_key_play_delay_ms() == 3000
    for spec in load_instruments().values():
        assert not hasattr(spec, "key_play_delay_ms"), "不该再有乐器级延迟"


def test_key_play_delay_from_json_and_clamped(tmp_path) -> None:
    """JSON 顶层写了就按它；非法/越界不抛，钳到 0~60000。缺项回落 3000。"""
    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    path = tmp_path / "instruments.json"

    for raw, expected in ((800, 800), (0, 0), (999999, 60000), (-5, 0), ("abc", 3000)):
        data["key_play_delay_ms"] = raw
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        got = load_key_play_delay_ms(path)
        assert got == expected, (raw, got)

    data.pop("key_play_delay_ms")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_key_play_delay_ms(path) == 3000, "没写就用默认值"


def test_key_play_delay_is_shared_by_all_instruments(tmp_path) -> None:
    """同一个全局值对每个乐器都成立（用户明确要求"所有乐器共用同一延迟"）。"""
    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    data["key_play_delay_ms"] = 1500
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_key_play_delay_ms(path) == 1500
    assert len(load_instruments(path)) >= 6, "全局参数不该影响乐器解析"


def test_key_play_entry_visible_default_off(tmp_path) -> None:
    """M6h：「演奏」入口显隐是全局 int 开关，**没写就是关闭**。

    注意别拿仓库的 `assets/instruments.json` 断言这条：实机测演奏期间那里是 `1`，
    否则这条测试会随"当前产品开关状态"时红时绿。这里只验默认语义。
    """
    assert load_key_play_entry_visible(tmp_path / "nope.json") is False, "文件缺失 → 隐藏"

    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    data.pop("key_play_entry_visible", None)
    path = tmp_path / "instruments_no_key.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_key_play_entry_visible(path) is False, "缺项 → 隐藏"


def test_key_play_entry_visible_reads_json(tmp_path) -> None:
    """1 = 显示，0 = 隐藏；缺项/非整数一律按隐藏；非 0 值当 1 处理并记 warning。"""
    data = json.loads(ASSETS.read_text(encoding="utf-8"))

    # 每个用例写**独立文件**：`_read_json` 按 (mtime, size) 缓存，
    # 同尺寸内容（"0" 与 "1" 字节数一样）在同一时间戳刻度内重写会被当成没变。
    for index, (raw, expected) in enumerate(
        ((1, True), (0, False), ("1", True), (2, True), (None, False))
    ):
        payload = dict(data)
        if raw is None:
            payload.pop("key_play_entry_visible", None)
        else:
            payload["key_play_entry_visible"] = raw
        path = tmp_path / f"instruments_{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        assert load_key_play_entry_visible(path) is expected, (raw, expected)

    bad = dict(data, key_play_entry_visible="abc")
    path = tmp_path / "instruments_bad.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert load_key_play_entry_visible(path) is False, "非法值不许把按钮亮出来"


def test_key_play_channel_default_background() -> None:
    """v1.51：注入通道由全局 int 参数 `key_play_channel` 决定，缺省 = 后台。"""
    from yqrec.instruments import (
        DEFAULT_KEY_PLAY_CHANNEL,
        KEY_PLAY_CHANNEL_BACKGROUND,
        load_key_play_channel,
    )

    assert DEFAULT_KEY_PLAY_CHANNEL == KEY_PLAY_CHANNEL_BACKGROUND == "background"
    assert load_key_play_channel() == KEY_PLAY_CHANNEL_BACKGROUND, "仓库自带配置是 0=后台"


def test_key_play_channel_reads_json(tmp_path) -> None:
    """0 = 后台 / 1 = 前台；缺项、非整数、越界一律回落后台（绝不抛）。"""
    from yqrec.instruments import KEY_PLAY_CHANNEL_BACKGROUND, KEY_PLAY_CHANNEL_FOREGROUND
    from yqrec.instruments import load_key_play_channel

    data = json.loads(ASSETS.read_text(encoding="utf-8"))
    cases = (
        (0, KEY_PLAY_CHANNEL_BACKGROUND),
        (1, KEY_PLAY_CHANNEL_FOREGROUND),
        ("1", KEY_PLAY_CHANNEL_FOREGROUND),
        ("0", KEY_PLAY_CHANNEL_BACKGROUND),
        (7, KEY_PLAY_CHANNEL_BACKGROUND),
        ("abc", KEY_PLAY_CHANNEL_BACKGROUND),
        (None, KEY_PLAY_CHANNEL_BACKGROUND),
    )
    # 每个用例独立文件：`_read_json` 按 (mtime, size) 缓存，同尺寸内容可能被判成没变
    for index, (raw, expected) in enumerate(cases):
        payload = dict(data)
        if raw is None:
            payload.pop("key_play_channel", None)
        else:
            payload["key_play_channel"] = raw
        path = tmp_path / f"channel_{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        assert load_key_play_channel(path) == expected, (raw, expected)

    missing = tmp_path / "channel_missing.json"
    assert load_key_play_channel(missing) == KEY_PLAY_CHANNEL_BACKGROUND, "读不到文件也不抛"
