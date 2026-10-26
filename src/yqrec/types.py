"""核心数据类型（契约见 03 §1）。

设计原则：
- 全部 `frozen=True`，跨线程传递安全；
- 构造时校验不变式，非法状态尽早暴露；
- 不依赖 numpy / cv2，便于在无 GUI 环境做单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

# --------------------------------------------------------------------------- #
# 几何 / 捕获
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Rect 宽高必须为正: {self.width}x{self.height}")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def almost_16x9(self, tolerance: float = 0.02) -> bool:
        return abs(self.aspect - 16 / 9) <= tolerance


@dataclass(frozen=True, slots=True)
class ScreenTarget:
    """任意屏幕区域：游戏窗口 / 浏览器视频 / 播放器 / 桌面。"""

    monitor: int
    rect: Rect

    def __post_init__(self) -> None:
        if self.monitor < 0:
            raise ValueError("monitor 必须 >= 0")


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """绑定窗口：用于排除自身 HUD / 抓被遮挡窗口。"""

    hwnd: int
    norm: tuple[float, float, float, float]
    title: str | None = None
    """窗口标题（WGC 后端按标题绑定；hwnd 用于 DXGI/GDI 换算与 PrintWindow）。"""

    def __post_init__(self) -> None:
        if self.hwnd <= 0:
            raise ValueError("hwnd 必须为正")
        u0, v0, u1, v1 = self.norm
        if not (0.0 <= u0 < u1 <= 1.0 and 0.0 <= v0 < v1 <= 1.0):
            raise ValueError(f"归一化 ROI 非法: {self.norm!r}")


CaptureTarget = ScreenTarget | WindowTarget


@dataclass(frozen=True, slots=True)
class CaptureStats:
    fps: float
    dropped: int = 0
    last_error: str | None = None


# --------------------------------------------------------------------------- #
# 乐器 / 标定
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KeyThreshold:
    """判按阈值（两种语义，默认保持旧的"单通道区间"）。

    - `mode="channel"`（默认）：`min/max_*` 区间语义，`max_* == 0` 表示该通道不参与判定
      （通道 AND 语义）；
    - `mode="max_drop"`：用**通道最大跌幅**判按 ——
      `max(base_r-r, base_g-g, base_b-b) >= delta` 即视为按下。真机上白盘→棕盘的跌幅
      在 R/G/B 上分别是 -29/-46/-75，只盯基线最亮的 R 通道会永远不触发，所以改用此法；
    - `mode="chroma"`：用**通道对的相对偏移**判按 ——
      `(rgb[j]-rgb[i]) - (base_j-base_i) >= delta`，方向 `(i→j)` 由参考色推出。
      为什么需要它：半透明键帽会透出背景，背景越亮绝对跌幅越小（金黄花海实测只跌 28，
      同一乐器在暗背景下跌 69），`max_drop` 无法用同一个 Δ 覆盖两种背景；
      而"绿相对红变强"的偏移在两种背景下都是 34~46，Δ 可以统一。
    - `mode="weighted"`（v1.20）：**按参考色的变化方向加权** ——
      `Σ w_c·max(0, (base_c-cur_c) - tol) / Σ w_c >= delta`，权重由 refs 的跌幅分配
      （白→红时 G/B 权重大、R 几乎为 0），`tolerance` 是单通道死区，
      用来吃掉"各键基准色微小偏差"与压缩噪声。M3 计划里的"点采样 + 加权"方案。
    """

    min_r: int = 0
    max_r: int = 0
    min_g: int = 0
    max_g: int = 0
    min_b: int = 0
    max_b: int = 0
    mode: Literal["channel", "max_drop", "chroma", "euclid", "weighted"] = "channel"
    channels: tuple[str, ...] = ("R", "G", "B")
    base_r: int = -1
    base_g: int = -1
    base_b: int = -1
    delta: int = 0
    shift_from: int = 0
    """`chroma` 模式的基准通道下标（0=R,1=G,2=B）。"""
    shift_to: int = 1
    """`chroma` 模式的"变强"通道下标；偏移量 = `(to-from)_cur - (to-from)_base`。"""
    weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
    """`weighted` 模式的三通道权重（R,G,B 顺序），只作用在"变暗"方向。"""
    tolerance: int = 0
    """`weighted` 模式的死区：单通道变化小于该值视为噪声（灰阶）。"""

    def _pairs(self, rgb: tuple[int, int, int]) -> list[tuple[float, float]]:
        """`[(基线值, 当前值), ...]`，只取 `channels` 里且基线有效的通道。"""
        index = {"R": 0, "G": 1, "B": 2}
        bases = (self.base_r, self.base_g, self.base_b)
        out: list[tuple[float, float]] = []
        for channel in self.channels:
            idx = index.get(channel.strip().upper())
            if idx is None or bases[idx] < 0:
                continue
            out.append((float(bases[idx]), float(rgb[idx])))
        return out

    def max_drop(self, rgb: tuple[int, int, int]) -> int:
        """相对基线的通道最大跌幅（像素值域，可为负）。"""
        pairs = self._pairs(rgb)
        if not pairs:
            return 0
        return int(round(max(base - current for base, current in pairs)))

    def chroma_shift(self, rgb: tuple[int, int, int]) -> int:
        """`chroma` 判据：通道对的相对偏移（像素值域，可为负）。

        `(to - from)_当前 - (to - from)_基线` —— 背景整体变亮/变暗时两边的差同步平移，
        这个量几乎不变，所以同一 Δ 能跨背景工作。
        """
        bases = (self.base_r, self.base_g, self.base_b)
        i, j = self.shift_from, self.shift_to
        if not (0 <= i < 3 and 0 <= j < 3) or i == j or min(bases[i], bases[j]) < 0:
            return 0
        return int(round(float(rgb[j] - rgb[i]) - float(bases[j] - bases[i])))

    def euclid_distance(self, rgb: tuple[int, int, int]) -> float:
        """颜色偏离度：`||cur - base|| / sqrt(通道数)`（与单通道量纲可比）。"""
        pairs = self._pairs(rgb)
        if not pairs:
            return 0.0
        total = sum((base - current) ** 2 for base, current in pairs)
        return float((total / len(pairs)) ** 0.5)

    def weighted_score(self, rgb: tuple[int, int, int]) -> float:
        """`weighted` 判据：按权重投影"变暗幅度"，带逐通道死区。

        `score = Σ w_c·max(0, (base_c − cur_c) − tol) / Σ w_c`

        - 只累加**变暗**方向（按下方向），变亮不算；
        - `tolerance` 是死区：差值不到 tol 的通道贡献 0（吃掉各键基准色偏差与噪声）；
        - 除以 `Σ w_c` 让 score 仍是"灰阶"量级，Δ 与 `max_drop` 基本可比
          （同一次按下，加权值略小于 `max_drop`，因为它是对参考方向的最优投影）。
        """
        bases = (self.base_r, self.base_g, self.base_b)
        index = {"R": 0, "G": 1, "B": 2}
        tol = float(max(0, self.tolerance))
        numerator = 0.0
        weight_sum = 0.0
        for channel in self.channels:
            idx = index.get(channel.strip().upper())
            if idx is None or bases[idx] < 0:
                continue
            weight = float(self.weights[idx]) if idx < len(self.weights) else 1.0
            if weight <= 0.0:
                continue
            weight_sum += weight
            drop = float(bases[idx]) - float(rgb[idx]) - tol
            if drop > 0.0:
                numerator += weight * drop
        if weight_sum <= 0.0:
            return 0.0
        return numerator / weight_sum

    def channel_range(self, channel: str) -> tuple[int, int] | None:
        ch = channel.strip().upper()
        if ch == "R":
            return (self.min_r, self.max_r)
        if ch == "G":
            return (self.min_g, self.max_g)
        if ch == "B":
            return (self.min_b, self.max_b)
        return None

    def accepts(self, rgb: tuple[int, int, int], channels: tuple[str, ...] = ()) -> bool:
        """是否判定为"按下"。

        - `mode="max_drop"` / `"chroma"`：只看该判据的标量值，`channels` 参数被忽略；
        - 否则：通道 AND 语义，无有效通道时返回 False。
        """
        if self.mode == "max_drop":
            return self.max_drop(rgb) >= self.delta
        if self.mode == "chroma":
            return self.chroma_shift(rgb) >= self.delta
        if self.mode == "euclid":
            return self.euclid_distance(rgb) >= float(self.delta)
        if self.mode == "weighted":
            return self.weighted_score(rgb) >= float(self.delta)
        active = 0
        for ch in channels:
            rng = self.channel_range(ch)
            if rng is None or rng[1] <= 0:
                continue
            active += 1
            lo, hi = rng
            value = {"R": rgb[0], "G": rgb[1], "B": rgb[2]}[ch.strip().upper()]
            if not (lo <= value <= hi):
                return False
        return active > 0


@dataclass(frozen=True, slots=True)
class KeyArea:
    code: str
    name: str
    channels: tuple[str, ...]
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError(f"KeyArea {self.code} 未包含任何键")


@dataclass(frozen=True, slots=True)
class KeyPoint:
    """归一化键位：u,v ∈ [0,1]（相对帧尺寸）；r 以 1920 宽为基准归一化。"""

    u: float
    v: float
    r: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.u <= 1.0 and 0.0 <= self.v <= 1.0):
            raise ValueError(f"KeyPoint 坐标越界: {self}")
        if self.r <= 0:
            raise ValueError(f"KeyPoint 半径必须为正: {self.r}")

    def to_pixels(self, width: int, height: int) -> tuple[float, float, float]:
        """映射到实际帧像素坐标（半径按宽度缩放）。"""
        return (self.u * width, self.v * height, self.r * width)


@dataclass(frozen=True, slots=True)
class KeyGrid:
    points: tuple[KeyPoint, ...]
    key_count: int

    def __post_init__(self) -> None:
        if self.key_count <= 0:
            raise ValueError("key_count 必须为正")
        if len(self.points) != self.key_count:
            raise ValueError(
                f"KeyGrid 点数({len(self.points)}) 与 key_count({self.key_count}) 不符"
            )


@dataclass(frozen=True, slots=True)
class Circle:
    """Hough 检测结果（像素坐标，已加回 Y 偏移）。"""

    x: float
    y: float
    r: float


RING_OUTER_RATIO = 0.98
"""环形采样的外径（× 键半径）：留一点余量避开键帽边缘抗锯齿/描边。"""

DEFAULT_RING_WIDTH = 0.18
"""默认环带宽（× 键半径）→ 内径 0.80（`0.98 - 0.18`）。采样点就落在这条内环圆周上。"""

DEFAULT_SQUARE_SIDE = 1.1
"""默认小方块边长（× 键半径）→ 半边长（中心到四边）0.55R。"""

DEFAULT_RING_POINTS = 8
"""环形点采样：内环圆周上的分段点数（起点 = 内环上方水平切点，等角分布）。"""

DEFAULT_SQUARE_POINTS = 4
"""方块点采样：**每条边**的分段点数（起点 = 左上角，顺时针），总点数 = 4×该值。"""

DEFAULT_POINT_PATCH_RATIO = 0.06
"""每个基准点取多小一块圆斑的均值（× 键半径）：抗压缩噪声/抗锯齿，0 = 单像素。"""

DEFAULT_TOLERANCE = 8
"""`weighted` 判据的默认死区（灰阶）：吃掉各键基准色偏差与噪声。"""

DEFAULT_CONFIRM_FRAMES = 2
"""连续多少帧满足条件才确认按下（默认 2 = 至少两帧）。

v1.24 曾默认 1（"看到就认"），但延音录制要求"按住态确实持续"——单帧确认会让
一次闪帧/动画回弹被当成按下，进而把延音起点算早。M4 起默认 2。
事件时间戳仍记**首帧**，所以调帧数不挪谱面时序。
"""

DEFAULT_SUSTAIN_MS = 0
"""乐器延音阈值（毫秒）：`0` = 该乐器不延音；`>0` = 按下时长达到它才记录延音。

这是"乐器级"参数（`assets/instruments.json` 每个乐器的 `sustain_ms`），
与 `sustain: bool` 的旧写法兼容：只写了 `sustain: true` 时按 `LEGACY_SUSTAIN_MS` 处理。
"""

LEGACY_SUSTAIN_MS = 200
"""旧写法 `sustain: true`（没有 `sustain_ms`）时的延音阈值。"""

SUSTAIN_TEXT_MIN_MS = 50
"""谱面文本里延音后缀 `[ms]` 的写入下限：比它短的延音不写（与 01 §7.1 对齐）。"""

DEFAULT_KEY_PLAY_DELAY_MS = 3000
"""按键演奏的**起播延迟**（毫秒，乐器级参数 `assets/instruments.json` 的 `key_play_delay_ms`）。

为什么需要它：点「演奏」时前台必然是本程序，后台模式也常需要先把游戏窗口摆好。
留一段延迟，用户可以在倒计时期间把焦点交给游戏，延迟结束后才开始发键
（倒计时在下方的消息区显示"x 秒后开始演奏"）。0 = 立即开始。
"""

MAX_KEY_PLAY_DELAY_MS = 60_000
"""起播延迟的上限（60s）：再长基本是填错了，超出按上限截断。"""

RECORD_CLICK_OFF = 0
"""`record_click` 模式 0：录制启动前不做任何输入注入。"""

RECORD_CLICK_FOCUS_KEY = 1
"""`record_click` 模式 1：把框选区域所在窗口激活为前台后模拟一次空格键。

激活失败（GetForegroundWindow 复核不通过）坚决不发空格——空格会落回
本程序焦点控件（录制按钮），导致录制立即被误停（见 01 §15）。"""

RECORD_CLICK_MOUSE = 2
"""`record_click` 模式 2：在框选区域中心模拟一次鼠标左键点击（原有行为）。"""

DEFAULT_RECORD_CLICK_MODE = RECORD_CLICK_OFF
"""录制启动注入的默认模式（缺省关：注入输入属于副作用；发布用
instruments.json 会显式写 `record_click`）。"""


@dataclass(frozen=True, slots=True)
class SamplingSpec:
    """**全局**采样口径（`assets/instruments.json` 的 `sampling` 段，所有乐器统一使用）。

    两个旋钮，单位都是键半径 R 的倍数：
    - `ring_width_ratio`: 环形只采 `RING_OUTER_RATIO - width .. RING_OUTER_RATIO` 这一圈；
    - `square_side_ratio`: 小方块采边长 `side × R` 的正方形（居中于键心）。

    点采样的三个旋钮（v1.20）：
    - `ring_points`: 内环圆周上等角分段点数（起点 = 内环上方水平切点）；
    - `square_points`: 方块**每条边**的分段点数（起点 = 左上角，顺时针绕一圈）；
    - `point_patch_ratio`: 每个基准点取多大一块圆斑均值（0 = 单像素）。

    改 JSON 后运行时即时生效（`instruments.load_sampling()` 按 mtime 缓存重读）。
    """

    ring_width_ratio: float = DEFAULT_RING_WIDTH
    square_side_ratio: float = DEFAULT_SQUARE_SIDE
    ring_points: int = DEFAULT_RING_POINTS
    square_points: int = DEFAULT_SQUARE_POINTS
    point_patch_ratio: float = DEFAULT_POINT_PATCH_RATIO

    @property
    def ring_inner_ratio(self) -> float:
        """环形内径比例（× R）= 外径 − 环宽，夹在 [0, 外径) 内。"""
        inner = RING_OUTER_RATIO - self.ring_width_ratio
        return min(max(inner, 0.0), RING_OUTER_RATIO - 1e-3)

    @property
    def square_half_ratio(self) -> float:
        """小方块半边长（× R）——sampler 与预览层画框都用它。"""
        return max(self.square_side_ratio, 1e-3) / 2.0

    def with_ring_inner_ratio(self, inner: float) -> SamplingSpec:
        """按**内环半径**反推 `ring_width_ratio`（界面滑条：环形 = 内环半径）。"""
        value = min(max(float(inner), 0.05), RING_OUTER_RATIO - 0.01)
        return replace(self, ring_width_ratio=round(RING_OUTER_RATIO - value, 4))

    def with_square_half_ratio(self, half: float) -> SamplingSpec:
        """按**中心到四边的距离**反推 `square_side_ratio`（界面滑条：方块恒居中）。"""
        value = min(max(float(half), 0.05), 4.0)
        return replace(self, square_side_ratio=round(value * 2.0, 4))


@dataclass(frozen=True, slots=True)
class DetectionSpec:
    """按乐器配置的识别规范（`assets/instruments.json` 的 `detection` 段）。

    换乐器/换皮肤只改 JSON，不用改代码：
    - `metric`: `max_drop`（通道最大跌幅）/ `chroma`（通道对的相对色偏，抗背景亮度）/
      `euclid`（颜色欧氏偏离）/ `weighted`（按参考色方向加权，带死区）/
      `channel`（旧的单通道区间）
    - `channels`: 参与判据的通道子集
    - `default_delta`: 灵敏度 Δ（JSON 里直接写在乐器上层的 `delta` 字段，便于调试）
    - `unpressed_ref` / `pressed_ref`: 未按下/按下参考色（RGB，可为 None = 待实测）
    - `weights` / `tolerance`: 仅 `weighted` 用；权重缺省时由 refs 的跌幅自动推出
    - `confirm_frames`: 连续多少帧满足条件才确认按下（默认 2 = 至少两帧）
    """

    metric: Literal["max_drop", "chroma", "euclid", "weighted", "channel"] = "max_drop"
    channels: tuple[str, ...] = ("R", "G", "B")
    default_delta: int = 40
    unpressed_ref: tuple[int, int, int] | None = None
    pressed_ref: tuple[int, int, int] | None = None
    weights: tuple[float, float, float] | None = None
    tolerance: int = DEFAULT_TOLERANCE
    confirm_frames: int = DEFAULT_CONFIRM_FRAMES
    """连续 N 帧都满足按下条件才确认（默认 2 = 连续两帧都亮）。

    这是"帧数"而不是"时长"：`min_hold_ms` 那种时间闸在低帧率下会把门槛抬到 2 帧以上
    （一帧 50ms 时 50ms ≈ 1 帧，但实现上要等**下一帧**才确认 → 只占 1 帧的轻按必丢）。
    帧数语义与帧率解耦：`=1` 就是"看到就认"，`=2` 是"连续两帧都亮才算"。

    M4 起默认 2（服务延音：单帧闪一下不算按住）。代价是只占 1 帧的极轻按会漏，
    需要更灵敏时把某乐器调回 1；确认时刻晚一帧，但事件时间戳仍记首帧，
    所以谱面时序不受影响。
    """
    note: str = ""


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    id: str
    name: str
    key_count: int
    sustain: bool
    areas: tuple[KeyArea, ...]
    note_to_key: dict[str, str]
    note_to_audio: dict[str, str]
    hough: dict[str, float]
    button_radius: float
    thresholds: dict[str, KeyThreshold]
    detection: DetectionSpec = field(default_factory=DetectionSpec)
    sustain_ms: int = DEFAULT_SUSTAIN_MS
    """延音阈值（毫秒，`assets/instruments.json` 的乐器级 `sustain_ms`）。

    这是延音的**唯一真相**：`0` = 不延音；`>0` = 按下时长达到它才记录延音。
    `sustain` 布尔字段保留下来只是为了兼容旧调用方/旧 JSON，构造时会被本字段归一化
    （见 `__post_init__`）——判断"有没有延音"请用 `sustain`，取阈值请用本字段。
    """

    preview_only: bool = False
    """只在乐器下拉里可见、但**不可选**（`assets/instruments.json` 的乐器级 `preview_only`）。

    "看得见但选不了"：参数还没标定完（识别会误报/漏报）的乐器用它先占位，
    用户能看到支持哪些乐器，但不会误选进一个认不准的乐器。
    与"删掉这个乐器"的区别：删掉后旧标定档/旧谱面会打不开、音频资源也失去引用。
    等实机标定完成后把 JSON 里的 `preview_only` 去掉（或改 false），重启即恢复可选。
    """

    key_play_delay_ms: int = DEFAULT_KEY_PLAY_DELAY_MS
    """按键演奏的起播延迟（毫秒，`assets/instruments.json` 的乐器级 `key_play_delay_ms`）。

    每次"点「演奏」开始"和"暂停后继续"都先倒计时这么久，再真的发键 ——
    给用户时间把焦点交给游戏窗口。0 = 立即开始。
    """

    def __post_init__(self) -> None:
        if self.key_count <= 0:
            raise ValueError("key_count 必须为正")
        if len(self.note_to_key) != self.key_count:
            raise ValueError(f"{self.id}: note_to_key 数量与 key_count 不符")
        if len(self.note_to_audio) != self.key_count:
            raise ValueError(f"{self.id}: note_to_audio 数量与 key_count 不符")
        keys = set(self.note_to_key.values())
        if len(keys) != self.key_count:
            raise ValueError(f"{self.id}: 键盘字母存在重复")
        for area in self.areas:
            missing = set(area.keys) - keys
            if missing:
                raise ValueError(f"{self.id}/{area.code}: 含未知键 {sorted(missing)}")

        # 延音：`sustain_ms` 优先；只写了旧 `sustain: true` 时按 LEGACY_SUSTAIN_MS 兜底。
        threshold = int(self.sustain_ms)
        if threshold <= 0 and self.sustain:
            threshold = LEGACY_SUSTAIN_MS
        threshold = max(0, threshold)
        object.__setattr__(self, "sustain_ms", threshold)
        object.__setattr__(self, "sustain", threshold > 0)

        # 起播延迟：负数无意义按 0，上限 60s（再长就是填错了）
        delay = int(self.key_play_delay_ms)
        object.__setattr__(self, "key_play_delay_ms", min(MAX_KEY_PLAY_DELAY_MS, max(0, delay)))

    # -- 便捷访问 ---------------------------------------------------------- #

    @property
    def note_order(self) -> tuple[str, ...]:
        """note_to_key 的插入顺序（= 圆→音映射顺序）。"""
        return tuple(self.note_to_key.keys())

    @property
    def key_order(self) -> tuple[str, ...]:
        return tuple(self.note_to_key.values())

    def note_of_key(self, key: str) -> str:
        for note, k in self.note_to_key.items():
            if k == key:
                return note
        raise KeyError(f"{self.id}: 未知键 {key!r}")

    def area_of_key(self, key: str) -> KeyArea:
        for area in self.areas:
            if key in area.keys:
                return area
        raise KeyError(f"{self.id}: 键 {key!r} 不属于任何 KeyArea")

    def threshold_of_key(self, key: str) -> KeyThreshold:
        return self.thresholds[self.area_of_key(key).code]


# --------------------------------------------------------------------------- #
# 采样 / 事件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RgbSample:
    ts: float
    rgb: tuple[tuple[int, int, int], ...]

    def __post_init__(self) -> None:
        if not self.rgb:
            raise ValueError("RgbSample 不能为空")


@dataclass(frozen=True, slots=True)
class PressEvent:
    key: str
    down: bool
    ts: float
    delta: float = 0.0
    """这次按下的**判据量**（Δ，与 `KeyThreshold.delta` 同口径、同单位）。

    只给"谱面自校验"（平均/最小 Δ）与试听回放显示用，**不参与时间轴**：
    抬起事件、`flush()` 补发的抬起都是 0。非录制来源的谱面没有这个值（0）。
    """


@dataclass(frozen=True, slots=True)
class NoteEvent:
    keys: tuple[str, ...]
    onset_ms: float
    sustain_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("NoteEvent.keys 不能为空")
        if len(set(self.keys)) != len(self.keys):
            raise ValueError(f"NoteEvent 含重复键: {self.keys}")
        if self.onset_ms < 0 or self.sustain_ms < 0:
            raise ValueError("时间不能为负")


@dataclass(frozen=True, slots=True)
class RestEvent:
    ms: float

    def __post_init__(self) -> None:
        if self.ms < 0:
            raise ValueError("RestEvent.ms 不能为负")


ScoreEvent = NoteEvent | RestEvent


@dataclass(frozen=True, slots=True)
class Diagnostic:
    message: str
    offset: int
    length: int
    level: Literal["error", "warning"] = "warning"

    @property
    def is_error(self) -> bool:
        return self.level == "error"


@dataclass(slots=True)
class Score:
    """一首曲子的解析结果（可变容器，便于编辑器增量更新）。"""

    events: list[ScoreEvent] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        total = 0.0
        for e in self.events:
            total += e.ms if isinstance(e, RestEvent) else 0.0
        if self.events and isinstance(self.events[-1], NoteEvent):
            total += self.events[-1].sustain_ms
        return total

    @property
    def note_count(self) -> int:
        return sum(1 for e in self.events if isinstance(e, NoteEvent))


# --------------------------------------------------------------------------- #
# 配置档 / 会话
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Profile:
    """识别源配置档（02/01 §8.4）。"""

    name: str
    target: CaptureTarget
    instrument: str
    backend: str = "auto"
    ref_size: tuple[int, int] = (1920, 1080)
    keygrid: KeyGrid | None = None
    thresholds: dict[str, KeyThreshold] = field(default_factory=dict)
    baseline: tuple[tuple[int, int, int], ...] | None = None
    """**逐键**基线色（学基线时每键各自的 RGB 均值）。

    判按一律用它当基准（v1.20 起）：区域均值会把"被角色挡住/背景更暗"的键
    算成常亮（风物之诗实测 R/V 偏移 57 灰阶），逐键基线则天然抵消这种空间差异。
    旧配置档没有这一段 → 退回 `thresholds.base_*`（区域均值）。
    """
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "instrument": self.instrument,
            "backend": self.backend,
            "ref_size": list(self.ref_size),
            "created": self.created or datetime.now().isoformat(timespec="seconds"),
            "target": _target_to_dict(self.target),
            "keygrid": (
                None
                if self.keygrid is None
                else {
                    "points": [[p.u, p.v, p.r] for p in self.keygrid.points],
                    "key_count": self.keygrid.key_count,
                }
            ),
            "baseline": (
                None
                if self.baseline is None
                else [[int(v) for v in rgb] for rgb in self.baseline]
            ),
            "thresholds": {
                code: {
                    "min_r": t.min_r, "max_r": t.max_r,
                    "min_g": t.min_g, "max_g": t.max_g,
                    "min_b": t.min_b, "max_b": t.max_b,
                    # max_drop 语义（真机白盘→棕盘）：不存这几个字段的话，
                    # 重启恢复出来的阈值会退化成"全 0 通道"，等于没阈值。
                    "mode": t.mode,
                    "base_r": t.base_r, "base_g": t.base_g, "base_b": t.base_b,
                    "delta": t.delta,
                    "weights": list(t.weights),
                    "tolerance": t.tolerance,
                }
                for code, t in self.thresholds.items()
            },
        }


def _target_to_dict(target: CaptureTarget) -> dict[str, Any]:
    if isinstance(target, ScreenTarget):
        return {
            "kind": "screen",
            "monitor": target.monitor,
            "rect": [target.rect.left, target.rect.top, target.rect.width, target.rect.height],
        }
    return {"kind": "window", "hwnd": target.hwnd, "norm": list(target.norm)}


def target_from_dict(data: dict[str, Any]) -> CaptureTarget:
    kind = data.get("kind")
    if kind == "screen":
        left, top, width, height = data["rect"]
        return ScreenTarget(
            int(data.get("monitor", 0)), Rect(int(left), int(top), int(width), int(height))
        )
    if kind == "window":
        u0, v0, u1, v1 = data["norm"]
        return WindowTarget(int(data["hwnd"]), (float(u0), float(v0), float(u1), float(v1)))
    raise ValueError(f"未知 target kind: {kind!r}")


@dataclass(slots=True)
class SessionMeta:
    version: int
    target: CaptureTarget
    instrument: str
    backend: str
    fps: float
    dropped: int
    started_at: str
    duration_ms: float
    profile: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target": _target_to_dict(self.target),
            "instrument": self.instrument,
            "profile": self.profile,
            "backend": self.backend,
            "fps": self.fps,
            "dropped": self.dropped,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }


__all__ = [
    "CaptureStats",
    "CaptureTarget",
    "Circle",
    "DetectionSpec",
    "Diagnostic",
    "InstrumentSpec",
    "KeyArea",
    "KeyGrid",
    "KeyPoint",
    "KeyThreshold",
    "NoteEvent",
    "PressEvent",
    "Profile",
    "Rect",
    "RestEvent",
    "RgbSample",
    "Score",
    "ScoreEvent",
    "ScreenTarget",
    "SessionMeta",
    "WindowTarget",
    "target_from_dict",
]
