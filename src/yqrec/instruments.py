"""乐器参数加载（`assets/instruments.json`，schema 见 03 §5.1/§5.2）。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .logging import get_logger
from .types import (
    DEFAULT_RECORD_CLICK_MODE,
    LEGACY_SUSTAIN_MS,
    RECORD_CLICK_MOUSE,
    RECORD_CLICK_OFF,
    RING_OUTER_RATIO,
    DetectionSpec,
    InstrumentSpec,
    KeyArea,
    KeyThreshold,
    SamplingSpec,
)

_log = get_logger("instruments")

_VALID_METRICS = ("max_drop", "chroma", "euclid", "weighted", "channel")

MAX_SUSTAIN_MS = 20000
"""`sustain_ms` 的合法上界（20s）：再长基本是"手离开但画面没恢复"，属异常。"""
_VALID_CHANNELS = ("R", "G", "B")

_KEYS_PER_ROW = 7
"""琴键每排 7 个（21 键 = 3 排，14 键 = 2 排）。"""

_JSON_CACHE: dict[Path, tuple[tuple[int, int], dict]] = {}
"""`{路径: ((mtime_ns, size), 解析后的 dict)}` —— 让"改 JSON 即时生效"不必每帧读盘。

签名同时带 size：万一两次写入落在同一个时间戳刻度上，内容长度不同也能识别出来。
"""


def project_root() -> Path:
    """src/yqrec/instruments.py → 项目根。可用 YQREC_ROOT 覆盖（打包后指向 exe 目录）。"""
    env = os.environ.get("YQREC_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    return Path(os.environ.get("YQREC_ASSETS", project_root() / "assets"))


def instruments_path() -> Path:
    return assets_dir() / "instruments.json"


def instruments_mtime() -> int:
    """乐器配置的修改时间（ns）；文件不存在返回 0。供 UI 轮询"开发者是否改了 JSON"。"""
    try:
        return instruments_path().stat().st_mtime_ns
    except OSError:
        return 0


def _read_json(path: Path) -> dict:
    """按 (mtime, size) 缓存的 JSON 读取：文件没改就复用上次解析结果（每帧调用也只有一次 stat）。"""
    try:
        st = path.stat()
    except OSError as exc:
        raise FileNotFoundError(f"找不到乐器配置: {path}") from exc
    signature = (st.st_mtime_ns, st.st_size)
    hit = _JSON_CACHE.get(path)
    if hit is not None and hit[0] == signature:
        return hit[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是对象")
    _JSON_CACHE[path] = (signature, data)
    return data


def _float_or(value: object, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _int_or(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _triplet(value: object, inst_id: str, field_name: str) -> tuple[int, int, int] | None:
    """解析 `[R, G, B]`；null/非法 → None（待实测，不影响判定）。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            pass
    _log.warning("%s: detection.%s 不是 [R,G,B]，按 None 处理", inst_id, field_name)
    return None


def parse_sampling(raw: object) -> SamplingSpec:
    """解析全局 `sampling` 段：环宽 / 方块边长 / 点采样分段数。非法值回落默认并记 warning，绝不抛。"""
    default = SamplingSpec()
    if not isinstance(raw, dict):
        if raw is not None:
            _log.warning("sampling 段不是对象，回落默认")
        return default

    width = _float_or(raw.get("ring_width_ratio", default.ring_width_ratio), default.ring_width_ratio)
    if not 0.05 <= width <= RING_OUTER_RATIO - 0.02:
        _log.warning(
            "sampling.ring_width_ratio=%s 越界（应在 0.05~%.2f），回落 %.2f",
            width,
            RING_OUTER_RATIO - 0.02,
            default.ring_width_ratio,
        )
        width = default.ring_width_ratio

    side = _float_or(raw.get("square_side_ratio", default.square_side_ratio), default.square_side_ratio)
    if not 0.1 <= side <= 4.0:
        _log.warning(
            "sampling.square_side_ratio=%s 越界（应在 0.1~4.0），回落 %.2f",
            side,
            default.square_side_ratio,
        )
        side = default.square_side_ratio

    ring_points = _int_or(raw.get("ring_points", default.ring_points), default.ring_points)
    if not 3 <= ring_points <= 256:
        _log.warning(
            "sampling.ring_points=%s 越界（应在 3~256），回落 %s", ring_points, default.ring_points
        )
        ring_points = default.ring_points

    square_points = _int_or(raw.get("square_points", default.square_points), default.square_points)
    if not 1 <= square_points <= 64:
        _log.warning(
            "sampling.square_points=%s 越界（应在 1~64），回落 %s",
            square_points,
            default.square_points,
        )
        square_points = default.square_points

    patch = _float_or(
        raw.get("point_patch_ratio", default.point_patch_ratio), default.point_patch_ratio
    )
    if not 0.0 <= patch <= 0.5:
        _log.warning(
            "sampling.point_patch_ratio=%s 越界（应在 0~0.5），回落 %.2f",
            patch,
            default.point_patch_ratio,
        )
        patch = default.point_patch_ratio

    return SamplingSpec(
        ring_width_ratio=width,
        square_side_ratio=side,
        ring_points=ring_points,
        square_points=square_points,
        point_patch_ratio=patch,
    )


def _weights_or(value: object, inst_id: str) -> tuple[float, float, float] | None:
    """解析 `detection.weights`：`[wR,wG,wB]`，负值按 0 处理；非法 → None（由 refs 推）。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            triple = tuple(max(0.0, float(v)) for v in value)
        except (TypeError, ValueError):
            triple = ()
        if triple and sum(triple) > 0:
            return triple  # type: ignore[return-value]
    _log.warning("%s: detection.weights 不是三个非负数，按 refs 自动推导", inst_id)
    return None


def parse_detection(raw: object, inst_id: str = "", *, delta: int | None = None) -> DetectionSpec:
    """解析 detection 段 + 乐器层级的 `delta`：任何非法值都回落默认并记 warning，绝不抛异常。"""
    default = DetectionSpec()
    if not isinstance(raw, dict):
        raw = {}

    metric = str(raw.get("metric", default.metric)).strip().lower()
    if metric not in _VALID_METRICS:
        _log.warning("%s: 未知 detection.metric=%r，回落 %s", inst_id, metric, default.metric)
        metric = default.metric

    channels_raw = raw.get("channels", default.channels)
    channels = tuple(
        str(c).strip().upper()
        for c in (channels_raw if isinstance(channels_raw, (list, tuple)) else [])
    )
    channels = tuple(c for c in channels if c in _VALID_CHANNELS)
    if not channels:
        _log.warning("%s: detection.channels 为空/非法，回落全通道", inst_id)
        channels = default.channels

    if delta is None:  # 旧文件把 Δ 放在 detection.default_delta 里
        delta = _int_or(raw.get("default_delta", default.default_delta), default.default_delta)
    if not 1 <= delta <= 255:
        _log.warning("%s: delta=%s 越界（应在 1~255），回落 %s", inst_id, delta, default.default_delta)
        delta = default.default_delta

    confirm_frames = _int_or(raw.get("confirm_frames", default.confirm_frames), default.confirm_frames)
    if not 1 <= confirm_frames <= 10:
        _log.warning(
            "%s: detection.confirm_frames=%s 越界（应在 1~10），回落 %s",
            inst_id,
            confirm_frames,
            default.confirm_frames,
        )
        confirm_frames = default.confirm_frames

    tolerance = _int_or(raw.get("tolerance", default.tolerance), default.tolerance)
    if not 0 <= tolerance <= 120:
        _log.warning(
            "%s: detection.tolerance=%s 越界（应在 0~120），回落 %s",
            inst_id,
            tolerance,
            default.tolerance,
        )
        tolerance = default.tolerance

    return DetectionSpec(
        metric=metric,
        channels=channels,
        default_delta=delta,
        unpressed_ref=_triplet(raw.get("unpressed_ref"), inst_id, "unpressed_ref"),
        pressed_ref=_triplet(raw.get("pressed_ref"), inst_id, "pressed_ref"),
        weights=_weights_or(raw.get("weights"), inst_id),
        tolerance=tolerance,
        confirm_frames=confirm_frames,
        note=str(raw.get("note", "")),
    )


def _iter_instrument_entries(data: object) -> list[tuple[str, dict]]:
    """把 `instruments` 节点统一成 `[(id, raw), ...]`（保持数组顺序）。

    - v2 数组格式：`[{ "id": ..., ... }, ...]`；
    - v1 对象格式：`{id: {...}}`（向后兼容，给一次迁移提示）。
    """
    if not isinstance(data, dict):
        raise ValueError("instruments.json 顶层必须是对象")
    instruments = data.get("instruments")
    entries: list[tuple[str, dict]] = []

    if isinstance(instruments, list):
        for index, raw in enumerate(instruments):
            if not isinstance(raw, dict):
                _log.warning("instruments[%d] 不是对象，已跳过", index)
                continue
            inst_id = str(raw.get("id", "")).strip()
            if not inst_id:
                inst_id = str(raw.get("name", "")).strip()
                _log.warning("instruments[%d] 缺少 id，用 name(%r) 兜底", index, inst_id)
            if not inst_id:
                _log.warning("instruments[%d] 既无 id 也无 name，已跳过", index)
                continue
            entries.append((inst_id, raw))
        return entries

    if isinstance(instruments, dict):
        _log.warning("instruments 仍是对象格式，建议迁移到数组格式（顺序即下拉顺序）")
        for inst_id, raw in instruments.items():
            if isinstance(raw, dict):
                entries.append((str(inst_id), raw))
        return entries

    raise ValueError("instruments 节点缺失或类型非法")


def _valid_key_count(raw: dict, inst_id: str) -> int | None:
    """校验 `key_count`：必须存在、> 0 且是 7 的倍数（琴键每排 7 个）。"""
    try:
        key_count = int(raw.get("key_count", 0))
    except (TypeError, ValueError):
        key_count = 0
    if key_count <= 0:
        _log.warning("%s: key_count 缺失或非正（%r），已跳过该乐器", inst_id, raw.get("key_count"))
        return None
    if key_count % _KEYS_PER_ROW != 0:
        _log.warning(
            "%s: key_count=%s 不是 7 的倍数，无法排成 7 列网格，已跳过该乐器", inst_id, key_count
        )
        return None
    return key_count


def _parse_sustain_ms(raw: dict, inst_id: str) -> int:
    """乐器级 `sustain_ms`（int，0 = 不延音，>0 = 延音阈值毫秒）。

    兼容旧写法：只写了 `sustain: true` 而没有 `sustain_ms` → 记 info 并按
    `LEGACY_SUSTAIN_MS` 处理（真正的归一化在 `InstrumentSpec.__post_init__`）。
    越界/非数值一律回落 0 并记 warning，绝不抛。
    """
    value = raw.get("sustain_ms")
    if value is None:
        if raw.get("sustain"):
            _log.info("%s: 只有旧 sustain=true，按 %d ms 阈值兼容", inst_id, LEGACY_SUSTAIN_MS)
        return 0
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        _log.warning("%s: sustain_ms=%r 不是整数，按不延音处理", inst_id, value)
        return 0
    if not 0 <= threshold <= MAX_SUSTAIN_MS:
        _log.warning(
            "%s: sustain_ms=%s 越界（应在 0~%d），按不延音处理",
            inst_id,
            threshold,
            MAX_SUSTAIN_MS,
        )
        return 0
    return threshold


def load_instruments(path: Path | str | None = None) -> dict[str, InstrumentSpec]:
    """加载乐器表，返回 `{id: InstrumentSpec}`（**插入顺序 = JSON 顺序 = 下拉顺序**）。"""
    p = Path(path) if path else instruments_path()
    data = _read_json(p)
    out: dict[str, InstrumentSpec] = {}
    for inst_id, raw in _iter_instrument_entries(data):
        if inst_id in out:
            _log.warning("乐器 id 重复: %r，保留第一个，跳过后面的", inst_id)
            continue
        key_count = _valid_key_count(raw, inst_id)
        if key_count is None:
            continue
        try:
            areas = tuple(
                KeyArea(
                    code=a["code"],
                    name=a.get("name", a["code"]),
                    channels=tuple(a.get("channels", ("R",))),
                    keys=tuple(a["keys"]),
                )
                for a in raw["areas"]
            )
            thresholds = {
                code: KeyThreshold(**t) for code, t in (raw.get("thresholds") or {}).items()
            }
            spec = InstrumentSpec(
                id=inst_id,
                name=str(raw["name"]),
                key_count=key_count,
                sustain=bool(raw.get("sustain", False)),
                areas=areas,
                note_to_key=dict(raw["note_to_key"]),
                note_to_audio=dict(raw["note_to_audio"]),
                hough=dict(raw["hough"]),
                button_radius=float(raw.get("button_radius", 47)),
                thresholds=thresholds,
                detection=parse_detection(
                    raw.get("detection"),
                    inst_id,
                    delta=None if raw.get("delta") is None else _int_or(raw.get("delta"), 40),
                ),
                sustain_ms=_parse_sustain_ms(raw, inst_id),
                preview_only=bool(raw.get("preview_only", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _log.warning("%s: 解析失败（%s），已跳过该乐器", inst_id, exc)
            continue
        out[inst_id] = spec
    if not out:
        raise ValueError(f"{p}: 没有任何可用的乐器定义")
    return out


def load_sampling(path: Path | str | None = None) -> SamplingSpec:
    """加载**全局**采样口径（环宽 / 方块边长）。缺 `sampling` 段时回落默认值。"""
    p = Path(path) if path else instruments_path()
    try:
        data = _read_json(p)
    except (OSError, ValueError) as exc:
        _log.warning("读取 sampling 失败（%s），用默认口径", exc)
        return SamplingSpec()
    return parse_sampling(data.get("sampling"))


def load_archive_path(path: Path | str | None = None) -> str:
    r"""加载**存档根目录**配置 `archive_path`（全局参数，所有乐器一致）。

    取值三形态：
    - ``"Local"``：跟 exe 同级（`<exe目录>/data`），便携/免写 C 盘用；
    - ``"Temp"``：`%APPDATA%\YQ_moTools`（**写不进去会自动降级为 Local**，见 `config.data_dir()`）；
    - 其他字符串：当作**绝对路径**直接用（`%VAR%` / `~` 会展开）；相对路径视为非法。

    缺段/非法值一律返回 ``""``（= 走默认 %APPDATA% 链路）并记 warning，绝不抛
    （与 `sampling` / `record_click` 同风格）。
    """
    p = Path(path) if path else instruments_path()
    try:
        data = _read_json(p)
    except (OSError, ValueError) as exc:
        _log.warning("读取 archive_path 失败（%s），按默认存档位置处理", exc)
        return ""
    raw = data.get("archive_path")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        _log.warning("archive_path=%r 不是字符串，按默认存档位置处理", raw)
        return ""
    return raw.strip()


def load_record_click(path: Path | str | None = None) -> int:
    """加载**全局** `record_click` 模式（录制启动前的输入注入，所有乐器一致）。

    取值：``0``=不启用，``1``=激活窗口+键盘空格，``2``=鼠标点击。
    兼容旧写法 ``{"enabled": true/false}`` → ``2``/``0``（记 info 提示迁移）。
    缺段/任何非法值一律回落 ``0`` 并记 warning，绝不抛（与 sampling 同风格）。
    """
    p = Path(path) if path else instruments_path()
    try:
        data = _read_json(p)
    except (OSError, ValueError) as exc:
        _log.warning("读取 record_click 失败（%s），按禁用处理", exc)
        return DEFAULT_RECORD_CLICK_MODE

    raw = data.get("record_click")
    if raw is None:
        return DEFAULT_RECORD_CLICK_MODE

    # 新写法：顶层 int（0/1/2）
    if isinstance(raw, bool):
        _log.warning("record_click=%r 是布尔值，按 0/1 容错映射（请改为 int 0/1/2）", raw)
        return RECORD_CLICK_MOUSE if raw else RECORD_CLICK_OFF
    if isinstance(raw, int):
        if raw in (0, 1, 2):
            return raw
        _log.warning("record_click=%r 不是 0/1/2，按禁用处理", raw)
        return DEFAULT_RECORD_CLICK_MODE

    # 旧写法：{"enabled": bool}（上一版发布格式，兼容读取）
    if isinstance(raw, dict):
        enabled = raw.get("enabled")
        if isinstance(enabled, bool):
            _log.info("record_click 用的是旧 enabled 写法，%s → 模式 2/0（建议改为 int）", enabled)
            return RECORD_CLICK_MOUSE if enabled else RECORD_CLICK_OFF
        if isinstance(enabled, int) and enabled in (0, 1):  # 容忍 0/1 字面量
            return RECORD_CLICK_MOUSE if enabled else RECORD_CLICK_OFF
        _log.warning("record_click.enabled=%r 非法，按禁用处理", enabled)
        return DEFAULT_RECORD_CLICK_MODE

    _log.warning("record_click=%r 类型不支持，按禁用处理", raw)
    return DEFAULT_RECORD_CLICK_MODE


_SAMPLING_WRITABLE = (
    "ring_width_ratio",
    "square_side_ratio",
    "ring_points",
    "square_points",
    "point_patch_ratio",
)


def update_sampling(**updates: float) -> bool:
    """把采样几何写回 `assets/instruments.json` 的 `sampling` 段（界面滑条用）。

    做**文本级**替换而不是整份 `json.dump`：保留用户手写的排版与 `_comment`，
    只改 `"ring_width_ratio": 0.43` 这类数值。键不存在时退回整份重写（并提示）。

    Returns:
        True = 已写盘；False = 值没变 / 失败（失败只记 warning，不抛）。
    """
    values = {
        key: value
        for key, value in updates.items()
        if key in _SAMPLING_WRITABLE and value is not None
    }
    if not values:
        return False
    path = instruments_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("写采样参数失败（读不到 %s）: %s", path, exc)
        return False

    changed = False
    for key, value in values.items():
        pattern = re.compile(r'("%s"\s*:\s*)(-?\d+(?:\.\d+)?)' % re.escape(key))
        if isinstance(value, int) or float(value).is_integer() and key.endswith("_points"):
            literal = str(int(value))
        else:
            literal = repr(round(float(value), 4))

        def _sub(match: re.Match[str]) -> str:
            return f"{match.group(1)}{literal}"

        new_text, count = pattern.subn(_sub, text)
        if count:
            if new_text != text:
                changed = True
            text = new_text
            continue
        _log.warning("sampling 段缺少 %s，改为整份重写", key)
        try:
            data = json.loads(text)
        except ValueError as exc:
            _log.warning("instruments.json 解析失败，放弃写入: %s", exc)
            return False
        sampling = data.setdefault("sampling", {})
        if not isinstance(sampling, dict):
            sampling = {}
            data["sampling"] = sampling
        sampling[key] = value
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        changed = True

    if not changed:
        return False
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        _log.warning("写采样参数失败（%s）: %s", path, exc)
        return False
    _JSON_CACHE.pop(path, None)  # 让下一次 load_sampling/load_instruments 读到新值
    _log.info("采样参数已写回 %s: %s", path.name, values)
    return True


def instrument_deltas(path: Path | str | None = None) -> dict[str, int]:
    """`{id: Δ}` —— 调试面板/日志用，一眼看出每个乐器当前灵敏度。"""
    return {i: s.detection.default_delta for i, s in load_instruments(path).items()}


LEGACY_INSTRUMENT_IDS = {"xlyq": "xljq"}
"""历史 id 别名 → 当前 id。

2026-10-22 用户指出谐律键琴的 id 应为 `xljq`（原 `xlyq` 是笔误）。已改 JSON / 音频目录 /
文档，但**旧标定档与旧谱面**里可能还写着老 id，读的时候用 `resolve_instrument_id()`
兜一下即可（下拉里只会出现当前 id，不会多出旧条目）。
"""


def resolve_instrument_id(inst_id: str) -> str:
    """把旧 id 映射到当前 id（读旧档/旧谱面用；未知 id 原样返回）。"""
    key = str(inst_id)
    return LEGACY_INSTRUMENT_IDS.get(key, key)


def load_instrument(inst_id: str, path: Path | str | None = None) -> InstrumentSpec:
    instruments = load_instruments(path)
    try:
        return instruments[resolve_instrument_id(inst_id)]
    except KeyError as exc:
        raise KeyError(f"未知乐器 {inst_id!r}，可用: {sorted(instruments)}") from exc
