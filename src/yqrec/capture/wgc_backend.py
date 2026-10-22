"""Windows Graphics Capture 后端（窗口目标首选，03 §4.1）。

依赖 `windows-capture`（Rust 绑定）。回调式 API → 内部转成"最新帧槽"。
注意：该库版本间接口可能有差异，`_extract_frame` 做了多字段兼容；
P0 需在目标机上实测确认字段与像素格式（03 §11 未定项）。
"""

from __future__ import annotations

import threading
import time

import numpy as np

from ..errors import CaptureError
from ..types import CaptureTarget, WindowTarget
from .base import BaseBackend


class WgcBackend(BaseBackend):
    name = "wgc"

    def __init__(self) -> None:
        super().__init__()
        self._capture = None
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._lock = threading.Lock()
        self._norm = (0.0, 0.0, 1.0, 1.0)

    @classmethod
    def is_available(cls) -> bool:
        try:
            import windows_capture  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, WindowTarget)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        from windows_capture import WindowsCapture

        assert isinstance(target, WindowTarget)
        if not target.title:
            raise CaptureError("WGC 需要窗口标题（WindowTarget.title）")
        self._norm = target.norm
        cap = WindowsCapture(cursor_capture=None, draw_border=None, window_name=target.title)

        @cap.event
        def on_frame_arrived(frame, capture_control) -> None:  # type: ignore[no-untyped-def]
            arr = _extract_frame(frame)
            if arr is None:
                return
            with self._lock:
                self._frame = _crop(arr, self._norm)
                self._ts = time.perf_counter()

        @cap.event
        def on_closed() -> None:
            with self._lock:
                self._frame = None

        cap.start()
        self._capture = cap

    def _grab(self) -> tuple[np.ndarray, float] | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy(), self._ts

    def _close(self) -> None:
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass
        with self._lock:
            self._frame = None


def _extract_frame(frame) -> np.ndarray | None:  # type: ignore[no-untyped-def]
    """兼容不同版本的帧字段：frame_buffer / buffer / numpy 数组。"""
    for attr in ("frame_buffer", "buffer"):
        buf = getattr(frame, attr, None)
        if buf is not None:
            arr = np.asarray(buf, dtype=np.uint8)
            return arr[:, :, :3] if arr.ndim == 3 and arr.shape[2] >= 3 else arr
    arr = np.asarray(frame, dtype=np.uint8)
    return arr if arr.ndim == 3 else None


def _crop(arr: np.ndarray, norm: tuple[float, float, float, float]) -> np.ndarray:
    h, w = arr.shape[:2]
    u0, v0, u1, v1 = norm
    x0, x1 = int(u0 * w), max(int(u1 * w), int(u0 * w) + 1)
    y0, y1 = int(v0 * h), max(int(v1 * h), int(v0 * h) + 1)
    return arr[y0:y1, x0:x1].copy()
