"""录制产物落盘（M2）：谱面 JSON（**以 tick 文本为唯一谱面数据**）。

设计原则：**只存一份谱面**。文件里的 `tick.text` 就是谱子本身 —— 自 v3 起数字是
**毫秒间隔**（`A1000F` = 按 A 后隔 1000ms 再按 F），还原时直接用，不需要任何 fps；
不再冗余展开成 `events[]`（`score_from_record()` 一步得到可播放的 `Score`）。

为什么不直接存 `.txt`：纯 tick 文本丢了乐器、采集后端、创建时间，这些留在
`instrument` / `capture` 块里。`capture` 的 fps/frames 现在**只作记录**、不参与换算
（v2 及更早的帧制文件仍按它换算，见 `record_unit()`）。

schema 版本：`version: 3`（`tick.unit = "ms"`）。v2 是 `frame`，v1 还带冗余 `events[]`；
两者都能读，因为时间轴一直只依赖 `tick.text` + `tick.unit`。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR_NAME
from .recording import Recording, TickNote, parse_tick_text
from .types import InstrumentSpec, NoteEvent, RestEvent, Score, ScoreEvent

RECORD_FORMAT = "yqrec.recording"
RECORD_VERSION = 3
"""schema 版本：v3 起 `tick.unit = "ms"`（v2 是 `frame`，v1 还带冗余 `events[]`）。"""

TICK_UNIT_MS = "ms"
TICK_UNIT_FRAME = "frame"
"""`tick.unit` 的取值。读文件时按它决定"数字"要不要乘帧时长。"""

DEFAULT_SCORE_FORMAT = "tick"
"""谱面格式标识（写进 JSON 的 `score_format`，也拼进默认文件名）。

当前只有 `tick`（毫秒间隔文本）；后续加 BetterGI / fengwu 等格式时，各用各的标识，
文件名与文件内容都能一眼看出谱面是怎么编码的。
"""

DEFAULT_MS_PER_FRAME = 1000.0 / 60.0
"""旧文件（`tick.unit == "frame"`）兜底帧时长：fps 缺失且时长不可推时用 60fps。"""


def record_default_dir() -> Path:
    """默认保存目录：`<用户>\\Documents\\YQ_moTools`（没有 Documents 就退回用户主目录）。"""
    home = Path.home()
    documents = home / "Documents"
    return (documents if documents.exists() else home) / DATA_DIR_NAME


def suggested_filename(
    score_format: str = DEFAULT_SCORE_FORMAT,
    *,
    prefix: str = "谱面",
    now: datetime | None = None,
) -> str:
    """默认文件名：`谱面_tick_20261020_1647.json`（含谱面格式段）。"""
    moment = now or datetime.now()
    return f"{prefix}_{score_format}_{moment:%Y%m%d_%H%M%S}.json"


def build_record(
    recording: Recording,
    spec: InstrumentSpec,
    *,
    name: str = "",
    backend: str = "",
    score_format: str = DEFAULT_SCORE_FORMAT,
    extra: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把一次录制转成可序列化的 dict（纯函数，便于单测）。"""
    moment = now or datetime.now().astimezone()
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "format": RECORD_FORMAT,
        "score_format": score_format,
        "name": name,
        "created_at": moment.isoformat(timespec="seconds"),
        "instrument": {"id": spec.id, "name": spec.name, "key_count": spec.key_count},
        "capture": {
            "backend": backend,
            "fps": round(float(recording.fps), 2),
            "frames": int(recording.frames),
            "duration_ms": round(float(recording.duration_ms), 1),
        },
        "tick": {"unit": TICK_UNIT_MS, "text": recording.tick_text},
    }
    if extra:
        record["extra"] = dict(extra)
    return record


def build_record_from_text(
    text: str,
    spec: InstrumentSpec,
    *,
    fps: float = 0.0,
    name: str = "",
    backend: str = "",
    score_format: str = DEFAULT_SCORE_FORMAT,
    extra: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把**任意来源**的 tick 文本（手输 / 读取的谱面 / 录制残留）包成谱面 JSON。

    文本按毫秒制解释（`A1000F` = 隔 1000ms），所以不需要 fps；
    `capture` 里的 fps/frames 只作记录，不参与换算（谱面时间轴全在 `tick.text` 里）。
    """
    moment = now or datetime.now().astimezone()
    notes = parse_tick_text(text)
    total_ms = sum(note.gap for note in notes)
    try:
        fps_value = round(float(fps), 2) if fps and fps > 0 else 0.0
    except (TypeError, ValueError):
        fps_value = 0.0
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "format": RECORD_FORMAT,
        "score_format": score_format,
        "name": name,
        "created_at": moment.isoformat(timespec="seconds"),
        "instrument": {"id": spec.id, "name": spec.name, "key_count": spec.key_count},
        "capture": {
            "backend": backend,
            "fps": fps_value,
            "frames": int(round(total_ms * fps_value / 1000.0)) if fps_value else 0,
            "duration_ms": total_ms,
        },
        "tick": {"unit": TICK_UNIT_MS, "text": text.strip()},
    }
    if extra:
        record["extra"] = dict(extra)
    return record


def save_record_text(
    path: Path | str,
    text: str,
    spec: InstrumentSpec,
    *,
    name: str = "",
    backend: str = "",
    fps: float = 0.0,
    score_format: str = DEFAULT_SCORE_FORMAT,
    extra: dict[str, Any] | None = None,
) -> Path:
    """把文本框里的谱面文本直接存成 JSON（不需要先有一份 `Recording`）。"""
    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_record_from_text(
        text,
        spec,
        fps=fps,
        name=name or target.stem,
        backend=backend,
        score_format=score_format,
        extra=extra,
    )
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def save_record(
    path: Path | str,
    recording: Recording,
    spec: InstrumentSpec,
    *,
    name: str = "",
    backend: str = "",
    score_format: str = DEFAULT_SCORE_FORMAT,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写 JSON（自动建目录、补 `.json` 后缀），返回实际路径。"""
    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_record(
        recording,
        spec,
        name=name or target.stem,
        backend=backend,
        score_format=score_format,
        extra=extra,
    )
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_record(path: Path | str) -> dict[str, Any]:
    """读回 JSON（v1 旧文件也能读：`events[]` 忽略，谱面取 `tick.text`）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 还原：tick 文本 → 可播放的 Score
# --------------------------------------------------------------------------- #


def record_tick_text(record: dict[str, Any]) -> str:
    """取谱面文本（兼容 `tick` 直接是字符串的写法）。"""
    tick = record.get("tick")
    if isinstance(tick, str):
        return tick
    if isinstance(tick, dict):
        return str(tick.get("text", ""))
    return ""


def record_notes(record: dict[str, Any]) -> list[TickNote]:
    """解析谱面文本（和弦 + 间隔数字，单位见 `record_unit`）。"""
    return parse_tick_text(record_tick_text(record))


def record_unit(record: dict[str, Any]) -> str:
    """`tick.unit`：新文件是 `"ms"`；v2 及更早没有这个字段 → 按帧处理。"""
    tick = record.get("tick")
    if isinstance(tick, dict):
        unit = str(tick.get("unit") or "").strip().lower()
        if unit in {TICK_UNIT_MS, TICK_UNIT_FRAME}:
            return unit
    return TICK_UNIT_FRAME


def ms_per_frame(record: dict[str, Any]) -> float:
    """**旧文件**（`tick.unit == "frame"`）的帧 → 毫秒换算系数。

    优先用「实测时长 / (帧数-1)」（最接近真实采集节奏），其次 `1000/fps`，
    都没有就按 60fps 兜底。新文件不需要它。
    """
    capture = record.get("capture") or {}
    frames = int(capture.get("frames") or 0)
    duration_ms = float(capture.get("duration_ms") or 0.0)
    if frames > 1 and duration_ms > 0:
        return duration_ms / (frames - 1)
    fps = float(capture.get("fps") or 0.0)
    if fps > 0:
        return 1000.0 / fps
    return DEFAULT_MS_PER_FRAME


def score_from_record(record: dict[str, Any]) -> Score:
    """把 JSON 还原成可播放的 `Score`（音符 onset + 休止，全部由 tick 文本推出）。

    用法::

        score = score_from_record(load_record(path))
        Player(score, audio=AudioEngine(spec, assets_dir=...)).play()

    时间轴：新文件（`unit = "ms"`）数字直接当毫秒；旧文件（`frame`）乘
    `ms_per_frame()`。两者都只靠 `tick.text`，没有第二份谱面数据。
    """
    factor = 1.0 if record_unit(record) == TICK_UNIT_MS else ms_per_frame(record)
    notes = record_notes(record)
    events: list[ScoreEvent] = []
    cursor = 0.0
    for note in notes:
        if note.gap > 0:
            gap = note.gap * factor
            events.append(RestEvent(ms=gap))
            cursor += gap
        events.append(NoteEvent(keys=note.keys, onset_ms=cursor))
    instrument = record.get("instrument") or {}
    metadata = {
        "name": str(record.get("name", "")),
        "instrument": str(instrument.get("name", "")),
        "instrument_id": str(instrument.get("id", "")),
    }
    return Score(events=events, metadata=metadata)
