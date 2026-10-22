"""录制前自动阈值标定（03 §3.5 / 01 §6.2）。

流程（三步向导的数据侧）：
1. 静默基线若干帧 → per-key release 分布；
2. 逐键试按若干帧 → per-key press 分布；
3. 选分离度最高的通道，求 enter/exit 阈值；分离度不足的键报出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..types import InstrumentSpec, KeyThreshold

MIN_DELTA = 25.0
"""按下/抬起的最小灰阶差（G2 门禁）。"""

MIN_RATIO = 2.5
"""delta / std 的最小分离度（G2 门禁）。"""

ENTER_FRACTION = 0.55
"""enter = release_mean - ENTER_FRACTION * delta。"""

CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2}


@dataclass(slots=True)
class CalibrationResult:
    thresholds: dict[str, KeyThreshold] = field(default_factory=dict)
    channel_by_area: dict[str, str] = field(default_factory=dict)
    deltas: dict[str, int] = field(default_factory=dict)
    low_snr_keys: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.low_snr_keys


def calibrate(
    baseline: np.ndarray,
    pressed: np.ndarray,
    spec: InstrumentSpec,
) -> CalibrationResult:
    """`baseline`/`pressed`: (F, K, 3) uint8 RGB，K = spec.key_count。

    通道候选来自 `KeyArea.channels`；每个区域独立选一个通道（其余通道阈值置 0）。
    """
    baseline = np.asarray(baseline, dtype=np.float32)
    pressed = np.asarray(pressed, dtype=np.float32)
    if baseline.ndim != 3 or pressed.ndim != 3 or baseline.shape[1:] != pressed.shape[1:]:
        raise ValueError("baseline/pressed 形状必须为 (F, K, 3) 且 K 一致")
    if baseline.shape[1] != spec.key_count:
        raise ValueError(f"样本键数 {baseline.shape[1]} 与乐器键数 {spec.key_count} 不符")

    res = CalibrationResult()
    low: list[str] = []

    for area in spec.areas:
        candidates = [c for c in area.channels if c in CHANNEL_INDEX] or ["R", "G", "B"]
        best_channel = candidates[0]
        best_ratio = -1.0

        # 先为每个键选区域级通道（取区域平均分离度最优）
        for ch in candidates:
            idx = CHANNEL_INDEX[ch]
            deltas = np.abs(baseline[:, :, idx].mean(axis=0) - pressed[:, :, idx].mean(axis=0))
            stds = baseline[:, :, idx].std(axis=0) + 1e-6
            ratio = float(np.mean(deltas / stds))
            if ratio > best_ratio:
                best_ratio, best_channel = ratio, ch

        idx = CHANNEL_INDEX[best_channel]
        rel_mean = baseline[:, :, idx].mean(axis=0)
        prs_mean = pressed[:, :, idx].mean(axis=0)
        stds = baseline[:, :, idx].std(axis=0) + 1e-6
        deltas = np.abs(rel_mean - prs_mean)

        if area.code not in res.channel_by_area:
            res.channel_by_area[area.code] = best_channel

        # 区域级阈值（取区域平均的 enter），逐键检查分离度
        area_enter = float((rel_mean - ENTER_FRACTION * deltas).mean())
        area_enter = min(254.0, max(1.0, area_enter))
        res.thresholds[area.code] = _make_threshold(best_channel, int(round(area_enter)))
        res.deltas[area.code] = int(round(float(deltas.mean())))

        for i, key in enumerate(area.keys):
            if deltas[i] < MIN_DELTA or deltas[i] / stds[i] < MIN_RATIO:
                low.append(key)

    res.low_snr_keys = tuple(low)
    return res


def _make_threshold(channel: str, enter: int) -> KeyThreshold:
    kwargs: dict[str, int] = {}
    prefix = {"R": "r", "G": "g", "B": "b"}[channel]
    kwargs[f"min_{prefix}"] = 0
    kwargs[f"max_{prefix}"] = enter
    return KeyThreshold(**kwargs)


def _chroma_axis(detection) -> tuple[int, int]:  # noqa: ANN001 - DetectionSpec（避免循环导入）
    """由未按下/按下参考色推出"哪个通道相对哪个通道变强"。

    取 `(pressed[j]-pressed[i]) - (unpressed[j]-unpressed[i])` 最大的那对 `(i, j)`：
    风物之诗（米白→青绿）是 R→G，谐律键琴（白→棕）是 B→R。缺参考色时默认 R→G。
    """
    unpressed = detection.unpressed_ref
    pressed = detection.pressed_ref
    if unpressed is None or pressed is None:
        return (0, 1)
    best = (0, 1)
    best_value = -10**9
    for i in range(3):
        for j in range(3):
            if i == j:
                continue
            value = (pressed[j] - pressed[i]) - (unpressed[j] - unpressed[i])
            if value > best_value:
                best_value, best = value, (i, j)
    return best


def chroma_axis(detection) -> tuple[int, int]:  # noqa: ANN001 - DetectionSpec
    """公开入口：由参考色推出"哪个通道相对哪个通道变强"（供 UI 诊断用）。"""
    return _chroma_axis(detection)


def press_weights(detection) -> tuple[float, float, float]:  # noqa: ANN001 - DetectionSpec
    """`weighted` 判据的三通道权重（R,G,B 顺序，和为 1）。

    优先用 JSON 里显式写的 `detection.weights`；没写就由参考色的跌幅自动推出
    （`w ∝ max(0, unpressed_ref - pressed_ref)`）——
    这样"白→红/青"这种按下时 G/B 掉得多、R 几乎不变的乐器，会自动对 G/B 更敏感；
    两者都没有（refs 未标定）时退化为等权，等价于"三通道平均跌幅"。
    """
    explicit = getattr(detection, "weights", None)
    if explicit is not None:
        raw = [max(0.0, float(v)) for v in explicit]
        total = sum(raw)
        if total > 0:
            return (raw[0] / total, raw[1] / total, raw[2] / total)

    unpressed = detection.unpressed_ref
    pressed = detection.pressed_ref
    if unpressed is not None and pressed is not None:
        drops = [max(0.0, float(unpressed[i]) - float(pressed[i])) for i in range(3)]
        total = sum(drops)
        if total > 0:
            return (drops[0] / total, drops[1] / total, drops[2] / total)
    return (1 / 3, 1 / 3, 1 / 3)


_MODE_BY_METRIC = {
    "max_drop": "max_drop",
    "chroma": "chroma",
    "weighted": "weighted",
    "euclid": "euclid",
}


def thresholds_from_baseline(
    baseline: np.ndarray,
    spec: InstrumentSpec,
    delta: float,
) -> dict[str, KeyThreshold]:
    """单点基线标定：只采"全部松开"的基线，用固定 Δ 生成阈值（M1 GUI 用）。

    判据由**乐器配置驱动**（`spec.detection.metric`，改 JSON 即可切换，不用改代码）：

    - `max_drop`：`max over detection.channels of (base_c - cur_c) >= delta`
      （真机实测：白盘→棕盘 R -29 / G -46 / B -75，B 通道跌幅最大，只盯最亮的 R 会永不触发）；
    - `chroma`：`(cur_to - cur_from) - (base_to - base_from) >= delta`，方向由参考色推出。
      半透明键帽会透出背景，背景亮度变化会等比例压缩 `max_drop`（花海 28 vs 暗背景 69），
      而"相对色偏"基本不受影响，所以跨背景场景用它更稳；
    - `weighted`：`Σ w_c·max(0, (base_c-current_c) - tolerance) / Σ w_c >= delta`，
      权重由 `detection.weights` 或 refs 的跌幅推出（见 `press_weights`），
      对"按下时掉得多的通道"更敏感，并用逐通道死区吃掉各键基准色偏差；
    - `euclid`：`||cur - base|| / sqrt(len(channels)) >= delta`；
    - `channel`：旧的单通道区间语义（保留兼容）。

    `base_*` 一律取该 area 成员键的基线均值，`delta` 取用户灵敏度。

    Args:
        baseline: `(K, 3)` 每个键的基线 RGB 均值（K = `spec.key_count`）。
        spec: 乐器参数（用于 area → 键的映射）。
        delta: 灵敏度（灰阶差，建议 5~120）。

    Returns:
        `{area code: KeyThreshold}`（`mode="max_drop"`），可直接传给 `PressDetector`。

    Raises:
        ValueError: 基线形状非法或键数与 spec 不符。
    """
    detection = spec.detection
    base = np.asarray(baseline, dtype=np.float32)
    if base.ndim != 2 or base.shape[1] != 3:
        raise ValueError(f"baseline 形状必须是 (K, 3)，实际 {base.shape}")
    if base.shape[0] != spec.key_count:
        raise ValueError(f"基线键数 {base.shape[0]} 与乐器 {spec.id} 的键数 {spec.key_count} 不符")
    if delta < 0:
        raise ValueError(f"delta 不能为负: {delta}")

    key_index = {key: i for i, key in enumerate(spec.key_order)}
    out: dict[str, KeyThreshold] = {}
    for area in spec.areas:
        indexes = [key_index[key] for key in area.keys if key in key_index]
        if not indexes:
            continue
        area_mean = base[indexes].mean(axis=0)
        if detection.metric == "channel":
            # 兼容旧语义：取该 area 候选通道里基线最亮的一条，用区间判据
            candidates = [c for c in area.channels if c in CHANNEL_INDEX] or list(detection.channels)
            channel = max(candidates, key=lambda c: float(area_mean[CHANNEL_INDEX[c]]))
            enter = max(1, min(254, int(round(float(area_mean[CHANNEL_INDEX[channel]] - delta)))))
            out[area.code] = _make_threshold(channel, enter)
            continue
        shift_from, shift_to = _chroma_axis(detection)
        out[area.code] = KeyThreshold(
            mode=_MODE_BY_METRIC.get(detection.metric, "euclid"),  # type: ignore[arg-type]
            channels=detection.channels,
            base_r=int(round(float(area_mean[0]))),
            base_g=int(round(float(area_mean[1]))),
            base_b=int(round(float(area_mean[2]))),
            delta=max(1, int(round(float(delta)))),
            shift_from=shift_from,
            shift_to=shift_to,
            weights=press_weights(detection),
            tolerance=int(getattr(detection, "tolerance", 0) or 0),
        )
    return out
