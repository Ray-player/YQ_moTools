"""BetterGI JS 导出（03 §3.10）：生成可直接拷进 `User\\JsScript\\` 的脚本目录。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..types import NoteEvent, Score

DEFAULT_HOLD_MS = 50.0
"""无延音时，keyDown→keyUp 的默认时长（ms）。"""

_SHEET_VERSION = 1

_MAIN_JS = """\
// 由 YQ_moTools 生成：在乐器界面运行，使用游戏默认键位。
(async function () {
  try {
    const sheet = JSON.parse(file.readTextSync("sheet.json"));
    let settings = { countdown_ms: 3000 };
    try {
      settings = JSON.parse(file.readTextSync("settings.json"));
    } catch (e) { /* 用默认值 */ }
    const t0 = Date.now() + (settings.countdown_ms || 0);
    for (const e of sheet.events) {
      const target = t0 + e.t;
      while (Date.now() < target) await sleep(Math.min(target - Date.now(), 40));
      if (e.down) keyDown(e.key); else keyUp(e.key);
    }
    log.info(`播放完成：${sheet.name}`);
  } catch (err) {
    log.error(`播放失败: ${err}`);
  } finally {
    for (const k of "ASDFGHJQWERTYUZXCVBNM") keyUp(k);  // 防卡键
  }
})();
"""


def build_events(score: Score, *, default_hold_ms: float = DEFAULT_HOLD_MS) -> list[dict]:
    """NoteEvent → (t, key, down) 事件表，按 t 稳定排序（同 t 先 down 后 up）。"""
    events: list[dict] = []
    for ev in score.events:
        if not isinstance(ev, NoteEvent):
            continue
        hold = ev.sustain_ms if ev.sustain_ms > 0 else default_hold_ms
        t = round(float(ev.onset_ms), 3)
        for key in ev.keys:
            events.append({"t": t, "key": key, "down": True})
            events.append({"t": round(t + hold, 3), "key": key, "down": False})
    events.sort(key=lambda e: (e["t"], 0 if e["down"] else 1))
    return events


def export(
    score: Score,
    out_dir: Path | str,
    *,
    name: str | None = None,
    instrument: str | None = None,
    countdown_ms: int = 3000,
) -> Path:
    """写出 manifest.json / settings.json / sheet.json / main.js，返回目录路径。"""
    title = name or score.metadata.get("name") or "未命名"
    instr = instrument or score.metadata.get("instrument", "")
    folder = _safe_folder_name(title)
    target = Path(out_dir) / folder
    target.mkdir(parents=True, exist_ok=True)

    manifest = {
        "manifest_version": 1,
        "name": f"识谱导出 - {title}",
        "version": "1.0",
        "bgi_version": "0.36.1",
        "description": (
            "YQ_moTools 导出：请在乐器界面运行，使用默认键位"
            "（A S D F G H J / Q W E R T Y U / Z X C V B N M）"
        ),
        "authors": [{"name": "YQ_moTools"}],
        "settings_ui": "settings.json",
        "main": "main.js",
        "saved_files": [],
    }
    sheet = {
        "version": _SHEET_VERSION,
        "name": title,
        "instrument": instr,
        "events": build_events(score),
    }

    _write_json(target / "manifest.json", manifest)
    _write_json(target / "settings.json", {"countdown_ms": countdown_ms})
    _write_json(target / "sheet.json", sheet)
    (target / "main.js").write_text(_MAIN_JS, encoding="utf-8")
    return target


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return (cleaned or "未命名") + "_bgi"
