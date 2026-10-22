"""`instruments.json` 全局 `record_click` 参数（录制启动输入注入模式）解析与容错。

取值 int：0=不启用，1=激活窗口+键盘空格，2=鼠标点击。
兼容旧写法 `{"enabled": bool/0/1}` → 2/0；缺段/非法值一律回落 0，绝不抛。
"""

from __future__ import annotations

import itertools
import json

from yqrec.instruments import instruments_path, load_record_click
from yqrec.types import DEFAULT_RECORD_CLICK_MODE

_WRITE_SEQ = itertools.count(1)


def _write(tmp_path, data: dict) -> str:
    # 每次写不同文件：_JSON_CACHE 按 (mtime, size) 判重，同路径连写两个
    # 长度相同的内容（如 1→0）可能命中同一时间戳刻度而拿到旧值
    path = tmp_path / f"instruments-{next(_WRITE_SEQ)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_record_click_missing_section_defaults_off(tmp_path) -> None:
    """缺 `record_click`（旧文件）→ 模式 0，不抛。"""
    mode = load_record_click(_write(tmp_path, {"version": 4, "instruments": []}))
    assert mode == DEFAULT_RECORD_CLICK_MODE == 0


def test_record_click_int_modes_parsed(tmp_path) -> None:
    assert load_record_click(_write(tmp_path, {"record_click": 0})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": 1})) == 1
    assert load_record_click(_write(tmp_path, {"record_click": 2})) == 2


def test_record_click_invalid_int_falls_back_to_off(tmp_path) -> None:
    assert load_record_click(_write(tmp_path, {"record_click": 3})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": -1})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": "2"})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": [2]})) == 0


def test_record_click_legacy_enabled_dict_mapped(tmp_path) -> None:
    """上一版发布的 `{"enabled": true/false}` 写法 → 2/0。"""
    assert load_record_click(_write(tmp_path, {"record_click": {"enabled": True}})) == 2
    assert load_record_click(_write(tmp_path, {"record_click": {"enabled": False}})) == 0
    # 容忍 0/1 字面量
    assert load_record_click(_write(tmp_path, {"record_click": {"enabled": 1}})) == 2
    assert load_record_click(_write(tmp_path, {"record_click": {"enabled": 0}})) == 0


def test_record_click_legacy_invalid_values_fall_back_to_off(tmp_path) -> None:
    assert load_record_click(_write(tmp_path, {"record_click": {"enabled": "true"}})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": {"hold_ms": 80}})) == 0
    assert load_record_click(_write(tmp_path, {"record_click": "yes"})) == 0


def test_record_click_missing_file_defaults_off(tmp_path) -> None:
    assert load_record_click(tmp_path / "nope.json") == 0


def test_shipped_instruments_json_has_explicit_record_click_mode() -> None:
    """发布的 instruments.json 显式配置了合法的 record_click int 模式（不依赖代码缺省 0）。

    不锁死具体值：0/1/2 都是合法运行态（用户实机会在 1/2 间切换验证）。
    """
    raw = json.loads(instruments_path().read_text(encoding="utf-8"))["record_click"]
    assert isinstance(raw, int) and not isinstance(raw, bool) and raw in (0, 1, 2)
    assert load_record_click() == raw
