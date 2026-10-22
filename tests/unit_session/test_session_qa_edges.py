"""`Session` 契约边界的 QA 补充用例（v1.1 §8 语义表逐一验证）。

与 `test_session_record.py`（工程师主用例）**不重复**，本文件专攻他没覆盖的角度：

- 状态机：录制中再次 `start_record` 不得打断在录的这次；失败后可重试；未 start 的 stop
  不得触碰捕获后端；
- `thresholds` 覆盖真的传到 `PressDetector`，且不污染 `spec.thresholds`；
- 黑帧「连续 30 帧」的**边界**（29 不算、30 才算、两段 15 帧不合并）；
- 时间戳回退时 `ts_rel` 单调钳制（`duration_ms` 不倒退、onset 不为负）；
- 非法帧 / 取帧异常 / `stats()` 异常 / `stop()` 异常 都不许掀翻录制；
- 线程名泄漏（前缀匹配，比只查 `yqrec-analysis` 严）；
- 分析线程不可恢复异常 → `stop_record()` 不抛、`metadata["thread_error"]` 有值；
- preview 回调在 stop 之后不再被调用；降级回调抛异常被吞掉；
- `Score.metadata` 各字段是可落盘的字符串。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

from yqrec.errors import CaptureError, ErrorCode, YQRecError
from yqrec.session import Recorder, Session, SessionState
from yqrec.timeline.detector import DetectorConfig
from yqrec.types import (
    CaptureStats,
    CaptureTarget,
    InstrumentSpec,
    KeyGrid,
    KeyPoint,
    KeyThreshold,
    PressEvent,
    Rect,
    ScreenTarget,
)

BRIGHT = 255
DARK = 40
SIZE = (640, 360)


# --------------------------------------------------------------------------- #
# 测试辅助（自带，不依赖工程师的测试模块）
# --------------------------------------------------------------------------- #


class FatalError(BaseException):
    """模拟分析线程内的不可恢复异常（`Exception` 之外，`_process` 不吞）。"""


def grid_7col(spec: InstrumentSpec) -> KeyGrid:
    """7 列 × N 行网格（顺序与 `spec.key_order` 对齐，与 CLI 内置布局同构）。"""
    count = spec.key_count
    radius = float(spec.button_radius) / 1920.0
    points: list[KeyPoint] = []
    for i in range(count):
        row, col = divmod(i, 7)
        cols = min(7, count - row * 7)
        u = 0.5 if cols <= 1 else 0.15 + 0.7 * (col / (cols - 1))
        v = min(0.85, 0.5 + 0.15 * row)
        points.append(KeyPoint(u=u, v=v, r=radius))
    return KeyGrid(points=tuple(points), key_count=count)


def _rect_of(grid: KeyGrid, index: int, width: int, height: int) -> tuple[int, int, int, int]:
    """该键 ROI 的涂色矩形 `(y0, y1, x0, x1)`（比 ROI 大 2px，避免边缘均值被亮像素拉高）。"""
    cx, cy, radius = grid.points[index].to_pixels(width, height)
    x0 = max(0, int(round(cx - radius)) - 2)
    x1 = min(width, int(round(cx + radius)) + 2)
    y0 = max(0, int(round(cy - radius)) - 2)
    y1 = min(height, int(round(cy + radius)) + 2)
    return (y0, max(y0 + 1, y1), x0, max(x0 + 1, x1))


def frame(grid: KeyGrid, pressed: set[int] | None = None, *, level: int = DARK) -> np.ndarray:
    """亮底 + 指定键涂暗的帧。"""
    width, height = SIZE
    out = np.full((height, width, 3), BRIGHT, dtype=np.uint8)
    for index in pressed or set():
        y0, y1, x0, x1 = _rect_of(grid, index, width, height)
        out[y0:y1, x0:x1] = level
    return out


def black_frame(*, level: int = 0) -> np.ndarray:
    width, height = SIZE
    return np.full((height, width, 3), level, dtype=np.uint8)


class ScriptedManager:
    """按脚本吐帧；脚本耗尽后一直返回 None（会话仍在录制状态）。"""

    def __init__(self, frames: list[tuple[Any, float]]) -> None:
        self.frames = list(frames)
        self.started = 0
        self.stopped = 0
        self.degraded_cb = None
        self._index = 0
        self._lock = threading.Lock()

    def start(self, target: CaptureTarget, *, prefer: str | None = None, target_fps: int = 60) -> None:
        self.started += 1
        self._index = 0

    def latest(self, timeout_ms: int = 100) -> tuple[Any, float] | None:
        with self._lock:
            if self._index >= len(self.frames):
                time.sleep(0.001)
                return None
            got = self.frames[self._index]
            self._index += 1
            return got

    def stop(self) -> None:
        self.stopped += 1

    def stats(self) -> CaptureStats:
        return CaptureStats(fps=50.0, dropped=1, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.degraded_cb = cb


class GatedManager:
    """先给 1 帧让 `start_record` 通过，之后挂起（返回 None）直到 `release` 被置位。

    用于稳定地「停在 RECORDING 状态」上做断言，不依赖墙钟时序。
    """

    def __init__(self, first: np.ndarray) -> None:
        self._first = (first, 0.0)
        self._sent = False
        self.started = 0
        self.stopped = 0
        self.degraded_cb = None
        self.frames_after_release: list[tuple[np.ndarray, float]] = []

    def start(self, target: CaptureTarget, *, prefer: str | None = None, target_fps: int = 60) -> None:
        self.started += 1
        self._sent = False

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        if not self._sent:
            self._sent = True
            return self._first
        if not self.frames_after_release:
            time.sleep(0.001)
            return None
        return self.frames_after_release.pop(0)

    def stop(self) -> None:
        self.stopped += 1

    def stats(self) -> CaptureStats:
        return CaptureStats(fps=0.0, dropped=0, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.degraded_cb = cb


def wait_for(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def target() -> ScreenTarget:
    return ScreenTarget(monitor=0, rect=Rect(0, 0, SIZE[0], SIZE[1]))


def record_all(
    spec: InstrumentSpec,
    grid: KeyGrid,
    frames: list[tuple[Any, float]],
    *,
    on_preview=None,  # type: ignore[no-untyped-def]
    thresholds: dict[str, KeyThreshold] | None = None,
    expected_frames: int | None = None,
) -> tuple[Session, ScriptedManager]:
    """跑完一整个脚本（等所有帧被消费）再返回会话。

    `expected_frames` 用于脚本里含**会被丢弃的坏帧**的场景（坏帧不计入 `frame_count`）。
    """
    manager = ScriptedManager(frames)
    session = Session(spec, manager=manager)
    session.start_record(target(), keygrid=grid, thresholds=thresholds, on_preview=on_preview)
    expect = len(frames) if expected_frames is None else expected_frames
    assert wait_for(lambda: session.frame_count >= expect, timeout=5.0), "帧未被消费完"
    return session, manager


@pytest.fixture
def grid(spec_fwzs: InstrumentSpec) -> KeyGrid:
    return grid_7col(spec_fwzs)


def _press_then_release(grid: KeyGrid, index: int, *, fps: float = 240.0) -> list[tuple[Any, float]]:
    """`index` 键：20 帧按下 + 8 帧抬起（保证 min_hold / min_gap 都满足）。"""
    frames: list[tuple[Any, float]] = []
    ts = 0.0
    step = 1.0 / fps
    for _ in range(20):
        frames.append((frame(grid, {index}), ts))
        ts += step
    for _ in range(8):
        frames.append((frame(grid), ts))
        ts += step
    return frames


# --------------------------------------------------------------------------- #
# 状态机（录制中再 start / 失败重试 / 未 start 的 stop）
# --------------------------------------------------------------------------- #


def test_state_is_recording_between_start_and_stop(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """`state` 在两次调用之间必须是 RECORDING（不能提前变 EDIT/IDLE）。"""
    manager = GatedManager(frame(grid))
    session = Session(spec_fwzs, manager=manager)
    assert session.state is SessionState.IDLE
    session.start_record(target(), keygrid=grid)
    try:
        assert session.state is SessionState.RECORDING
        assert session.frame_count >= 1
    finally:
        session.stop_record()
    assert session.state is SessionState.EDIT


def test_second_start_while_recording_keeps_current_take(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """录制中再次 `start_record` → CONFIG，且**不能**打断正在录的这次。

    若实现里先重置计数器/再校验状态，这里就会丢帧或让 stop 拿到空谱。
    """
    manager = GatedManager(frame(grid))
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target(), keygrid=grid)
    manager.frames_after_release = _press_then_release(grid, 3)

    with pytest.raises(YQRecError) as excinfo:
        session.start_record(target(), keygrid=grid)
    assert excinfo.value.code == ErrorCode.CONFIG
    assert session.state is SessionState.RECORDING
    assert manager.started == 1, "第二次 start 不得再次启动后端"

    assert wait_for(lambda: session.frame_count >= 29, timeout=5.0)
    score = session.stop_record()
    assert session.press_count == 1, "第二次 start 的失败不得清空本次录制"
    assert score.note_count == 1
    assert score.events[0].keys == (spec_fwzs.key_order[3],)


def test_failed_start_leaves_session_retryable(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """缺 keygrid 失败后：状态回 IDLE，且**同一个 Session** 能正常重试成功。"""
    manager = GatedManager(frame(grid))
    session = Session(spec_fwzs, manager=manager)
    with pytest.raises(YQRecError):
        session.start_record(target(), keygrid=None)
    assert session.state is SessionState.IDLE
    assert manager.started == 0, "参数校验失败不该打开捕获后端"

    session.start_record(target(), keygrid=grid)
    assert session.state is SessionState.RECORDING
    score = session.stop_record()
    assert score.metadata["instrument_id"] == spec_fwzs.id


def test_stop_before_start_does_not_touch_manager(spec_fwzs: InstrumentSpec) -> None:
    """未 start 就 stop → CONFIG，且不关闭/启动后端（不留半开句柄）。"""
    manager = ScriptedManager([])
    session = Session(spec_fwzs, manager=manager)
    with pytest.raises(YQRecError) as excinfo:
        session.stop_record()
    assert excinfo.value.code == ErrorCode.CONFIG
    assert manager.started == 0
    assert manager.stopped == 0
    assert session.state is SessionState.IDLE


# --------------------------------------------------------------------------- #
# thresholds 覆盖（v1.1 新增参数）真的生效，且不污染 spec
# --------------------------------------------------------------------------- #


def _always_pressed(spec: InstrumentSpec) -> dict[str, KeyThreshold]:
    """任何 R <= 255 都算按下：亮帧也会被判按（用来证明阈值确实被替换）。"""
    return {area.code: KeyThreshold(min_r=0, max_r=BRIGHT) for area in spec.areas}


def test_threshold_override_reaches_detector(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """同一批全亮帧：默认阈值 0 次按下；覆盖阈值后 21 键全按下。

    只断言「不抛异常」是弱测试；这里用**判按结果差异**证明 thresholds 真的传到了
    `PressDetector`，否则两种配置会给出一样的结果。
    """
    bright = [(frame(grid), i / 240.0) for i in range(20)]

    baseline, _ = record_all(spec_fwzs, grid, bright)
    baseline_score = baseline.stop_record()

    overridden, _ = record_all(
        spec_fwzs, grid, bright, thresholds=_always_pressed(spec_fwzs)
    )
    overridden_score = overridden.stop_record()

    assert baseline.press_count == 0
    assert baseline_score.events == []
    assert overridden.press_count == spec_fwzs.key_count
    assert overridden_score.note_count == 0, "21 键同时按下应被 scene guard 丢弃"


def test_recorder_thresholds_kwarg_does_not_mutate_spec(spec_fwzs: InstrumentSpec) -> None:
    """`Recorder(thresholds=...)` 只作用于本次录制，不改 `spec.thresholds`。"""
    before = dict(spec_fwzs.thresholds)
    custom = _always_pressed(spec_fwzs)
    recorder = Recorder(spec_fwzs, thresholds=custom, detector_cfg=DetectorConfig())

    bright_rgb = np.full((spec_fwzs.key_count, 3), BRIGHT, dtype=np.uint8)
    for i in range(10):
        recorder.push(i / 240.0, bright_rgb)
    score = recorder.finish()

    assert recorder.press_count == spec_fwzs.key_count
    assert score.events == [], "全键同刻 → scene guard 丢弃"
    assert spec_fwzs.thresholds == before, "spec 内置阈值不得被覆盖写回"
    assert spec_fwzs.thresholds is not custom


# --------------------------------------------------------------------------- #
# 黑帧边界：连续 30 帧才算 ever_black
# --------------------------------------------------------------------------- #


def test_black_run_of_29_is_not_flagged(spec_fwzs: InstrumentSpec, grid: KeyGrid) -> None:
    frames = [(black_frame(), i / 240.0) for i in range(29)]
    frames.append((frame(grid), 29 / 240.0))
    session, _ = record_all(spec_fwzs, grid, frames)
    assert session.black_frames == 29
    assert session.ever_black is False, "29 帧未达阈值，不应报警"
    session.stop_record()


def test_black_run_of_30_is_flagged(spec_fwzs: InstrumentSpec, grid: KeyGrid) -> None:
    frames = [(black_frame(), i / 240.0) for i in range(30)]
    session, _ = record_all(spec_fwzs, grid, frames)
    assert session.black_frames == 30
    assert session.ever_black is True
    session.stop_record()


def test_disjoint_black_runs_do_not_add_up(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """15 黑 + 1 亮 + 15 黑：黑帧总数 30，但「连续」被亮帧打断 → 不报警。"""
    frames: list[tuple[Any, float]] = []
    ts = 0.0
    step = 1.0 / 240.0
    for _ in range(15):
        frames.append((black_frame(), ts))
        ts += step
    frames.append((frame(grid), ts))
    ts += step
    for _ in range(15):
        frames.append((black_frame(), ts))
        ts += step

    session, _ = record_all(spec_fwzs, grid, frames)
    assert session.black_frames == 30
    assert session.ever_black is False, "连续段必须被非黑帧打断，不能累加"
    session.stop_record()


# --------------------------------------------------------------------------- #
# 时间戳单调钳制
# --------------------------------------------------------------------------- #


def test_non_monotonic_timestamps_are_clamped(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """后端时钟回退时 `ts_rel` 取历史最大值：`duration_ms` 不倒退、onset 不为负。"""
    frames: list[tuple[Any, float]] = [
        (frame(grid), 1.00),
        (frame(grid, {2}), 1.03),
        (frame(grid, {2}), 1.06),
        (frame(grid), 1.00),  # 回退
        (frame(grid), 0.50),  # 再回退
    ]
    session, _ = record_all(spec_fwzs, grid, frames)
    score = session.stop_record()

    assert session.duration_ms == pytest.approx(60.0, abs=1e-6), "不得被回退时间戳拉低"
    assert session.press_count == 1
    assert score.events[0].onset_ms == pytest.approx(30.0, abs=1e-6)
    assert all(getattr(e, "ms", 0.0) >= 0.0 for e in score.events)


# --------------------------------------------------------------------------- #
# 坏帧 / 取帧异常 / stats 异常 / stop 异常 都不许掀翻录制
# --------------------------------------------------------------------------- #


def test_malformed_frames_are_skipped_and_thread_survives(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """2D 帧 / 单通道帧 / None 帧被跳过（不计入 frame_count），后续好帧照常判按。"""
    bad = [
        (np.zeros((SIZE[1], SIZE[0]), dtype=np.uint8), 0.000),
        (np.zeros((SIZE[1], SIZE[0], 1), dtype=np.uint8), 0.004),
        (None, 0.008),
    ]
    good = _press_then_release(grid, 5)
    session, _ = record_all(spec_fwzs, grid, bad + good, expected_frames=len(good))
    score = session.stop_record()

    assert session.frame_count == len(good), "坏帧不应计入已分析帧数"
    assert session.press_count == 1
    assert score.events[0].keys == (spec_fwzs.key_order[5],)
    assert session.thread_error is None


class FlakyManager(ScriptedManager):
    """前 `fail_first` 次取帧抛 CaptureError，之后正常吐帧。"""

    def __init__(self, frames: list[tuple[Any, float]], *, fail_first: int) -> None:
        super().__init__(frames)
        self._remaining_failures = fail_first

    def latest(self, timeout_ms: int = 100) -> tuple[Any, float] | None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise CaptureError("临时取帧失败", code=ErrorCode.CAPTURE_TIMEOUT)
        return super().latest(timeout_ms)


def test_capture_errors_during_recording_are_tolerated(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """取帧连续抛错只记 debug，线程不得退出（否则一次抖动就毁掉整次录制）。"""
    frames = _press_then_release(grid, 7)
    manager = FlakyManager(frames, fail_first=5)
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target(), keygrid=grid)
    assert wait_for(lambda: session.frame_count >= len(frames), timeout=5.0)
    score = session.stop_record()

    assert session.thread_error is None
    assert "thread_error" not in score.metadata
    assert session.press_count == 1


class NoStatsManager(ScriptedManager):
    def stats(self) -> CaptureStats:
        raise RuntimeError("stats 探针坏了")


def test_stats_failure_falls_back_to_zero_stats(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    frames = [(frame(grid), i / 240.0) for i in range(6)]
    manager = NoStatsManager(frames)
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target(), keygrid=grid)
    assert wait_for(lambda: session.frame_count >= len(frames), timeout=5.0)
    score = session.stop_record()

    stats = session.stats
    assert stats.fps == 0.0 and stats.dropped == 0
    assert stats.last_error and "stats" in stats.last_error
    assert score.metadata["fps"] == "0.0"
    assert int(score.metadata["dropped"]) == 0


class NoStopManager(ScriptedManager):
    def stop(self) -> None:
        super().stop()
        raise RuntimeError("关闭后端失败")


def test_manager_stop_failure_does_not_block_score(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """后端 close 抛异常时仍要拿到谱面（已录数据不能因为收尾失败丢掉）。"""
    frames = _press_then_release(grid, 1)
    manager = NoStopManager(frames)
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target(), keygrid=grid)
    assert wait_for(lambda: session.frame_count >= len(frames), timeout=5.0)
    score = session.stop_record()

    assert manager.stopped == 1
    assert session.state is SessionState.EDIT
    assert score.note_count == 1


# --------------------------------------------------------------------------- #
# 线程泄漏 / 预览回调生命周期 / 降级回调异常
# --------------------------------------------------------------------------- #


def _yqrec_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if "yqrec" in t.name]


def test_no_yqrec_thread_leak_after_stop(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    session, _ = record_all(spec_fwzs, grid, [(frame(grid), i / 240.0) for i in range(6)])
    session.stop_record()
    assert wait_for(lambda: _yqrec_threads() == [], timeout=3.0), _yqrec_threads()


def test_no_thread_leak_after_first_frame_timeout(
    spec_fwzs: InstrumentSpec, grid: KeyGrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首帧超时路径也必须把线程收干净（只回 IDLE 却留活线程会串到下一次录制）。"""
    monkeypatch.setattr("yqrec.session.FIRST_FRAME_TIMEOUT_S", 0.15)
    session = Session(spec_fwzs, manager=ScriptedManager([]))
    with pytest.raises(CaptureError) as excinfo:
        session.start_record(target(), keygrid=grid)
    assert excinfo.value.code == ErrorCode.CAPTURE_TIMEOUT
    assert wait_for(lambda: _yqrec_threads() == [], timeout=3.0), _yqrec_threads()


def test_preview_callback_stops_after_stop_record(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """stop 之后不得再有预览回调（P4 里回调会碰 Qt 对象，晚到就会崩）。"""
    seen: list[tuple[int, ...]] = []

    def on_preview(item: np.ndarray) -> None:
        seen.append(item.shape)

    session, _ = record_all(
        spec_fwzs, grid, [(frame(grid), i / 240.0) for i in range(8)], on_preview=on_preview
    )
    session.stop_record()
    count = len(seen)
    time.sleep(0.05)
    assert len(seen) == count, "stop_record 之后仍在回调"
    assert count == 4, "8 帧 → 每 2 帧一次 → 4 次"
    assert all(shape == (SIZE[1], SIZE[0], 3) for shape in seen)


def test_degraded_callback_exception_is_swallowed(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    manager = GatedManager(frame(grid))
    session = Session(spec_fwzs, manager=manager)

    def boom(_name: str) -> None:
        raise RuntimeError("降级提示 UI 炸了")

    session.on_degraded(boom)
    session.start_record(target(), keygrid=grid)
    try:
        assert manager.degraded_cb is not None
        manager.degraded_cb("gdi")  # 不得把异常抛回捕获层
        assert session.state is SessionState.RECORDING
    finally:
        session.stop_record()


# --------------------------------------------------------------------------- #
# 分析线程不可恢复异常 → thread_error 契约
# --------------------------------------------------------------------------- #


class BlockingFatalSampler:
    """第一次采样就卡住，等测试放行后抛 BaseException（`Exception` 之外的致命错）。

    卡在 `sample()` 里是为了保证 `start_record()` 的首帧握手已经完成，
    避免「线程死得比 start_record 返回还快」造成的竞态（那种情况下
    `start_record` 会按契约抛 `CaptureError`，不是本用例要验的路径）。
    """

    frame_size = SIZE

    def __init__(self) -> None:
        self.release = threading.Event()

    def sample(self, frame_: np.ndarray) -> np.ndarray:
        self.release.wait(timeout=2.0)
        raise FatalError("GPU 驱动崩了")


def test_thread_error_is_recorded_and_stop_still_returns_score(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """线程炸了：`stop_record()` 不抛、`metadata["thread_error"]` 有值、状态到 EDIT。"""
    sampler = BlockingFatalSampler()
    manager = GatedManager(frame(grid))
    session = Session(
        spec_fwzs, manager=manager, sampler_factory=lambda _g, _w, _h: sampler
    )
    session.start_record(target(), keygrid=grid)
    assert session.state is SessionState.RECORDING
    assert session.frame_count >= 1

    sampler.release.set()
    assert wait_for(lambda: session.thread_error is not None, timeout=3.0)

    score = session.stop_record()
    assert session.state is SessionState.EDIT
    assert "GPU 驱动崩了" in score.metadata["thread_error"]
    assert score.events == []
    assert wait_for(lambda: _yqrec_threads() == [], timeout=3.0)


# --------------------------------------------------------------------------- #
# metadata 落盘契约
# --------------------------------------------------------------------------- #


def test_calibrate_is_placeholder_and_keeps_state(spec_fwzs: InstrumentSpec) -> None:
    """`calibrate()` 按契约是 P4 占位：抛 `NotImplementedYet(E_NOT_IMPLEMENTED)`，不动状态。"""
    session = Session(spec_fwzs, manager=ScriptedManager([]))
    with pytest.raises(YQRecError) as excinfo:
        session.calibrate()
    assert excinfo.value.code == ErrorCode.NOT_IMPLEMENTED
    assert "P4" in str(excinfo.value)
    assert session.state is SessionState.IDLE


def test_recorder_push_and_finish_contract(spec_fwzs: InstrumentSpec) -> None:
    """`Recorder` 的公开契约：`push` 返回 `PressEvent` 列表，`finish` 返回 `Score`。"""
    recorder = Recorder(spec_fwzs)
    bright = np.full((spec_fwzs.key_count, 3), BRIGHT, dtype=np.uint8)
    dark = np.full((spec_fwzs.key_count, 3), DARK, dtype=np.uint8)

    pushed: list[list[Any]] = []
    for i in range(20):
        pushed.append(recorder.push(i / 240.0, dark if i >= 2 else bright))
    score = recorder.finish()

    flat = [event for batch in pushed for event in batch]
    assert flat, "20 帧暗样本必须产出按下事件"
    assert all(isinstance(event, PressEvent) for event in flat)
    assert recorder.press_count == spec_fwzs.key_count, "全键同时按下 → 每键 1 次"
    assert score.metadata["instrument"] == spec_fwzs.name
    assert score.note_count == 0, "全键和弦被 scene guard 丢弃"


def test_metadata_values_are_plain_strings(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """`metadata` 会写进 tick 注释头，必须全是可直接落盘的 str 且能被 CLI 解析。"""
    session, _ = record_all(spec_fwzs, grid, _press_then_release(grid, 0))
    score = session.stop_record()

    for key, value in score.metadata.items():
        assert isinstance(value, str), f"{key} 不是 str: {value!r}"
    assert int(score.metadata["duration_ms"]) >= 0
    assert float(score.metadata["fps"]) >= 0.0
    assert int(score.metadata["dropped"]) >= 0
    assert int(score.metadata["black_frames"]) == 0
    assert "thread_error" not in score.metadata, "正常结束不得留这个 key"
