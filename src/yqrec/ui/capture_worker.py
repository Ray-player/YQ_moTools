"""抓帧 → 采样 → 判按 的后台线程（GUI 主线程只读信号）。

线程边界（03 §7）：**只有**本线程碰捕获后端与 `KeySampler`/`PressDetector`；
主线程通过 Qt Signal 拿 `QImage` 预览与 `(K,3)` 采样快照，绝不直接调捕获。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from ..capture.manager import CaptureManager, backend_label
from ..logging import get_logger
from ..recording import Recording, RecordingBuffer
from ..timeline.detector import DetectorConfig, PressDetector
from ..types import (
    DEFAULT_POINT_PATCH_RATIO,
    DEFAULT_RING_POINTS,
    DEFAULT_SQUARE_POINTS,
    DEFAULT_SQUARE_SIDE,
    CaptureTarget,
    InstrumentSpec,
    KeyGrid,
    KeyThreshold,
    PressEvent,
)
from ..vision.pointsampler import KeyPointSampler
from ..vision.sampler import KeySampler

_log = get_logger("ui.capture_worker")

PREVIEW_EVERY = 3
"""每 N 帧发一次预览图（60fps 源 → 20fps 预览，省主线程开销，给分析留余量）。"""

SAMPLES_EVERY = 3
"""每 N 帧发一次采样快照（给右侧灯板显示 RGB/Δ）。"""

STATUS_EVERY_S = 0.5
"""状态栏刷新间隔（秒）。"""

DEFAULT_BASELINE_S = 1.5
"""学习基线的采样时长（秒）。"""

BASELINE_MIN_FRAMES = 8
"""基线窗口内至少要采到的帧数（画面静止时 DXGI 出帧很慢，太少会学到脏帧）。"""

BASELINE_MAX_S = 6.0
"""基线等待上限（秒）：凑不满最小帧数也到此为止，避免界面假死。"""

WAITING_FRAMES = 30
"""连续这么多次拿不到新帧（≈0.6s）就认为"后端在等新帧"，状态栏提示。"""

REPUBLISH_INTERVAL_S = 0.5
"""等待新帧期间重发上一帧的间隔（秒），保证画面不黑屏。"""


def to_qimage(frame: np.ndarray) -> QImage:
    """BGR ndarray → QImage（**必须 copy**：否则 numpy buffer 释放后花屏）。

    注意：`gdi`(mss) 后端返回的是 `[:, :, :3]` 这种**非连续切片**，直接交给 `QImage`
    会抛 `BufferError: underlying buffer is not C-contiguous`，所以先转成连续内存。
    """
    if not frame.flags["C_CONTIGUOUS"]:
        frame = np.ascontiguousarray(frame)
    height, width = frame.shape[:2]
    image = QImage(
        frame.data, width, height, int(frame.strides[0]), QImage.Format.Format_BGR888
    )
    return image.copy()


def from_qimage(image: QImage) -> np.ndarray:
    """QImage → BGR ndarray（自动检测键位时复用预览里的最后一帧）。"""
    converted = image.convertToFormat(QImage.Format.Format_BGR888)
    width = converted.width()
    height = converted.height()
    stride = converted.bytesPerLine()
    buffer = np.frombuffer(converted.constBits(), dtype=np.uint8, count=stride * height)
    return buffer.reshape(height, stride)[:, : width * 3].reshape(height, width, 3).copy()


class CaptureWorker(QThread):
    """后台抓帧线程。

    对外只暴露 `configure/update_grid/update_thresholds/request_baseline/stop`，
    其余全部在 `run()` 内单线程执行。
    """

    frame_ready = Signal(QImage)
    samples_ready = Signal(object)
    press_ready = Signal(object)
    status_ready = Signal(object)
    backend_fallback = Signal(str)
    """用户指定了后端却回退到别的后端（如 dxgi 打不开 → gdi），带原因说明。"""
    baseline_ready = Signal(object)
    baseline_started = Signal()
    record_progress = Signal(object)
    """录制中每来一个事件发一次：`{"text", "notes", "frames", "duration_ms"}`。"""
    error = Signal(str)
    stopped = Signal(str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._judge_lock = threading.Lock()
        """只保护"判按器状态"（`update` / `flush`）。

        分析线程每帧调 `update`，主线程 `stop_recording` 要 `flush` 一次；
        不加锁的话收尾可能与某一帧的判定交错，导致同一个键抬起两次/延音算错。
        锁内只做纯内存运算（不 emit 信号），不会死锁。
        """
        self._running = False
        self._target: CaptureTarget | None = None
        self._spec: InstrumentSpec | None = None
        self._grid: KeyGrid | None = None
        self._thresholds: dict[str, KeyThreshold] | None = None
        self._baseline: Any = None
        self._detector_cfg = DetectorConfig()
        """判按器配置（来自 `settings.json` 的 `record` 段：min_hold / min_gap）。"""
        """逐键基线色 `(K,3)`（学基线时写入；判按优先用它当基准）。"""
        self._backend = "auto"
        self._target_fps = 60
        self._baseline_request_s: float | None = None
        self._rec: RecordingBuffer | None = None

        self._sampler: KeySampler | KeyPointSampler | None = None
        self._sampler_size: tuple[int, int] = (0, 0)
        self._sample_inner_ratio = 0.0
        """环形采样内径比例（0 = 走小方块）；由 UI 按「检测方式」+ 全局 sampling 设置。"""
        self._sample_square_side = DEFAULT_SQUARE_SIDE
        """小方块边长（× 键半径）；来自全局 sampling.square_side_ratio。"""
        self._sample_offset_y = 0.0
        """方块模式纵向偏移（1920 宽基准像素）。"""
        self._sample_point_sampling = False
        """True = 分段点采样（`KeyPointSampler`），False = 整块/整环均值（`KeySampler`）。"""
        self._sample_ring_points = DEFAULT_RING_POINTS
        self._sample_square_points = DEFAULT_SQUARE_POINTS
        self._sample_patch_ratio = DEFAULT_POINT_PATCH_RATIO
        self._detector: PressDetector | None = None
        self._down: list[bool] = []
        self._frames = 0
        self._backend_name = "-"
        self._last_frame: np.ndarray | None = None
        self._src_ts: deque[float] = deque(maxlen=31)
        """最近若干帧的**采集时间戳**（后端的 ts），用来算"源到底几帧"。"""

    # -- 主线程 API -------------------------------------------------------- #

    def configure(
        self,
        target: CaptureTarget,
        spec: InstrumentSpec,
        grid: KeyGrid | None,
        *,
        thresholds: dict[str, KeyThreshold] | None = None,
        backend: str = "auto",
        target_fps: int = 60,
        baseline: Any = None,
        detector_cfg: DetectorConfig | None = None,
    ) -> None:
        """配置捕获参数；`grid=None` 表示"先只看画面"，键位可后续热更新。"""
        self._target = target
        self._spec = spec
        self._grid = grid
        self._thresholds = thresholds
        self._baseline = baseline
        self._detector_cfg = detector_cfg or DetectorConfig()
        self._backend = backend
        self._target_fps = max(1, int(target_fps))
        self._detector = None

    def set_keygrid(self, grid: KeyGrid | None) -> None:
        """热更新键位：重建采样器并清空判按状态（键位一变，旧的按下状态就没意义）。

        `grid=None` 表示"清空键位"（重置检测），预览继续跑但不再采样/判按。
        """
        with self._lock:
            self._grid = grid
            self._sampler = None
            self._sampler_size = (0, 0)
            self._detector = None
            key_count = 0 if self._spec is None else self._spec.key_count
        self._down = [False] * (grid.key_count if grid is not None else key_count)
        self.press_ready.emit(list(self._down))

    def update_grid(self, grid: KeyGrid) -> None:
        """`set_keygrid` 的旧名字（保持既有调用方不破）。"""
        self.set_keygrid(grid)

    def latest_frame(self) -> np.ndarray | None:
        """最近一帧 BGR 的副本（供"自动检测键位"取静帧用；无帧返回 None）。"""
        with self._frame_lock:
            frame = self._last_frame
        return None if frame is None else frame.copy()

    def update_thresholds(
        self,
        thresholds: dict[str, KeyThreshold] | None,
        baseline: Any = None,
    ) -> None:
        """热更新阈值 + 逐键基线（学完基线 / 改 Δ）：重建判按器，保留已判到的按下状态。"""
        with self._lock:
            self._thresholds = thresholds
            self._baseline = baseline
            self._detector = None


    def set_spec(self, spec: InstrumentSpec) -> None:
        """热换乐器参数（F5 重读 / 换乐器）：重建判按器。

        `confirm_frames`、`delta`、`weights` 都是判按器构造时读进去的，不重建就不会生效
        （以前 F5 只换了界面上的 spec，后台还在用旧参数 —— 调 confirm_frames 会"按了没反应"）。
        重建会丢掉当前按下状态：录制中途按 F5 时，已按下的键不会补一次抬起事件。
        M4 起延音也记进谱面（`A[800]`），所以"丢掉按下态"不再只影响时长 —— 会直接
        丢掉这段延音。录制中请勿改参数（UI 侧已在录制期间锁住相关控件）。
        """
        with self._lock:
            if spec is self._spec:
                return
            self._spec = spec
            self._detector = None

    def set_sampler_options(
        self,
        *,
        inner_ratio: float,
        square_side: float = DEFAULT_SQUARE_SIDE,
        offset_y: float = 0.0,
        point_sampling: bool = False,
        ring_points: int = DEFAULT_RING_POINTS,
        square_points: int = DEFAULT_SQUARE_POINTS,
        patch_ratio: float = DEFAULT_POINT_PATCH_RATIO,
    ) -> None:
        """切换"采哪块像素"：环形（inner_ratio>0）/ 方块（0）+ 方块边长 + 纵向偏移，
        以及是否用**分段点采样**（v1.20）与它的分段数。

        参数变了就丢掉缓存的 sampler，下一帧按新口径重建（尺寸不变也重建）。
        """
        inner_ratio = max(0.0, float(inner_ratio))
        square_side = max(0.05, float(square_side))
        offset_y = float(offset_y)
        point_sampling = bool(point_sampling)
        ring_points = max(1, int(ring_points))
        square_points = max(1, int(square_points))
        patch_ratio = max(0.0, float(patch_ratio))
        with self._lock:
            if (
                abs(inner_ratio - self._sample_inner_ratio) < 1e-6
                and abs(square_side - self._sample_square_side) < 1e-6
                and abs(offset_y - self._sample_offset_y) < 1e-6
                and point_sampling == self._sample_point_sampling
                and ring_points == self._sample_ring_points
                and square_points == self._sample_square_points
                and abs(patch_ratio - self._sample_patch_ratio) < 1e-6
            ):
                return
            self._sample_inner_ratio = inner_ratio
            self._sample_square_side = square_side
            self._sample_offset_y = offset_y
            self._sample_point_sampling = point_sampling
            self._sample_ring_points = ring_points
            self._sample_square_points = square_points
            self._sample_patch_ratio = patch_ratio
            self._sampler = None
            self._sampler_size = (0, 0)

    def request_baseline(self, seconds: float = DEFAULT_BASELINE_S) -> None:
        with self._lock:
            self._baseline_request_s = max(0.1, float(seconds))

    # -- 录制（M2） -------------------------------------------------------- #

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._rec is not None

    def start_recording(self) -> bool:
        """开始攒谱面；`False` 表示条件不足（还没配乐器/键位）。"""
        with self._lock:
            if self._spec is None or self._grid is None:
                return False
            self._rec = RecordingBuffer(self._spec)
            return True

    def stop_recording(self) -> Recording | None:
        """结束录制并返回结果（主线程调用；当时没在录就返回 None）。

        只在锁内摘走缓冲，装配在锁外做，避免长时间阻塞分析线程。

        收尾时会用**最后一帧的时间戳**给还按着的键补一次抬起（`PressDetector.flush`）：
        否则那些键的按住时长只能算到"最后一个事件"那一帧，正在延音的音会被截短
        （M4 要点：停止识谱时按当前已按下的毫秒数记延音）。
        """
        flushed: list[PressEvent] = []
        with self._lock:
            rec, self._rec = self._rec, None
            if rec is not None:
                rec.close()  # 先封口再摘走：保证分析线程不会往收尾中的缓冲追加
        if rec is None:
            return None

        # 收尾：还按着的键按"最后一帧"补一次抬起，延音才能算到停止这一刻。
        detector = self._detector
        ts = rec.last_frame_ts
        if detector is not None and ts is not None:
            with self._judge_lock:
                flushed = detector.flush(ts)
            if flushed:
                _log.info("录制结束时仍有 %d 个键按住，已按结束时刻补抬起", len(flushed))
        detector = self._detector
        if detector is not None and detector.short_presses:
            _log.info(
                "本次录制有 %d 次按下刚好用满 %d 帧就确认（最轻的一类短按）",
                detector.short_presses,
                detector.confirm_frames,
            )
        return rec.finish(flushed)

    def stop(self) -> None:
        """请求退出（主线程调用后应 `wait()`）。"""
        self._running = False

    # -- 线程体 ------------------------------------------------------------ #

    def run(self) -> None:  # noqa: C901 - 单线程主循环，线性流程更易读
        manager = CaptureManager()
        if self._target is None or self._spec is None:
            self.error.emit("未配置捕获参数")
            return
        if self._down == [] and self._spec is not None:
            self._down = [False] * self._spec.key_count
        try:
            manager.start(self._target, prefer=self._backend, target_fps=self._target_fps)
            self._backend_name = getattr(getattr(manager, "_current", None), "name", "?")
            requested = (self._backend or "auto").strip()
            if requested not in ("auto", "") and self._backend_name != requested:
                reasons = "; ".join(manager.start_errors) or "未知原因"
                _log.warning("后端回退: 请求 %s → 实际 %s（%s）", requested, self._backend_name, reasons)
                self.backend_fallback.emit(
                    f"「{backend_label(requested)}」打不开，已回退到 {backend_label(self._backend_name)}：{reasons}"
                )
            self._running = True
            self._loop(manager)
        except Exception as exc:  # 后端打不开 / 用户关窗口 / 驱动异常
            _log.exception("捕获线程失败: %s", exc)
            self.error.emit(str(exc))
        finally:
            self._running = False
            try:
                manager.stop()
            finally:
                self.stopped.emit(self._backend_name)

    def _loop(self, manager: CaptureManager) -> None:
        fps_window: deque[float] = deque(maxlen=60)
        ms_ema = 0.0
        last_status = 0.0
        baseline_seconds: float | None = None
        baseline_acc: list[np.ndarray] = []
        baseline_started = 0.0
        idle = 0
        last_republish = 0.0

        while self._running:
            request = self._take_baseline_request()
            if request is not None and baseline_seconds is None:
                baseline_seconds = request
                baseline_acc = []
                baseline_started = time.perf_counter()
                self.baseline_started.emit()

            got = manager.latest(timeout_ms=20)
            if got is None:
                idle += 1
                now = time.perf_counter()
                if idle >= WAITING_FRAMES and now - last_republish >= REPUBLISH_INTERVAL_S:
                    # 后端迟迟不出新帧（DXGI 静止画面）：重发上一帧，避免画面看着像冻死
                    with self._frame_lock:
                        cached = self._last_frame
                    if cached is not None:
                        self.frame_ready.emit(to_qimage(cached))
                        last_republish = now
                time.sleep(0.001)
                last_status = self._emit_status(
                    fps_window, ms_ema, last_status, waiting=idle >= WAITING_FRAMES
                )
                continue

            idle = 0
            frame, _ts = got
            self._src_ts.append(float(_ts))
            started = time.perf_counter()
            with self._frame_lock:
                self._last_frame = frame  # 供「自动检测键位」取静帧（只存引用，不拷贝）
            with self._lock:
                has_grid = self._grid is not None
            sampler = self._ensure_sampler(frame) if has_grid else None
            if sampler is None:
                # 无键位（或采样器建不起来）：只出画面 + 状态，不采样、不判按
                self._frames += 1
                fps_window.append(started)
                if self._frames % PREVIEW_EVERY == 0:
                    self.frame_ready.emit(to_qimage(frame))
                last_status = self._emit_status(fps_window, ms_ema, last_status)
                continue
            try:
                rgb = sampler.sample(frame)
            except Exception as exc:  # 尺寸竞态等，跳过该帧
                _log.warning("采样失败（跳过该帧）: %s", exc)
                continue

            self._frames += 1
            fps_window.append(started)
            ms = (time.perf_counter() - started) * 1000.0
            ms_ema = ms if ms_ema <= 0 else 0.9 * ms_ema + 0.1 * ms

            if self._frames % PREVIEW_EVERY == 0:
                self.frame_ready.emit(to_qimage(frame))
            if self._frames % SAMPLES_EVERY == 0:
                self.samples_ready.emit(rgb)

            with self._lock:
                rec = self._rec
            if rec is not None:
                rec.push_frame(_ts)
            events = self._judge(rgb, _ts)
            if rec is not None and events:
                rec.push_events(events)
                self.record_progress.emit(rec.snapshot())

            if baseline_seconds is not None:
                baseline_acc.append(rgb.astype(np.float32))
                elapsed = time.perf_counter() - baseline_started
                enough_frames = len(baseline_acc) >= BASELINE_MIN_FRAMES
                if (elapsed >= baseline_seconds and enough_frames) or elapsed >= BASELINE_MAX_S:
                    mean = np.mean(np.stack(baseline_acc), axis=0).astype(np.float32)
                    _log.info("基线采样: %d 帧 / %.2fs", len(baseline_acc), elapsed)
                    baseline_seconds = None
                    baseline_acc = []
                    self.baseline_ready.emit(mean)

            last_status = self._emit_status(fps_window, ms_ema, last_status)

    # -- 内部 -------------------------------------------------------------- #

    def _take_baseline_request(self) -> float | None:
        with self._lock:
            request, self._baseline_request_s = self._baseline_request_s, None
        return request

    def _judge(self, rgb: np.ndarray, ts: float) -> list[PressEvent]:
        """判按（施密特触发已含 min_hold 去抖）；无阈值时状态灯保持全灰。

        返回本帧事件（录制要用）；时间戳用后端帧 `ts`（与录制帧序列同源），
        不再用循环时刻，否则事件与帧之间会有几毫秒漂移。
        """
        if self._thresholds is None:
            return []
        if self._detector is None:
            self._detector = PressDetector(
                self._spec,
                self._thresholds,
                cfg=self._detector_cfg,
                baseline=self._baseline,
            )
        with self._judge_lock:
            events = self._detector.update(rgb, ts)
        if not events:
            return []
        keys = self._spec.key_order if self._spec is not None else ()
        index_of = {key: i for i, key in enumerate(keys)}
        for event in events:
            index = index_of.get(event.key)
            if index is not None:
                self._down[index] = bool(event.down)
        self.press_ready.emit(list(self._down))
        return events

    def _ensure_sampler(self, frame: np.ndarray) -> KeySampler | KeyPointSampler | None:
        height, width = int(frame.shape[0]), int(frame.shape[1])
        size = (width, height)
        if self._sampler is not None and self._sampler_size == size:
            return self._sampler
        with self._lock:
            grid = self._grid
        if grid is None:
            return None  # 无键位属正常状态（先看画面），不要刷错误
        try:
            with self._lock:
                inner_ratio = self._sample_inner_ratio
                square_side = self._sample_square_side
                offset_y = self._sample_offset_y
                point_sampling = self._sample_point_sampling
                ring_points = self._sample_ring_points
                square_points = self._sample_square_points
                patch_ratio = self._sample_patch_ratio
            if point_sampling:
                self._sampler = KeyPointSampler(
                    grid,
                    width,
                    height,
                    inner_ratio=inner_ratio,
                    square_side=square_side,
                    offset_y=offset_y,
                    ring_points=ring_points,
                    square_points=square_points,
                    patch_ratio=patch_ratio,
                )
            else:
                self._sampler = KeySampler(
                    grid,
                    width,
                    height,
                    stride=4,
                    inner_ratio=inner_ratio,
                    square_side=square_side,
                    offset_y=offset_y,
                )
            self._sampler_size = size
        except Exception as exc:
            _log.warning("重建采样器失败（%dx%d）: %s", width, height, exc)
            self._sampler = None
            self._sampler_size = size
            return None
        return self._sampler

    def _emit_status(
        self,
        fps_window: deque[float],
        ms_ema: float,
        last_status: float,
        *,
        waiting: bool = False,
    ) -> float:
        now = time.perf_counter()
        if now - last_status < STATUS_EVERY_S:
            return last_status
        fps = 0.0
        if len(fps_window) >= 2:
            span = fps_window[-1] - fps_window[0]
            if span > 1e-6:
                fps = (len(fps_window) - 1) / span
        src_ms = 0.0
        stamps = list(self._src_ts)
        if len(stamps) >= 3:
            gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
            if gaps:
                src_ms = gaps[len(gaps) // 2] * 1000.0  # 中位数：抗偶发卡顿
        self.status_ready.emit(
            {
                "backend": self._backend_name,
                "fps": fps,
                "src_ms": src_ms,
                "src_fps": (1000.0 / src_ms) if src_ms > 0 else 0.0,
                "ms": ms_ema,
                "frames": self._frames,
                "waiting": waiting,
            }
        )
        return now
