"""读取 BetterGI 谱面（midi / yuanqin）后写进编辑框的 tick 文本。

规则（M6d 用户要求）：`[ms]` 后缀只在**目标乐器支持延音**时才写 ——
风物之诗琴这类按下即发声的乐器，把文件里的按住时长照搬过来只会得到
一串 `A[500]120B` 的噪音；文件没标乐器时按风物之诗琴处理。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")

# A 按住 500ms，620ms 处按 D 并按 60ms
BGI_MIDI_NOTES = "DA0|UA500|DD120|UD60"


def _bgi(path, *, instrument: str = "", notes: str = BGI_MIDI_NOTES) -> str:
    sheet = {
        "name": "测试曲",
        "type": "midi",
        "bpm": "125",
        "ticks": 480,
        "instrument": instrument,
        "notes": notes,
    }
    path.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    return win, app, tmp_path


def _load(win, app, monkeypatch: pytest.MonkeyPatch, path: str) -> str:
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, ""))
    )
    win._load_score_file()
    app.processEvents()
    return win._record_view.toPlainText()


def test_no_instrument_defaults_to_sustain_free_text(window, monkeypatch) -> None:
    """文件没标乐器 → 按风物之诗琴（无延音）转换，文本里不该有 `[`。"""
    win, app, tmp_path = window
    path = _bgi(tmp_path / "无乐器.json")
    text = _load(win, app, monkeypatch, path)
    assert text == "A620D", text
    assert "[" not in text
    win.close()


def test_sustain_instrument_keeps_sustain_suffix(window, monkeypatch) -> None:
    """文件标了谐律键琴（支持延音）→ 照旧写 `[ms]`，不能把延音丢掉。"""
    win, app, tmp_path = window
    path = _bgi(tmp_path / "谐律键琴.json", instrument="谐律键琴")
    text = _load(win, app, monkeypatch, path)
    # 每个音的按住时长都留着（这正是用户抱怨"全是延音"的样子 —— 但延音乐器需要它）
    assert text == "A[500]620D[60]", text
    win.close()


def test_stripped_text_drives_playback_score(window, monkeypatch) -> None:
    """文本是唯一来源：延音被裁掉后，演奏用的谱面也必须跟着没有延音。"""
    from yqrec.types import NoteEvent

    win, app, tmp_path = window
    path = _bgi(tmp_path / "无乐器2.json")
    _load(win, app, monkeypatch, path)
    score = win._playback_score
    assert score is not None
    sustains = [e.sustain_ms for e in score.events if isinstance(e, NoteEvent)]
    assert sustains == [pytest.approx(0.0), pytest.approx(0.0)], "编辑框里没有延音就别偷偷带延音"
    win.close()


def test_yuanqin_sheet_with_fwzs_has_no_sustain(window, monkeypatch) -> None:
    """yuanqin 谱（样例自标风物之诗琴）→ 编辑框里不该出现 `[ms]` 延音后缀。"""
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "data" / "bgi" / "pugongying_yuanqin.json"
    if not sample.exists():  # 样例随仓库带，缺了就别拦着其它用例
        pytest.skip("缺少 yuanqin 样例")
    win, app, _tmp = window
    text = _load(win, app, monkeypatch, str(sample))
    assert text.strip(), "必须转出内容"
    assert "[" not in text, text[:120]
    win.close()
