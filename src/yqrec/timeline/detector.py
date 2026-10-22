"""每键判按状态机（03 §3.4）：施密特触发 + 毫秒去抖，帧率无关。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..types import (
    DEFAULT_CONFIRM_FRAMES,
    InstrumentSpec,
    KeyThreshold,
    PressEvent,
)


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    min_hold_ms: float = 0.0
    """可选的**绝对时长**下限（`0` = 不启用，默认）。

    与"确认帧数"是两回事：帧数由**乐器**决定（`detection.confirm_frames`，默认 2），
    这里只做"无论帧率多高，至少按住这么久"的补充限制，一般留 0。
    为什么不靠它去抖：`min_hold > 0` 的语义是"必须在**后续某一帧**仍然满足"，
    低帧率下等价于至少 2 帧可见 —— 只占 1 帧的轻按会被整场丢掉（M2q.4 的教训）。
    去抖请改用 `confirm_frames`（帧数语义与帧率解耦）。
    """

    min_gap_ms: float = 30.0
    """同键两次按下之间的最小间隔。"""

    hysteresis: float = 0.15
    """抬起阈值 = enter * (1 + hysteresis)，形成滞回带。"""


@dataclass(slots=True)
class _KeyState:
    down: bool = False
    down_ts: float = 0.0
    pending_since: float | None = None
    last_up_ts: float = float("-inf")
    seen: int = 0
    """本次按下累计被"看到"的帧数（用于统计轻按/漏检）。"""


class PressDetector:
    """`update()` 输入 (K,3) RGB 与时间戳（秒），输出按下/抬起事件。

    时间戳语义：事件记在**首次满足条件的时刻**，而非确认时刻，避免整体偏移。

    `baseline`（可选，(K,3)）：**逐键**基线色。给了就用它替换阈值里的区域均值，
    因为区域均值会把"被角色遮挡 / 背景更暗"的键算成常按（风物之诗实测偏 57 灰阶）。
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        thresholds: dict[str, KeyThreshold] | None = None,
        cfg: DetectorConfig | None = None,
        baseline: Any = None,
    ) -> None:
        self._cfg = cfg or DetectorConfig()
        self._spec = spec
        self._thresholds = thresholds or spec.thresholds
        self._keys = spec.key_order
        self._state = [_KeyState() for _ in self._keys]
        self._confirm_frames = max(
            1, int(getattr(spec.detection, "confirm_frames", DEFAULT_CONFIRM_FRAMES))
        )
        """连续多少帧满足条件才确认（来自乐器 `detection.confirm_frames`，默认 2）。"""
        self._short_presses = 0
        """刚好用满 `confirm_frames` 帧就确认的按下次数（= 本次最轻的那类按下）。

        `confirm_frames=1` 时它就是"只占 1 帧"的计数；调大后可用于观察轻按的帧数分布。
        默认 2 时它统计"刚好两帧就确认"的按下（最轻的那一类）。
        """
        per_key = _per_key_baseline(spec, self._keys, baseline)
        plan: list[tuple[KeyThreshold, tuple[str, ...]]] = []
        for index, key in enumerate(self._keys):
            area = spec.area_of_key(key)
            threshold = self._thresholds[area.code]
            if per_key is not None:
                r, g, b = per_key[index]
                threshold = replace(
                    threshold, base_r=int(r), base_g=int(g), base_b=int(b)
                )
            plan.append((threshold, area.channels))
        self._plan = plan

    # -- 内部 ------------------------------------------------------------- #

    def _limits(self, thr: KeyThreshold, channels: tuple[str, ...]) -> tuple[int, int] | None:
        """返回 (enter, exit)；无有效通道时 None。"""
        active = [c for c in channels if (rng := thr.channel_range(c)) and rng[1] > 0]
        if not active:
            return None
        enters = [thr.channel_range(c)[1] for c in active]  # type: ignore[index]
        enter = max(enters)
        exit_v = min(255.0, enter * (1.0 + self._cfg.hysteresis))
        return int(enter), int(exit_v)

    def _signals(
        self, rgb: tuple[int, int, int], thr: KeyThreshold, channels: tuple[str, ...]
    ) -> tuple[bool, bool] | None:
        """返回 (可按下, 可保持按下)。"""
        if thr.mode in ("max_drop", "chroma", "euclid", "weighted"):
            # 颜色偏离语义：标量偏离 >= Δ 按下，回弹 15% 才抬起。
            # 三种模式各自的"偏离量"必须在这里分派 —— 漏掉 chroma 会掉进单通道区间分支，
            # 而 chroma 阈值没设 min/max → 返回 None → 整场永远判不出按下（M2n 的 bug）。
            if thr.mode == "max_drop":
                value = float(thr.max_drop(rgb))
            elif thr.mode == "chroma":
                value = float(thr.chroma_shift(rgb))
            elif thr.mode == "weighted":
                value = float(thr.weighted_score(rgb))
            else:
                value = float(thr.euclid_distance(rgb))
            enter = float(thr.delta)
            return (value >= enter, value >= enter * (1.0 - self._cfg.hysteresis))
        limits = self._limits(thr, channels)
        if limits is None:
            return None
        enter, exit_v = limits
        can_press = True
        keep = True
        for ch in channels:
            rng = thr.channel_range(ch)
            if rng is None or rng[1] <= 0:
                continue
            value = {"R": rgb[0], "G": rgb[1], "B": rgb[2]}[ch.strip().upper()]
            if value > enter:
                can_press = False
            if value > exit_v:
                keep = False
        return can_press, keep

    # -- 公开 API ---------------------------------------------------------- #

    def update(self, rgb, ts: float) -> list[PressEvent]:
        """`rgb`: (K,3) 序列（支持 list/np.ndarray）。"""
        cfg = self._cfg
        out: list[PressEvent] = []
        for i, key in enumerate(self._keys):
            thr, channels = self._plan[i]
            sig = self._signals((int(rgb[i][0]), int(rgb[i][1]), int(rgb[i][2])), thr, channels)
            if sig is None:
                continue
            can_press, keep = sig
            st = self._state[i]

            if not st.down:
                if can_press:
                    if st.pending_since is None:
                        st.pending_since = ts
                        st.seen = 0
                    st.seen += 1
                    if (
                        st.seen >= self._confirm_frames
                        and ts - st.pending_since >= cfg.min_hold_ms / 1000.0
                    ):
                        if st.pending_since - st.last_up_ts >= cfg.min_gap_ms / 1000.0:
                            st.down = True
                            st.down_ts = st.pending_since  # 时间戳仍记首帧，确认晚一帧不挪时序
                            if st.seen <= self._confirm_frames:
                                self._short_presses += 1
                            out.append(PressEvent(key=key, down=True, ts=st.down_ts))
                        else:
                            # 距上次抬起还不够 `min_gap`：把计时起点推到本帧重新数。
                            # 否则 `pending_since` 与 `last_up_ts` 都不再变化 → 条件永远不成立，
                            # **一直按着也永远判不出按下**（快速连击时第二个音整个丢失，不是延迟）。
                            # 新语义：连击太快就"等够间隔再认"，而不是丢弃。
                            st.pending_since = ts
                            st.seen = 1  # 本帧已经数过一次，作为新一轮的第 1 帧
                else:
                    st.pending_since = None
            else:
                if not keep:
                    st.down = False
                    st.pending_since = None
                    st.last_up_ts = ts
                    out.append(PressEvent(key=key, down=False, ts=ts))
        return out

    def flush(self, ts: float) -> list[PressEvent]:
        """录制结束时释放所有仍按下的键。"""
        out: list[PressEvent] = []
        for i, key in enumerate(self._keys):
            st = self._state[i]
            if st.down:
                st.down = False
                st.last_up_ts = ts
                out.append(PressEvent(key=key, down=False, ts=ts))
        return out

    @property
    def confirm_frames(self) -> int:
        """本乐器生效的"确认帧数"（调试时可和 `instruments.json` 对账）。"""
        return self._confirm_frames

    @property
    def short_presses(self) -> int:
        """本次会话里"只占 1 帧"的按下次数（0 说明帧率/闸门没在吃音）。"""
        return self._short_presses

    def reset(self) -> None:
        self._state = [_KeyState() for _ in self._keys]


def _per_key_baseline(
    spec: InstrumentSpec, keys: tuple[str, ...], baseline: Any
) -> list[tuple[int, int, int]] | None:
    """把 `(K,3)` 基线规整成 `[(r,g,b), ...]`；形状不符就返回 None（退回区域均值）。"""
    if baseline is None:
        return None
    try:
        rows = [tuple(row)[:3] for row in baseline]
    except TypeError:
        return None
    if len(rows) != len(keys):
        return None
    out: list[tuple[int, int, int]] = []
    for row in rows:
        if len(row) != 3:
            return None
        out.append(tuple(max(0, min(255, int(round(float(v))))) for v in row))  # type: ignore[arg-type]
    return out
