"""`BaseBackend.latest()` 的错误码透传与丢帧统计（P1 补丁）。

规则：
- 子类 `_grab` 抛 `CaptureError` → **原样透传**（保留 `CAPTURE_TIMEOUT` 等语义码），
  但仍计入 `dropped` 并记录 `last_error`；
- 子类 `_grab` 抛其它异常 → 包成 `CaptureError(CAPTURE_OPEN)`；
- `_grab` 返回 None → 计入 `dropped`，不抛。
"""

from __future__ import annotations

import numpy as np
import pytest

from yqrec.capture.base import BaseBackend
from yqrec.errors import CaptureError, ErrorCode
from yqrec.types import CaptureTarget, Rect, ScreenTarget


class _RaiseCaptureErrorBackend(BaseBackend):
    name = "raise-capture-error"

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        return None

    def _grab(self) -> tuple[np.ndarray, float] | None:
        raise CaptureError("PrintWindow 返回 0", code=ErrorCode.CAPTURE_TIMEOUT)

    def _close(self) -> None:
        return None


class _RaiseValueErrorBackend(BaseBackend):
    name = "raise-value-error"

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        return None

    def _grab(self) -> tuple[np.ndarray, float] | None:
        raise ValueError("驱动炸了")

    def _close(self) -> None:
        return None


class _NoneBackend(BaseBackend):
    name = "none"

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        return None

    def _grab(self) -> tuple[np.ndarray, float] | None:
        return None

    def _close(self) -> None:
        return None


def _open(backend: BaseBackend) -> BaseBackend:
    backend.open(ScreenTarget(monitor=0, rect=Rect(0, 0, 8, 8)))
    return backend


def test_latest_preserves_semantic_error_code() -> None:
    backend = _open(_RaiseCaptureErrorBackend())
    with pytest.raises(CaptureError) as excinfo:
        backend.latest()
    assert excinfo.value.code == ErrorCode.CAPTURE_TIMEOUT
    stats = backend.stats()
    assert stats.dropped == 1, "透传错误码也必须计入丢帧"
    assert stats.last_error is not None and "PrintWindow" in stats.last_error
    backend.close()


def test_latest_wraps_unexpected_exception_as_open_error() -> None:
    backend = _open(_RaiseValueErrorBackend())
    with pytest.raises(CaptureError) as excinfo:
        backend.latest()
    assert excinfo.value.code == ErrorCode.CAPTURE_OPEN
    assert "取帧失败" in str(excinfo.value)
    stats = backend.stats()
    assert stats.dropped == 1
    assert stats.last_error is not None and "驱动炸了" in stats.last_error
    backend.close()


def test_latest_counts_drop_when_no_frame() -> None:
    backend = _open(_NoneBackend())
    assert backend.latest() is None
    assert backend.latest() is None
    assert backend.stats().dropped == 2
    backend.close()


def test_latest_before_open_raises_timeout() -> None:
    with pytest.raises(CaptureError) as excinfo:
        _NoneBackend().latest()
    assert excinfo.value.code == ErrorCode.CAPTURE_TIMEOUT
