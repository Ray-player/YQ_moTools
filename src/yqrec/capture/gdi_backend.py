"""GDI（mss / BitBlt）后端：屏幕区域兜底（03 §4.1）。"""

from __future__ import annotations

import numpy as np

from ..types import CaptureTarget, ScreenTarget
from .base import BaseBackend


class GdiBackend(BaseBackend):
    name = "gdi"

    def __init__(self) -> None:
        super().__init__()
        self._sct = None
        self._rect = None

    @classmethod
    def is_available(cls) -> bool:
        try:
            import mss  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, ScreenTarget)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        import mss

        assert isinstance(target, ScreenTarget)
        self._sct = mss.mss()
        r = target.rect
        self._rect = {"left": r.left, "top": r.top, "width": r.width, "height": r.height}

    def _grab(self) -> tuple[np.ndarray, float] | None:
        import time

        assert self._sct is not None and self._rect is not None
        img = self._sct.grab(self._rect)  # BGRA
        arr = np.asarray(img, dtype=np.uint8)[:, :, :3]
        return arr, time.perf_counter()

    def _close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            finally:
                self._sct = None
                self._rect = None
