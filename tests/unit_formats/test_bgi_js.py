"""BGI JS 导出：目录结构、manifest、事件表、命名清洗。"""

from __future__ import annotations

import json

from yqrec.formats import bgi_js, tick


def test_export_structure(tmp_path):
    score = tick.parse("/*\n @name 测试曲\n @instrument 风物之诗\n*/\nA.100 100 S 200")
    target = bgi_js.export(score, tmp_path, name="测试/曲:名")

    assert target.is_dir()
    for name in ("manifest.json", "settings.json", "sheet.json", "main.js"):
        assert (target / name).exists(), name

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["main"] == "main.js"
    assert manifest["settings_ui"] == "settings.json"
    assert "测试" in manifest["name"]

    sheet = json.loads((target / "sheet.json").read_text(encoding="utf-8"))
    assert sheet["instrument"] == "风物之诗"
    events = sheet["events"]
    # A: down@0, up@100；S: down@100, up@100+50
    assert events[0] == {"t": 0.0, "key": "A", "down": True}
    assert {"t": 100.0, "key": "A", "down": False} in events
    assert {"t": 100.0, "key": "S", "down": True} in events
    assert {"t": 150.0, "key": "S", "down": False} in events
    assert len(events) == 4


def test_export_sanitizes_folder_name(tmp_path):
    score = tick.parse("A")
    target = bgi_js.export(score, tmp_path, name='a\\b/c:d*e?"f<g>h|i')
    assert target.name.endswith("_bgi")
    for bad in '\\/:*?"<>|':
        assert bad not in target.name


def test_event_order_same_t_down_before_up(tmp_path):
    score = tick.parse("A 0 B")
    events = bgi_js.build_events(score)
    same_t = [e for e in events if e["t"] == 0.0]
    assert same_t and same_t[0]["down"] is True
