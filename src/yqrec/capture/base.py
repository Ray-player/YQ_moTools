"""捕获后端协议与基类（03 §2/§4.1）。"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import numpy as np

from ..errors import CaptureError, ErrorCode
from ..types import CaptureStats, CaptureTarget


@runtime_checkable
class CaptureBackend(Protocol):
    """所有后端统一契约：输出 BGR uint8 帧 + perf_counter 时间戳（秒）。"""

    name: str

    @classmethod
    def is_available(cls) -> bool:
        """依赖是否安装、平台是否支持（不实际打开捕获）。"""
        ...

    def supports(self, target: CaptureTarget) -> bool:
        """该目标类型是否支持（ScreenTarget / WindowTarget）。"""
        ...

    def open(self, target: CaptureTarget, *, target_fps: int = 60) -> None: ...

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        """返回 (BGR frame, ts)；暂无新帧返回 None。"""
        ...

    def close(self) -> None: ...

    def stats(self) -> CaptureStats: ...


class BaseBackend:
    """统计与错误包装，子类实现 `_open/_grab/_close`。"""

    name = "base"

    def __init__(self) -> None:
        self._opened = False
        self._frames = 0
        self._dropped = 0
        self._t0 = 0.0
        self._last_error: str | None = None

    # -- 供子类实现 -------------------------------------------------------- #

    def _open(self, target: CaptureTarget, target_fps: int) -> None:
        raise NotImplementedError

    def _grab(self) -> tuple[np.ndarray, float] | None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    # -- 公共流程 ---------------------------------------------------------- #

    def open(self, target: CaptureTarget, *, target_fps: int = 60) -> None:
        if self._opened:
            self.close()
        try:
            self._open(target, target_fps)
        except CaptureError:
            raise
        except Exception as exc:  # 驱动/权限/窗口消失
            self._last_error = str(exc)
            raise CaptureError(f"{self.name} 打开失败", detail=str(exc)) from exc
        self._opened = True
        self._frames = 0
        self._dropped = 0
        self._t0 = time.perf_counter()

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        if not self._opened:
            raise CaptureError("后端尚未打开", code=ErrorCode.CAPTURE_TIMEOUT)
        try:
            got = self._grab()
        except CaptureError as exc:
            # 子类已给出语义错误码（如 CAPTURE_TIMEOUT）：原样透传，但仍计入丢帧与最后错误
            self._last_error = str(exc)
            self._dropped += 1
            raise
        except Exception as exc:
            self._last_error = str(exc)
            self._dropped += 1
            raise CaptureError(f"{self.name} 取帧失败", detail=str(exc)) from exc
        if got is None:
            self._dropped += 1
            return None
        self._frames += 1
        return got

    def close(self) -> None:
        if self._opened:
            try:
                self._close()
            finally:
                self._opened = False

    def stats(self) -> CaptureStats:
        elapsed = max(1e-6, time.perf_counter() - self._t0) if self._opened else 0.0
        fps = self._frames / elapsed if elapsed > 0 else 0.0
        return CaptureStats(fps=fps, dropped=self._dropped, last_error=self._last_error)
