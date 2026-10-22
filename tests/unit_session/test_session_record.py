"""`Session` 录制编排单测（03 §7/§8）。

策略：用**假捕获层**喂确定性帧序列，覆盖

- 状态机与非法转换；
- 键位缺失 / 键数不符；
- 首帧超时与清理；
- 事件数与源信号一致（端到端）；
- 黑帧统计、预览回调、帧尺寸变化重建 sampler；
- 经真实 `CaptureManager` + 假后端的集成路径。
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, ClassVar

import numpy as np
import pytest

from yqrec.capture.base import BaseBackend
from yqrec.capture.manager import CaptureManager
from yqrec.errors import CaptureError, ErrorCode, YQRecError
from yqrec.session import Session, SessionState
from yqrec.types import (
    CaptureStats,
    CaptureTarget,
    InstrumentSpec,
    KeyGrid,
    KeyPoint,
    Rect,
    ScreenTarget,
)

BRIGHT = 255
"""未按下亮度（R=255 > 滞回上界 218 → 判为抬起）。"""

DARK = 40
"""按下亮度（R=40 <= 阈值上界 190 → 判为按下）。"""


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #


def make_grid(spec: InstrumentSpec) -> KeyGrid:
    """7 列 × N 行网格（顺序 = `spec.key_order`）。"""
    count = spec.key_count
    radius = float(spec.button_radius) / 1920.0
    points = tuple(
        KeyPoint(
            u=0.5 if min(7, count - (i // 7) * 7) <= 1 else 0.15 + 0.7 * ((i % 7) / (min(7, count - (i // 7) * 7) - 1)),
            v=min(0.85, 0.5 + 0.15 * (i // 7)),
            r=radius,
        )
        for i in range(count)
    )
    return KeyGrid(points=points, key_count=count)


def _rects(grid: KeyGrid, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """每个键要涂暗的像素矩形 `(y0, y1, x0, x1)`，比采样 ROI 略大。"""
    out: list[tuple[int, int, int, int]] = []
    for point in grid.points:
        cx, cy, radius = point.to_pixels(width, height)
        y0 = max(0, int(cy - radius) - 1)
        y1 = min(height, int(cy + radius) + 2)
        x0 = max(0, int(cx - radius) - 1)
        x1 = min(width, int(cx + radius) + 2)
        out.append((y0, max(y0 + 1, y1), x0, max(x0 + 1, x1)))
    return out


def synth_frames(
    grid: KeyGrid,
    presses: list[tuple[int, float, float]],
    *,
    size: tuple[int, int] = (640, 360),
    fps: float = 240.0,
    duration_s: float = 3.0,
    level: int = DARK,
) -> list[tuple[np.ndarray, float]]:
    """生成帧序列：`presses = [(键序号, 起始秒, 持续秒), ...]`。"""
    width, height = size
    boxes = _rects(grid, width, height)
    total = int(round(duration_s * fps))
    frames: list[tuple[np.ndarray, float]] = []
    for index in range(total):
        ts = index / fps
        frame = np.full((height, width, 3), BRIGHT, dtype=np.uint8)
        for key_index, start, duration in presses:
            if start <= ts < start + duration:
                y0, y1, x0, x1 = boxes[key_index]
                frame[y0:y1, x0:x1] = level
        frames.append((frame, ts))
    return frames


def solid_frames(
    count: int, *, size: tuple[int, int] = (64, 36), fps: float = 240.0, level: int = 0
) -> list[tuple[np.ndarray, float]]:
    """常量帧（黑帧/预览用）。"""
    frame = np.full((size[1], size[0], 3), level, dtype=np.uint8)
    return [(frame.copy(), i / fps) for i in range(count)]


class FakeManager:
    """`CaptureManager` 替身：按脚本吐帧，耗尽后返回 None。"""

    def __init__(self, frames: list[tuple[np.ndarray, float]] | None = None) -> None:
        self.frames = list(frames or [])
        self.drained = threading.Event()
        self.started = 0
        self.stopped = 0
        self.start_args: dict[str, Any] = {}
        self.degraded_cb = None
        self.stats_calls = 0
        self._index = 0
        self._lock = threading.Lock()
        if not self.frames:
            self.drained.set()

    # -- 管理器接口 -------------------------------------------------------- #

    def start(
        self, target: CaptureTarget, *, prefer: str | None = None, target_fps: int = 60
    ) -> None:
        self.started += 1
        self.start_args = {"target": target, "prefer": prefer, "target_fps": target_fps}
        self._index = 0

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        with self._lock:
            if self._index >= len(self.frames):
                self.drained.set()
                return None
            got = self.frames[self._index]
            self._index += 1
            if self._index >= len(self.frames):
                self.drained.set()
            return got

    def stop(self) -> None:
        self.stopped += 1

    def stats(self) -> CaptureStats:
        self.stats_calls += 1
        return CaptureStats(fps=123.0, dropped=7, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.degraded_cb = cb


def wait_for(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """轮询等待条件成立（避免用固定 sleep 造成偶发失败）。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def target_for(size: tuple[int, int] = (640, 360)) -> ScreenTarget:
    return ScreenTarget(monitor=0, rect=Rect(0, 0, size[0], size[1]))


@pytest.fixture
def grid(spec_fwzs: InstrumentSpec) -> KeyGrid:
    return make_grid(spec_fwzs)


def run_recording(
    spec: InstrumentSpec,
    grid: KeyGrid,
    frames: list[tuple[np.ndarray, float]],
    *,
    on_preview=None,  # type: ignore[no-untyped-def]
) -> tuple[Session, FakeManager]:
    """跑一次完整录制（等到帧序列耗尽再 stop）。"""
    manager = FakeManager(frames)
    session = Session(spec, manager=manager)
    session.start_record(target_for(), keygrid=grid, on_preview=on_preview)
    assert manager.drained.wait(timeout=10.0), "假后端帧未被消费完"
    assert wait_for(lambda: session.frame_count >= len(frames), timeout=5.0)
    return session, manager


# --------------------------------------------------------------------------- #
# 状态机 / 参数校验
# --------------------------------------------------------------------------- #


def test_start_without_keygrid_raises_config(spec_fwzs: InstrumentSpec) -> None:
    session = Session(spec_fwzs, manager=FakeManager())
    with pytest.raises(YQRecError) as excinfo:
        session.start_record(target_for(), keygrid=None)
    assert excinfo.value.code == ErrorCode.CONFIG
    assert "配置档" in str(excinfo.value)
    assert session.state is SessionState.IDLE


def test_start_with_mismatched_keygrid_raises(spec_fwzs: InstrumentSpec) -> None:
    bad = KeyGrid(
        points=tuple(KeyPoint(u=0.5, v=0.5, r=0.02) for _ in range(spec_fwzs.key_count - 1)),
        key_count=spec_fwzs.key_count - 1,
    )
    session = Session(spec_fwzs, manager=FakeManager())
    with pytest.raises(YQRecError) as excinfo:
        session.start_record(target_for(), keygrid=bad)
    assert excinfo.value.code == ErrorCode.CONFIG


def test_stop_without_start_raises(spec_fwzs: InstrumentSpec) -> None:
    session = Session(spec_fwzs, manager=FakeManager())
    with pytest.raises(YQRecError) as excinfo:
        session.stop_record()
    assert excinfo.value.code == ErrorCode.CONFIG


def test_restart_after_stop_is_illegal(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    session, _manager = run_recording(spec_fwzs, grid, solid_frames(10, level=BRIGHT))
    session.stop_record()
    assert session.state is SessionState.EDIT
    with pytest.raises(YQRecError) as excinfo:
        session.start_record(target_for(), keygrid=grid)
    assert excinfo.value.code == ErrorCode.CONFIG


def test_stop_twice_returns_same_score(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    session, manager = run_recording(spec_fwzs, grid, solid_frames(10, level=BRIGHT))
    first = session.stop_record()
    stopped_once = manager.stopped
    second = session.stop_record()
    assert first is second
    assert manager.stopped == stopped_once, "重复 stop 不应再关闭一次后端"


def test_start_forwards_backend_and_fps(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    manager = FakeManager(solid_frames(5, level=BRIGHT))
    session = Session(spec_fwzs, manager=manager)
    session.start_record(
        target_for(), keygrid=grid, backend="gdi", target_fps=30
    )
    assert manager.drained.wait(timeout=5.0)
    session.stop_record()
    assert manager.start_args["prefer"] == "gdi"
    assert manager.start_args["target_fps"] == 30
    assert manager.stopped == 1, "退出前必须关闭 manager"


# --------------------------------------------------------------------------- #
# 首帧超时
# --------------------------------------------------------------------------- #


def test_first_frame_timeout_cleans_up(
    spec_fwzs: InstrumentSpec, grid: KeyGrid, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("yqrec.session.FIRST_FRAME_TIMEOUT_S", 0.2)
    manager = FakeManager([])  # 永远没有帧
    session = Session(spec_fwzs, manager=manager)
    with pytest.raises(CaptureError) as excinfo:
        session.start_record(target_for(), keygrid=grid)
    assert excinfo.value.code == ErrorCode.CAPTURE_TIMEOUT
    assert manager.stopped >= 1, "超时后必须关闭 manager"
    assert session.state is SessionState.IDLE
    assert session._thread is None  # noqa: SLF001 - 断言线程已回收
    with pytest.raises(YQRecError):
        session.stop_record()


def test_manager_start_failure_wrapped(spec_fwzs: InstrumentSpec, grid: KeyGrid) -> None:
    class BoomManager(FakeManager):
        def start(self, target, *, prefer=None, target_fps=60) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002
            raise RuntimeError("没有可用的捕获后端")

    session = Session(spec_fwzs, manager=BoomManager())
    with pytest.raises(CaptureError) as excinfo:
        session.start_record(target_for(), keygrid=grid)
    assert "没有可用的捕获后端" in str(excinfo.value)
    assert session.state is SessionState.IDLE


# --------------------------------------------------------------------------- #
# 端到端：事件数与源信号一致
# --------------------------------------------------------------------------- #

SIGNAL: list[tuple[int, float, float]] = [
    (0, 0.30, 0.20),
    (5, 0.90, 0.20),
    (10, 1.50, 0.20),
    (15, 2.10, 0.20),
    (20, 2.70, 0.20),
]


def test_events_match_source_signal(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """3 秒合成信号 → 5 次按下，事件数一致（±1）。"""
    frames = synth_frames(grid, SIGNAL, duration_s=3.0)
    session, _manager = run_recording(spec_fwzs, grid, frames)
    score = session.stop_record()

    assert abs(score.note_count - len(SIGNAL)) <= 1
    assert abs(session.press_count - len(SIGNAL)) <= 1
    assert len(score.events) == 2 * score.note_count - 1, "音符之间应各有一个 Rest"
    notes = [e for e in score.events if not hasattr(e, "ms")]
    assert notes, "应至少产出 1 个音符"
    assert notes[0].onset_ms == pytest.approx(SIGNAL[0][1] * 1000.0, abs=15.0)


def test_metadata_contains_instrument_and_duration(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    frames = synth_frames(grid, SIGNAL, duration_s=1.0)
    session, _manager = run_recording(spec_fwzs, grid, frames)
    score = session.stop_record()
    assert score.metadata["instrument"] == spec_fwzs.name
    duration_ms = int(score.metadata["duration_ms"])
    assert 800 <= duration_ms <= 1200, duration_ms
    assert float(score.metadata["fps"]) > 0
    assert score.metadata["black_frames"] == "0"


def test_stats_snapshot_survives_stop(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    session, manager = run_recording(spec_fwzs, grid, solid_frames(6, level=BRIGHT))
    session.stop_record()
    stats = session.stats
    assert stats.fps == 123.0  # 来自假 manager 的快照
    assert stats.dropped == 7
    assert manager.stats_calls >= 1


# --------------------------------------------------------------------------- #
# 黑帧 / 预览 / 尺寸变化
# --------------------------------------------------------------------------- #


def test_black_frames_counted_and_flagged(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    frames = solid_frames(40, size=(320, 180), level=0)  # 全黑
    session, _manager = run_recording(spec_fwzs, grid, frames)
    assert wait_for(lambda: session.black_frames == 40, timeout=5.0)
    assert session.ever_black is True
    session.stop_record()


def test_black_frames_do_not_interrupt_recording(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    """黑帧后恢复正常帧仍能判按（黑帧只统计，不中断录制）。

    纯黑帧意味着"21 键全暗"，于是黑帧期会判出全键按下（装配阶段被 scene guard 丢弃）；
    关注点是：黑帧之后仍能正常检出新的按下事件。
    """
    dark = solid_frames(35, size=(320, 180), level=0)
    bright = [(f, ts + 35 / 240.0) for f, ts in solid_frames(20, size=(320, 180), level=BRIGHT)]
    press = [
        (f, ts + 55 / 240.0)
        for f, ts in synth_frames(grid, [(3, 0.0, 0.2)], size=(320, 180), duration_s=0.25, fps=240.0)
    ]
    session, _manager = run_recording(spec_fwzs, grid, dark + bright + press)
    score = session.stop_record()
    assert session.ever_black is True
    assert session.press_count == spec_fwzs.key_count + 1, "黑帧期的全键按下 + 恢复后的 1 次"
    assert score.note_count == 1, "全键'和弦'被 scene guard 丢弃，只剩恢复后的单音"
    assert score.events[0].keys == (spec_fwzs.key_order[3],)


def test_preview_callback_called_every_two_frames(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    seen: list[int] = []
    frames = solid_frames(20, level=BRIGHT)
    session, _manager = run_recording(
        spec_fwzs, grid, frames, on_preview=lambda _f: seen.append(1)
    )
    session.stop_record()
    assert len(seen) == 10, len(seen)


def test_preview_callback_errors_are_swallowed(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    calls: list[int] = []

    def boom(_frame) -> None:  # type: ignore[no-untyped-def]
        calls.append(1)
        raise RuntimeError("预览渲染炸了")

    frames = solid_frames(10, level=BRIGHT)
    session, _manager = run_recording(spec_fwzs, grid, frames, on_preview=boom)
    score = session.stop_record()
    assert calls, "回调应被调用"
    assert session.frame_count == 10
    assert score is not None


def test_frame_size_change_rebuilds_sampler(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    small = solid_frames(8, size=(320, 180), level=BRIGHT)
    big = synth_frames(
        grid, [(7, 0.0, 0.2)], size=(640, 360), duration_s=0.4, fps=240.0
    )
    shifted = [(f, ts + 8 / 240.0) for f, ts in big]
    session, _manager = run_recording(spec_fwzs, grid, small + shifted)
    score = session.stop_record()
    assert session.frame_count == len(small) + len(big)
    assert session.press_count == 1
    assert score.note_count == 1


def test_sampler_factory_failure_does_not_kill_thread(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    def boom(grid_, width, height):  # type: ignore[no-untyped-def]  # noqa: ARG001
        raise RuntimeError("采样器构建失败")

    manager = FakeManager(solid_frames(12, level=BRIGHT))
    session = Session(spec_fwzs, manager=manager, sampler_factory=boom)
    session.start_record(target_for(), keygrid=grid)
    assert manager.drained.wait(timeout=5.0)
    assert wait_for(lambda: session.frame_count >= 12, timeout=5.0)
    score = session.stop_record()  # 不崩、能收尾
    assert score.events == []
    assert session.press_count == 0


def test_degraded_callback_forwarded(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    manager = FakeManager(solid_frames(5, level=BRIGHT))
    session = Session(spec_fwzs, manager=manager)
    names: list[str] = []
    session.on_degraded(names.append)
    session.start_record(target_for(), keygrid=grid)
    assert manager.drained.wait(timeout=5.0)
    assert manager.degraded_cb is not None
    manager.degraded_cb("dxgi")
    session.stop_record()
    assert names == ["dxgi"]


# --------------------------------------------------------------------------- #
# 经真实 CaptureManager + 假后端的集成路径
# --------------------------------------------------------------------------- #


class ScriptedBackend(BaseBackend):
    """按类属性脚本吐帧的最小后端（走真实 `CaptureManager` 流程）。"""

    name = "scripted"
    script: ClassVar[list[tuple[np.ndarray, float]]] = []

    def __init__(self) -> None:
        super().__init__()
        self._index = 0

    @classmethod
    def is_available(cls) -> bool:
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, ScreenTarget)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        self._index = 0

    def _grab(self) -> tuple[np.ndarray, float] | None:
        script = type(self).script
        if self._index >= len(script):
            return None
        got = script[self._index]
        self._index += 1
        return got

    def _close(self) -> None:
        return None


def test_session_with_real_capture_manager(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    presses = [(2, 0.05, 0.2), (9, 0.45, 0.2), (16, 0.85, 0.2)]
    ScriptedBackend.script = synth_frames(grid, presses, duration_s=1.5, fps=120.0)
    manager = CaptureManager(backends=[ScriptedBackend])
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target_for(), keygrid=grid, backend="scripted", target_fps=120)
    assert wait_for(lambda: session.frame_count >= len(ScriptedBackend.script), timeout=10.0)
    score = session.stop_record()
    assert abs(session.press_count - len(presses)) <= 1
    assert abs(score.note_count - len(presses)) <= 1
    assert session.stats.dropped >= 0
    assert session.state is SessionState.EDIT


def test_analysis_thread_not_alive_after_stop(
    spec_fwzs: InstrumentSpec, grid: KeyGrid
) -> None:
    session, _manager = run_recording(spec_fwzs, grid, solid_frames(6, level=BRIGHT))
    session.stop_record()
    assert session._thread is None  # noqa: SLF001 - 内部断言线程已回收
    names = [t.name for t in threading.enumerate()]
    assert "yqrec-analysis" not in names


def test_synth_signal_levels_match_thresholds(spec_fwzs: InstrumentSpec) -> None:
    """守住合成帧与内置阈值的耦合：亮帧必须判"未按下"，暗帧必须判"按下"。"""
    threshold = spec_fwzs.thresholds["main"]
    channels = spec_fwzs.areas[0].channels
    assert threshold.accepts((BRIGHT, BRIGHT, BRIGHT), channels) is False
    assert threshold.accepts((DARK, DARK, DARK), channels) is True
    assert sys.byteorder in ("little", "big")
