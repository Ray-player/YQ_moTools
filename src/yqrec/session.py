"""录制会话：采样 → 判按 → 装配 → 谱面（03 §7）。

分两层：

- `Recorder`：纯数据管线（喂 RGB 序列 → 事件流 → `Score`），与捕获解耦；
- `Session`：应用服务门面（P1 起可用）——状态机 + 捕获编排 + 分析线程。

线程模型（03 §7）：**分析线程是唯一**执行 `KeySampler.sample` + `PressDetector.update`
的地方；UI/CLI 只能读快照（`stats` / `press_count` / `black_frames` / `on_preview` 回调）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from .capture.manager import CaptureManager
from .errors import CaptureError, ErrorCode, NotImplementedYet, YQRecError
from .instruments import load_sampling
from .logging import get_logger
from .timeline.assembler import AssembleConfig, assemble
from .timeline.detector import DetectorConfig, PressDetector
from .types import (
    DEFAULT_SQUARE_SIDE,
    CaptureStats,
    CaptureTarget,
    InstrumentSpec,
    KeyGrid,
    KeyThreshold,
    PressEvent,
    Score,
    ScoreEvent,
)
from .vision.sampler import KeySampler

_log = get_logger("session")

# --------------------------------------------------------------------------- #
# 可调常量（录制行为参数；测试可 monkeypatch 模块属性覆盖）
# --------------------------------------------------------------------------- #

FIRST_FRAME_TIMEOUT_S: float = 3.0
"""首帧超时：启动后该时长内一帧都没拿到 → `CaptureError(CAPTURE_TIMEOUT)`。"""

THREAD_JOIN_TIMEOUT_S: float = 1.5
"""停止录制时等待分析线程退出的上限（秒）。"""

BLACK_PIXEL_MAX: int = 2
"""帧像素最大值 <= 该值视为黑帧（与 03 §6 `E_CAPTURE_BLACK` 对齐）。"""

BLACK_RUN_FRAMES: int = 30
"""连续黑帧达到该数量 → `ever_black = True`（黑帧不中断录制，只提示）。"""

PREVIEW_EVERY_N_FRAMES: int = 2
"""`on_preview` 每 N 帧回调一次。"""

LATEST_TIMEOUT_MS: int = 100
"""单次向后端要帧的等待上限。"""

IDLE_SLEEP_S: float = 0.001
"""无新帧时的让出 CPU 时长。"""


class SessionState(StrEnum):
    IDLE = "idle"
    CALIBRATING = "calibrating"
    RECORDING = "recording"
    ENCODING = "encoding"
    EDIT = "edit"


class _SamplerLike(Protocol):
    """`KeySampler` 的最小接口（便于测试注入）。

    `frame_size` 用于探测帧尺寸变化；`sample(frame)` 返回 (K,3) uint8 RGB。
    """

    @property
    def frame_size(self) -> tuple[int, int]: ...

    def sample(self, frame: np.ndarray) -> np.ndarray: ...


class _ManagerLike(Protocol):
    """`CaptureManager` 的最小接口（便于测试/合成源注入）。"""

    def start(
        self, target: CaptureTarget, *, prefer: str | None = None, target_fps: int = 60
    ) -> Any: ...

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None: ...

    def stop(self) -> None: ...

    def stats(self) -> CaptureStats: ...

    def on_degraded(self, cb: Callable[[str], None]) -> None: ...


def _default_sampler_factory(grid: KeyGrid, width: int, height: int) -> KeySampler:
    """默认按 `stride=4` 构建 `KeySampler`（03 §3.3 性能要求）。"""
    return KeySampler(grid, width, height, stride=4)


def make_sampler_factory(inner_ratio: float, square_side: float = DEFAULT_SQUARE_SIDE):
    """按全局 `sampling` 口径造采样器工厂（`inner_ratio>0` = 环形，否则边长 `square_side·R` 的方块）。"""

    def factory(grid: KeyGrid, width: int, height: int) -> KeySampler:
        return KeySampler(
            grid,
            width,
            height,
            stride=4,
            inner_ratio=inner_ratio,
            square_side=square_side,
        )

    return factory


class Recorder:
    """纯数据录制器：喂 RGB 序列，产出事件流。

    与捕获解耦：GUI/CLI 负责取帧与采样，`Recorder` 只做判按与装配。
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        *,
        detector_cfg: DetectorConfig | None = None,
        assemble_cfg: AssembleConfig | None = None,
        thresholds: dict[str, KeyThreshold] | None = None,
    ) -> None:
        self._spec = spec
        self._detector = PressDetector(spec, thresholds, cfg=detector_cfg)
        self._assemble_cfg = assemble_cfg or AssembleConfig()
        self._presses: list[PressEvent] = []
        self._last_ts = 0.0

    @property
    def press_count(self) -> int:
        return sum(1 for e in self._presses if e.down)

    def push(self, ts: float, rgb: np.ndarray) -> list[PressEvent]:
        """喂一帧采样（K,3）；返回本帧产生的事件。"""
        events = self._detector.update(rgb, ts)
        self._presses.extend(events)
        self._last_ts = max(self._last_ts, ts)
        return events

    def finish(self) -> Score:
        """结束录制：释放按下的键 → 装配成谱面。"""
        self._presses.extend(self._detector.flush(self._last_ts + 0.001))
        events: list[ScoreEvent] = assemble(list(self._presses), self._spec, self._assemble_cfg)
        return Score(
            events=events,
            metadata={"name": "", "instrument": self._spec.name},
        )


class Session:
    """应用服务门面：状态机 + 捕获 + 标定 + 录制。

    状态机（本批冻结）：:

        IDLE --start_record--> RECORDING --stop_record--> EDIT

    - 非法转换（例如未 start 就 stop、录制中再次 start）抛 `YQRecError(CONFIG)`；
    - `stop_record()` 语义：**可重复调用**，第二次起返回同一个 `Score` 对象（不再重算）；
    - `calibrate()` 仍是 P4 占位（`NotImplementedYet`）。

    时间戳语义：内部统一使用后端返回的 `time.perf_counter()` 秒，并减去首帧时间戳
    得到"相对录制起点"的时间（因此 `NoteEvent.onset_ms` 从 0 开始，便于谱面比对）。
    """

    def __init__(
        self,
        spec: InstrumentSpec,
        *,
        manager: _ManagerLike | None = None,
        sampler_factory: Callable[[KeyGrid, int, int], _SamplerLike] | None = None,
    ) -> None:
        self._spec = spec
        self.state = SessionState.IDLE
        self._manager: _ManagerLike = manager if manager is not None else CaptureManager()
        if sampler_factory is not None:
            self._sampler_factory = sampler_factory
        else:
            sampling = load_sampling()
            self._sampler_factory = make_sampler_factory(
                sampling.ring_inner_ratio, sampling.square_side_ratio
            )

        self._recorder: Recorder | None = None
        self._score: Score | None = None
        self._grid: KeyGrid | None = None
        self._sampler: _SamplerLike | None = None
        self._sampler_size: tuple[int, int] | None = None

        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._first_frame_evt = threading.Event()
        self._error: BaseException | None = None

        self._t0: float | None = None
        self._last_ts_rel = 0.0
        self._frames = 0
        self._black_frames = 0
        self._black_run = 0
        self._ever_black = False

        self._on_preview: Callable[[np.ndarray], None] | None = None
        self._degraded_cb: Callable[[str], None] | None = None
        self._last_stats: CaptureStats | None = None

    # -- 只读快照（UI/CLI 线程可读） --------------------------------------- #

    @property
    def stats(self) -> CaptureStats:
        """当前/最近一次捕获统计（停止录制后返回快照）。"""
        if self._last_stats is not None:
            return self._last_stats
        return self._safe_stats()

    @property
    def press_count(self) -> int:
        """已确认的按下次数（下沿事件数）。"""
        return self._recorder.press_count if self._recorder is not None else 0

    @property
    def black_frames(self) -> int:
        """累计黑帧数（按帧计，不是按"连续段"计）。"""
        return self._black_frames

    @property
    def ever_black(self) -> bool:
        """是否曾出现连续 `BLACK_RUN_FRAMES` 帧全黑（提示 DRM/独占全屏）。"""
        return self._ever_black

    @property
    def frame_count(self) -> int:
        """已分析的帧数。"""
        return self._frames

    @property
    def thread_error(self) -> BaseException | None:
        """分析线程退出时的异常（`None` 表示线程正常结束）。

        线程因不可恢复异常退出时，`stop_record()` 不抛异常（已录数据仍要能落盘），
        而是把原因写进 `Score.metadata["thread_error"]`。
        """
        return self._error

    @property
    def duration_ms(self) -> float:
        """已录制时长（毫秒，按后端时间戳计算）。"""
        return self._last_ts_rel * 1000.0

    def on_degraded(self, cb: Callable[[str], None]) -> None:
        """注册后端降级回调（在捕获线程内触发，实现需自行保证线程安全）。"""
        self._degraded_cb = cb
        self._bind_degraded()

    # -- 录制 -------------------------------------------------------------- #

    def start_record(
        self,
        target: CaptureTarget,
        *,
        keygrid: KeyGrid | None = None,
        thresholds: dict[str, KeyThreshold] | None = None,
        backend: str = "auto",
        target_fps: int = 60,
        on_preview: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        """启动录制：打开捕获 → 起分析线程 → 阻塞等首帧（最多 `FIRST_FRAME_TIMEOUT_S`）。

        Args:
            target: 捕获目标（屏幕区域 / 窗口）。
            keygrid: 键位点（必填）；缺省抛 `YQRecError(CONFIG)`。
            thresholds: 标定阈值（area code → `KeyThreshold`）；None 用 spec 内置。
            backend: 后端名或 `"auto"`。
            target_fps: 期望帧率。
            on_preview: 预览帧回调 `(frame_bgr)`，异常被吞掉只记 debug。

        Raises:
            YQRecError: 状态非法 / 缺 `keygrid` / 键数不符（码 `CONFIG`）。
            CaptureError: 后端打不开或首帧超时（码 `CAPTURE_OPEN` / `CAPTURE_TIMEOUT`）。
        """
        if self.state is not SessionState.IDLE:
            raise YQRecError(
                ErrorCode.CONFIG,
                f"非法状态转换: {self.state} → {SessionState.RECORDING}；"
                "Session 单次录制，需要重新录制请新建 Session",
            )
        if keygrid is None:
            raise YQRecError(
                ErrorCode.CONFIG,
                "录制需要含键位点的配置档（KeyGrid）：请先标定或指定 --profile",
            )
        if keygrid.key_count != self._spec.key_count:
            raise YQRecError(
                ErrorCode.CONFIG,
                f"KeyGrid 点数({keygrid.key_count}) 与乐器 {self._spec.id} 的键数"
                f"({self._spec.key_count}) 不符",
            )

        self._reset_counters()
        self._grid = keygrid
        self._on_preview = on_preview
        self._recorder = Recorder(self._spec, thresholds=thresholds)
        self._bind_degraded()

        try:
            self._manager.start(target, prefer=backend, target_fps=target_fps)
        except YQRecError:
            self._reset_after_failure()
            raise
        except Exception as exc:  # 管理器内部多为 RuntimeError / 驱动异常
            self._reset_after_failure()
            raise CaptureError("无法启动捕获", detail=str(exc)) from exc

        self.state = SessionState.RECORDING
        self._thread = threading.Thread(target=self._run, name="yqrec-analysis", daemon=True)
        self._thread.start()
        self._await_first_frame()

    def stop_record(self) -> Score:
        """停止录制并返回谱面（可重复调用，第二次起返回同一个 `Score`）。

        容错语义：若分析线程已因**不可恢复异常**退出，本方法**不抛异常**（已录到的数据
        仍要能落盘），而是把原因记进 `Score.metadata["thread_error"]` 并记 error 日志；
        线程正常结束时不会写入该 key（保持 metadata 干净）。

        Raises:
            YQRecError: 当前不在 `RECORDING` 状态（码 `CONFIG`）；或线程无法退出。
        """
        if self.state is SessionState.EDIT and self._score is not None:
            return self._score
        if self.state is not SessionState.RECORDING:
            raise YQRecError(
                ErrorCode.CONFIG,
                f"未在录制中，无法 stop_record（当前状态: {self.state}）",
            )

        self._stop_evt.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            if thread.is_alive():
                _log.error("分析线程未在 %.1fs 内退出，强制关闭捕获后端", THREAD_JOIN_TIMEOUT_S)
                self._close_manager()
                thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            if thread.is_alive():
                raise YQRecError(
                    ErrorCode.CONFIG,
                    "分析线程未能退出（可能阻塞在捕获后端），请检查后端实现",
                )
        self._thread = None

        # 线程死了也要收尾：记录原因（不抛），已录数据照常装配
        thread_error = self._error
        if thread_error is not None:
            _log.error("分析线程异常退出，谱面可能不完整: %s", thread_error)

        # 统计必须在关闭 manager 之前取（关闭后后端实例被置空）
        self._last_stats = self._safe_stats()
        self._close_manager()

        recorder = self._recorder
        score = recorder.finish() if recorder is not None else Score()
        score.metadata.update(
            {
                "instrument": self._spec.name,
                "instrument_id": self._spec.id,
                "duration_ms": str(int(round(self.duration_ms))),
                "fps": f"{self._last_stats.fps:.1f}",
                "dropped": str(self._last_stats.dropped),
                "black_frames": str(self._black_frames),
            }
        )
        if thread_error is not None:
            # 裸 `SystemExit()` / `KeyboardInterrupt()` 的 str() 为空，用类型名兜底，
            # 便于 CLI 侧据 "thread_error 是否存在" 判定 partial（而不依赖字符串真值）
            score.metadata["thread_error"] = str(thread_error) or type(thread_error).__name__
        self._score = score
        self.state = SessionState.EDIT
        _log.info(
            "录制结束: %d 个事件 / %d 次按下 / %.0fms",
            len(score.events),
            self.press_count,
            self.duration_ms,
        )
        return score

    def calibrate(self) -> None:
        raise NotImplementedYet("Session.calibrate（标定向导数据侧）", phase="P4")

    # -- 分析线程 ---------------------------------------------------------- #

    def _run(self) -> None:
        """分析线程主循环：取帧 → 采样 → 判按。

        单帧异常只记日志不中断（捕获层异常已由 `CaptureManager` 内部降级）；
        只有不可恢复的异常才写入 `self._error` 并退出。
        """
        try:
            while not self._stop_evt.is_set():
                try:
                    got = self._manager.latest(timeout_ms=LATEST_TIMEOUT_MS)
                except Exception as exc:
                    _log.debug("取帧异常（继续录制）: %s", exc)
                    self._stop_evt.wait(IDLE_SLEEP_S)
                    continue
                if got is None:
                    self._stop_evt.wait(IDLE_SLEEP_S)
                    continue
                self._process(got)
        except BaseException as exc:  # noqa: BLE001 - 记录后由 stop_record/start_record 报告
            self._error = exc
            _log.exception("分析线程异常退出: %s", exc)
        finally:
            self._stop_evt.set()
            self._first_frame_evt.set()

    def _process(self, got: tuple[np.ndarray, float]) -> None:
        """处理一帧：黑帧检测 → 采样 → 判按 → 预览。"""
        frame, ts = got
        if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
            _log.debug("非法帧（跳过）: %r", getattr(frame, "shape", None))
            return
        recorder = self._recorder
        if recorder is None:
            return
        if self._t0 is None:
            self._t0 = float(ts)

        self._frames += 1
        self._update_black(frame)
        self._first_frame_evt.set()

        self._ensure_sampler(frame)
        sampler = self._sampler
        if sampler is None:
            return
        try:
            rgb = sampler.sample(frame)
        except Exception as exc:
            _log.warning("采样失败（跳过该帧）: %s", exc)
            return

        ts_rel = max(self._last_ts_rel, max(0.0, float(ts) - self._t0))
        self._last_ts_rel = ts_rel
        recorder.push(ts_rel, rgb)
        self._maybe_preview(frame)

    def _ensure_sampler(self, frame: np.ndarray) -> None:
        """帧尺寸变化时按新尺寸重建 `KeySampler`（重建失败只记 warning）。"""
        height, width = int(frame.shape[0]), int(frame.shape[1])
        size = (width, height)
        if self._sampler is not None and self._sampler_size == size:
            return
        if self._sampler is None and self._sampler_size == size:
            # 同尺寸已重建失败过，避免每帧刷日志
            return
        grid = self._grid
        if grid is None:
            return
        try:
            self._sampler = self._sampler_factory(grid, size[0], size[1])
            self._sampler_size = size
            _log.debug("KeySampler 就绪: %dx%d", size[0], size[1])
        except Exception as exc:
            _log.warning("重建 KeySampler 失败（%dx%d）: %s", size[0], size[1], exc)
            self._sampler = None
            self._sampler_size = size

    def _update_black(self, frame: np.ndarray) -> None:
        """黑帧统计：连续 `BLACK_RUN_FRAMES` 帧 → `ever_black`；不中断录制。"""
        try:
            is_black = int(frame.max()) <= BLACK_PIXEL_MAX
        except Exception as exc:  # 空帧等异常数据
            _log.debug("黑帧检测失败: %s", exc)
            is_black = False
        if not is_black:
            self._black_run = 0
            return
        self._black_frames += 1
        self._black_run += 1
        if self._black_run >= BLACK_RUN_FRAMES and not self._ever_black:
            self._ever_black = True
            _log.warning(
                "连续 %d 帧全黑：疑似 DRM / 独占全屏 / 窗口化优化，请切换捕获后端",
                self._black_run,
            )

    def _maybe_preview(self, frame: np.ndarray) -> None:
        """每 `PREVIEW_EVERY_N_FRAMES` 帧回调一次；异常一律吞掉（只记 debug）。"""
        cb = self._on_preview
        if cb is None or PREVIEW_EVERY_N_FRAMES <= 0:
            return
        if self._frames % PREVIEW_EVERY_N_FRAMES != 0:
            return
        try:
            cb(frame)
        except Exception as exc:
            _log.debug("预览回调异常（已忽略）: %s", exc)

    # -- 生命周期辅助 ------------------------------------------------------ #

    def _await_first_frame(self) -> None:
        """阻塞等待首帧；超时或线程提前退出则清理并抛错。"""
        deadline = time.perf_counter() + FIRST_FRAME_TIMEOUT_S
        while time.perf_counter() < deadline:
            if self._first_frame_evt.wait(timeout=0.01):
                break
            if self._thread is not None and not self._thread.is_alive():
                break

        if self._error is not None:
            error = self._error
            self._abort()
            if isinstance(error, YQRecError):
                raise error
            raise CaptureError("捕获线程启动失败", detail=str(error)) from error
        if not self._first_frame_evt.is_set():
            self._abort()
            raise CaptureError(
                f"{FIRST_FRAME_TIMEOUT_S:.1f} 秒内未收到任何帧：请检查目标区域/后端/权限",
                code=ErrorCode.CAPTURE_TIMEOUT,
            )

    def _abort(self) -> None:
        """启动失败时清理线程与后端，并把状态退回 IDLE。

        顺序：停标志 → 先给线程一点时间自己退 → 关后端（把可能阻塞在 `latest()` 的线程
        弹出来）→ 再 join 一次。任何路径都不允许把活线程留在 `threading.enumerate()` 里。
        """
        self._stop_evt.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._close_manager()
        if thread is not None and thread.is_alive():
            thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
        if thread is not None and thread.is_alive():
            _log.error("分析线程在启动失败路径上仍未退出，请检查捕获后端实现")
        self._reset_after_failure()

    def _close_manager(self) -> None:
        try:
            self._manager.stop()
        except Exception as exc:  # 关闭失败不应影响收尾
            _log.debug("关闭捕获后端失败（已忽略）: %s", exc)

    def _safe_stats(self) -> CaptureStats:
        try:
            return self._manager.stats()
        except Exception as exc:
            _log.debug("读取捕获统计失败: %s", exc)
            return CaptureStats(fps=0.0, dropped=0, last_error=str(exc))

    def _bind_degraded(self) -> None:
        binder = getattr(self._manager, "on_degraded", None)
        if binder is None:
            return
        try:
            binder(self._handle_degraded)
        except Exception as exc:  # 测试替身不一定支持
            _log.debug("注册降级回调失败（已忽略）: %s", exc)

    def _handle_degraded(self, name: str) -> None:
        _log.warning("捕获后端降级: %s", name)
        cb = self._degraded_cb
        if cb is None:
            return
        try:
            cb(name)
        except Exception as exc:
            _log.debug("降级回调异常（已忽略）: %s", exc)

    def _reset_counters(self) -> None:
        self._stop_evt = threading.Event()
        self._first_frame_evt = threading.Event()
        self._error = None
        self._t0 = None
        self._last_ts_rel = 0.0
        self._frames = 0
        self._black_frames = 0
        self._black_run = 0
        self._ever_black = False
        self._sampler = None
        self._sampler_size = None
        self._score = None
        self._last_stats = None

    def _reset_after_failure(self) -> None:
        """启动失败后的复位：状态回 IDLE，清掉本次录制的临时对象。"""
        self.state = SessionState.IDLE
        self._thread = None
        self._recorder = None
        self._grid = None
        self._sampler = None
        self._sampler_size = None
        self._on_preview = None
        self._score = None
        # 换成新的事件对象：下次 start_record 前保证是"未触发"状态
        self._stop_evt = threading.Event()
        self._first_frame_evt = threading.Event()
