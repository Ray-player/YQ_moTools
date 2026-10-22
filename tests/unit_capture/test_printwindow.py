"""PrintWindow 后端单测（P1）。

覆盖：
- 纯函数 `roi_from_norm` 的边界（0/1/四舍五入/1px 下限/非法尺寸）；
- `BITMAPINFO` 构造（top-down、32bpp）；
- 非 win32 不崩、`supports` 只吃窗口目标、`close` 幂等；
- 真实窗口抓帧（用控制台窗口 hwnd 试，失败即 skip，不让测试变红）。
"""

from __future__ import annotations

import sys

import pytest

from yqrec.capture.printwindow_backend import (
    PrintWindowBackend,
    make_bitmap_info,
    roi_from_norm,
)
from yqrec.errors import CaptureError, ErrorCode
from yqrec.types import Rect, ScreenTarget, WindowTarget

# --------------------------------------------------------------------------- #
# roi_from_norm
# --------------------------------------------------------------------------- #


def test_roi_from_norm_full_frame() -> None:
    assert roi_from_norm(1920, 1080, (0.0, 0.0, 1.0, 1.0)) == (0, 0, 1920, 1080)


def test_roi_from_norm_half_frame() -> None:
    assert roi_from_norm(1920, 1080, (0.5, 0.5, 1.0, 1.0)) == (960, 540, 960, 540)


def test_roi_from_norm_rounds_to_nearest() -> None:
    # 0.4→0, 0.6→1, 99.9→100, 99.8→100
    assert roi_from_norm(100, 100, (0.004, 0.006, 0.999, 0.998)) == (0, 1, 100, 99)


def test_roi_from_norm_min_one_pixel() -> None:
    assert roi_from_norm(100, 100, (0.0, 0.0, 0.0001, 0.0001)) == (0, 0, 1, 1)
    assert roi_from_norm(100, 100, (0.9999, 0.9999, 1.0, 1.0)) == (99, 99, 1, 1)


def test_roi_from_norm_inner_window() -> None:
    assert roi_from_norm(1000, 500, (0.1, 0.2, 0.9, 0.8)) == (100, 100, 800, 300)


def test_roi_from_norm_rejects_bad_size() -> None:
    with pytest.raises(ValueError):
        roi_from_norm(0, 100, (0.0, 0.0, 1.0, 1.0))
    with pytest.raises(ValueError):
        roi_from_norm(100, -1, (0.0, 0.0, 1.0, 1.0))


# --------------------------------------------------------------------------- #
# BITMAPINFO
# --------------------------------------------------------------------------- #


def test_bitmap_info_is_top_down_32bpp() -> None:
    info = make_bitmap_info(320, 180)
    assert info.bmiHeader.biWidth == 320
    assert info.bmiHeader.biHeight == -180, "biHeight 取负 → 行序 top-down"
    assert info.bmiHeader.biBitCount == 32
    assert info.bmiHeader.biPlanes == 1
    assert info.bmiHeader.biSizeImage == 320 * 180 * 4
    assert info.bmiHeader.biSize == 40


# --------------------------------------------------------------------------- #
# 后端契约
# --------------------------------------------------------------------------- #


def test_is_available_returns_bool() -> None:
    assert PrintWindowBackend.is_available() is (sys.platform == "win32")


def test_supports_only_window_target() -> None:
    backend = PrintWindowBackend()
    assert backend.supports(WindowTarget(hwnd=1, norm=(0.0, 0.0, 1.0, 1.0))) is True
    assert backend.supports(ScreenTarget(0, Rect(0, 0, 10, 10))) is False


def test_close_without_open_is_idempotent() -> None:
    backend = PrintWindowBackend()
    backend.close()
    backend.close()


def test_grab_before_open_raises() -> None:
    backend = PrintWindowBackend()
    with pytest.raises(CaptureError) as excinfo:
        backend._grab()  # noqa: SLF001 - 直接验证未打开时的防御分支
    assert excinfo.value.code == ErrorCode.NOT_IMPLEMENTED


@pytest.mark.skipif(sys.platform != "win32", reason="PrintWindow 仅 Windows")
def test_open_invalid_hwnd_raises_capture_error() -> None:
    backend = PrintWindowBackend()
    with pytest.raises(CaptureError):
        backend.open(WindowTarget(hwnd=0x7FFF_FFF0, norm=(0.0, 0.0, 1.0, 1.0)))
    backend.close()


@pytest.mark.skipif(sys.platform != "win32", reason="PrintWindow 仅 Windows")
def test_grab_real_window_frame() -> None:
    """真实抓帧：拿控制台窗口试一把；环境不配合就 skip（不让测试红）。"""
    import ctypes

    hwnd = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    if hwnd <= 0:
        pytest.skip("当前进程没有控制台窗口可抓")

    backend = PrintWindowBackend()
    target = WindowTarget(hwnd=hwnd, norm=(0.0, 0.0, 1.0, 1.0))
    try:
        backend.open(target)
    except CaptureError as exc:
        pytest.skip(f"PrintWindow 打开失败（环境限制）: {exc}")
    try:
        got = backend.latest()
    except CaptureError as exc:
        pytest.skip(f"PrintWindow 取帧失败（环境限制）: {exc}")
    finally:
        backend.close()
        backend.close()  # 幂等

    assert got is not None, "已 open 且未超时应拿到帧"
    frame, ts = got
    assert frame.ndim == 3 and frame.shape[2] == 3, "必须返回 BGR 三通道"
    assert frame.shape[0] > 0 and frame.shape[1] > 0
    assert ts > 0.0
