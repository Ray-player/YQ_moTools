"""`preview_only` 乐器：下拉里**看得见、选不了**。

悠可琴（ykq）/ 余音（yy）的双区配色和和弦区判定还没实机标定，选中必然认不准 →
先在乐器下拉里占位但灰显，等标定完把 `instruments.json` 的 `preview_only` 去掉即可恢复。

要保证的四件事：
1. 下拉里仍然**列出**它们（用户能看到"支持哪些乐器"），但 `ItemIsEnabled` 为假；
2. 任何**程序化**切换（旧标定档恢复 / 读取属于它的谱面）都不能把当前乐器切到它上面；
3. 旧标定档如果是它的 → 整份不恢复（基线/阈值绑在它身上，套到别的乐器上必错）；
4. 默认可选乐器跳过它（JSON 顺序变了也不会一启动就选中一个不可选的）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

PREVIEW_IDS = ("ykq", "yy")


def _window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yqrec.ui.main_window import MainWindow

    return app, MainWindow()


def _items(combo) -> dict[str, object]:
    """`{inst_id: QStandardItem}`，方便逐项断言。"""
    model = combo.model()
    out = {}
    for row in range(model.rowCount()):
        item = model.item(row)
        out[str(item.data(Qt.ItemDataRole.UserRole))] = item
    return out


def test_preview_instruments_are_listed_but_disabled(monkeypatch, tmp_path) -> None:
    app, window = _window(monkeypatch, tmp_path)
    items = _items(window._combo_instrument)

    for inst_id in PREVIEW_IDS:
        assert inst_id in items, "仅预览的乐器仍要出现在下拉里（用户看得见）"
        item = items[inst_id]
        assert not item.flags() & Qt.ItemFlag.ItemIsEnabled, f"{inst_id} 不能可选"
        assert not item.flags() & Qt.ItemFlag.ItemIsSelectable, f"{inst_id} 不能被选中"
        assert "仅预览" in item.text(), "文字上要写清楚为什么不能选"
        assert item.toolTip(), "灰显项要有气泡说明"

    for inst_id in ("fwzs", "xljq", "jhzq", "ljdsq"):
        item = items[inst_id]
        assert item.flags() & Qt.ItemFlag.ItemIsEnabled, f"{inst_id} 必须可选"
        assert "仅预览" not in item.text()

    assert window._spec.id not in PREVIEW_IDS, "启动时默认乐器不能是仅预览的"
    window.close()
    app.processEvents()


def test_switching_to_preview_instrument_is_refused(monkeypatch, tmp_path) -> None:
    """程序化 `_load_instrument('yy')` 要被拒，且下拉回退到原乐器（不能停在灰显项上）。"""
    app, window = _window(monkeypatch, tmp_path)
    before = window._spec.id

    window._load_instrument("yy")
    assert window._spec.id == before, "仅预览的乐器不能变成当前乐器"
    assert window._combo_instrument.currentData() == before, "下拉要回退，别停在灰显项"
    window.close()
    app.processEvents()


def test_profile_for_preview_instrument_is_not_restored(monkeypatch, tmp_path) -> None:
    """旧标定档属于仅预览乐器 → 整份跳过（不给"看起来恢复了"的假状态）。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    profile = {
        "name": "default",
        "instrument": "ykq",
        "backend": "auto",
        "ref_size": [800, 400],
        "created": "2026-10-22T11:00:00",
        "target": {"kind": "screen", "monitor": 0, "rect": [0, 0, 800, 400]},
        "keygrid": {
            "points": [[0.1 + 0.04 * i, 0.8, 50 / 1920] for i in range(21)],
            "key_count": 21,
        },
        "baseline": [[250, 245, 235]] * 21,
        "thresholds": {},
    }
    target = Path(tmp_path) / "YQ_moTools" / "profiles" / "default.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    app, window = _window(monkeypatch, tmp_path)
    assert window._spec.id != "ykq"
    assert window._baseline is None, "别的乐器的基线不能拿来用"
    assert window._grid is None
    window.close()
    app.processEvents()


def test_preview_flag_comes_from_json(monkeypatch, tmp_path) -> None:
    """`preview_only` 必须来自 `instruments.json`，代码里不该出现乐器 id 分支。"""
    app, window = _window(monkeypatch, tmp_path)
    for inst_id, spec in window._instruments.items():
        assert spec.preview_only is (inst_id in PREVIEW_IDS), (
            f"{inst_id} 的 preview_only 应来自 JSON"
        )
    window.close()
    app.processEvents()
