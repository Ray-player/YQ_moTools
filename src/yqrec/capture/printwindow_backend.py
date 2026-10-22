"""PrintWindow 后端：窗口兜底（P1）。

链路：`GetClientRect` → `GetDC` → `CreateCompatibleDC/Bitmap` → `PrintWindow(hwnd, dc, 2)`
→ `GetDIBits` 取 BGRA（top-down）→ 切 `[:, :, :3]` 得 BGR。

两个关键设计：

1. **GDI 对象只创建一次**（DC / Bitmap 在 `_open` 里建好，`_close` 里释放），
   每帧只做 `PrintWindow` + `GetDIBits`，避免逐帧分配句柄导致 GDI 泄漏；
2. `PrintWindow` 永远把**整个客户区**画到目标 DC 的 (0,0)，因此位图按客户区尺寸创建，
   `WindowTarget.norm` 换算出 ROI 后在 `_grab` 里用 numpy 切片裁出
   （`roi_from_norm` 是纯函数，可脱离窗口单测）。

非 Windows 平台：`is_available()` 返回 False，`_open` 抛 `CaptureError(NOT_IMPLEMENTED)`。
"""

from __future__ import annotations

import ctypes
import sys
import time
from contextlib import suppress
from typing import Any

import numpy as np

from ..errors import CaptureError, ErrorCode
from ..types import CaptureTarget, WindowTarget
from .base import BaseBackend

PW_RENDERFULLCONTENT = 2
"""`PrintWindow` 的 flag：让 DWM 内容（含 GPU 渲染）也能画进 DC。"""

DIB_RGB_COLORS = 0
BI_RGB = 0
"""`BITMAPINFOHEADER.biCompression`：不压缩。"""


class BITMAPINFOHEADER(ctypes.Structure):
    """`BITMAPINFOHEADER`（40 字节；纯 ctypes 结构，跨平台可定义）。"""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    """`BITMAPINFO` = 头 + 调色板占位（BI_RGB 32bpp 不读调色板）。"""

    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


def roi_from_norm(
    width: int, height: int, norm: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    """把窗口归一化 ROI 换算成客户区像素矩形 `(x, y, w, h)`。

    - 坐标四舍五入（`int(round(...))`），与 03 §3.1 的 `target_to_frame_rect` 一致；
    - 结果保证落在客户区内，且宽高至少 1px。

    Args:
        width: 客户区宽度（像素）。
        height: 客户区高度（像素）。
        norm: `(u0, v0, u1, v1)`，0..1。

    Returns:
        `(x, y, w, h)`，裁剪用。

    Raises:
        ValueError: 客户区尺寸非正。
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"客户区尺寸必须为正: {width}x{height}")
    u0, v0, u1, v1 = norm
    x0 = min(max(0, int(round(width * float(u0)))), width - 1)
    y0 = min(max(0, int(round(height * float(v0)))), height - 1)
    x1 = min(max(x0 + 1, int(round(width * float(u1)))), width)
    y1 = min(max(y0 + 1, int(round(height * float(v1)))), height)
    return (x0, y0, x1 - x0, y1 - y0)


def make_bitmap_info(width: int, height: int) -> BITMAPINFO:
    """构造 top-down 的 32bpp `BITMAPINFO`（`biHeight` 取负 → 行序从上到下）。"""
    info = BITMAPINFO()
    header = info.bmiHeader
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = int(width)
    header.biHeight = -int(height)
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    header.biSizeImage = int(width) * int(height) * 4
    header.biXPelsPerMeter = 0
    header.biYPelsPerMeter = 0
    header.biClrUsed = 0
    header.biClrImportant = 0
    return info


def _load_win32() -> tuple[Any, Any, Any]:
    """加载并声明 user32/gdi32 的函数签名（64 位下句柄必须显式声明）。"""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    return user32, gdi32, wintypes


class PrintWindowBackend(BaseBackend):
    """窗口兜底后端：靠 `PrintWindow` 抓客户区（含被遮挡窗口）。

    优点：不需要 DWM/DXGI，被遮挡也能出图；
    缺点：帧率较低、部分游戏/DRM 画面全黑 —— 因此只作为 `wgc` 之后的兜底。
    """

    name = "printwindow"

    def __init__(self) -> None:
        super().__init__()
        self._user32: Any = None
        self._gdi32: Any = None
        self._hwnd: int = 0
        self._hdc_win: Any = None
        self._mem_dc: Any = None
        self._bitmap: Any = None
        self._old_bitmap: Any = None
        self._buf: np.ndarray | None = None
        self._info: BITMAPINFO | None = None
        self._size: tuple[int, int] = (0, 0)
        """位图尺寸 = **窗口矩形**（PrintWindow 连标题栏一起画）。"""
        self._client_size: tuple[int, int] = (0, 0)
        self._offset: tuple[int, int] = (0, 0)
        """客户区左上角在窗口位图中的偏移 `(dx, dy)`（标题栏/边框）。"""
        self._roi: tuple[int, int, int, int] = (0, 0, 1, 1)

    @classmethod
    def is_available(cls) -> bool:
        return sys.platform == "win32"

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, WindowTarget)

    # -- 生命周期 ---------------------------------------------------------- #

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        if sys.platform != "win32":
            raise CaptureError(
                "PrintWindow 仅在 Windows 上可用", code=ErrorCode.NOT_IMPLEMENTED
            )
        if not isinstance(target, WindowTarget):
            raise CaptureError("PrintWindow 仅支持 WindowTarget（窗口目标）")

        user32, gdi32, wintypes = _load_win32()
        hwnd = int(target.hwnd)
        if not user32.IsWindow(hwnd):
            raise CaptureError(f"窗口不存在或已销毁: hwnd=0x{hwnd:X}")

        client = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            raise CaptureError(f"GetClientRect 失败: hwnd=0x{hwnd:X}")
        client_w = int(client.right - client.left)
        client_h = int(client.bottom - client.top)
        if client_w <= 0 or client_h <= 0:
            raise CaptureError(f"客户区尺寸非法: {client_w}x{client_h}")

        window = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
            raise CaptureError(f"GetWindowRect 失败: hwnd=0x{hwnd:X}")
        win_w = int(window.right - window.left)
        win_h = int(window.bottom - window.top)
        if win_w <= 0 or win_h <= 0:
            raise CaptureError(f"窗口尺寸非法: {win_w}x{win_h}")

        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise CaptureError(f"ClientToScreen 失败: hwnd=0x{hwnd:X}")
        # 客户区在"窗口位图"里的偏移：PrintWindow 连标题栏/边框一起画，必须补偿
        offset_x = int(origin.x - window.left)
        offset_y = int(origin.y - window.top)

        roi = roi_from_norm(client_w, client_h, target.norm)

        hdc = user32.GetDC(hwnd)
        if not hdc:
            raise CaptureError(f"GetDC 失败: hwnd=0x{hwnd:X}")
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        if not mem_dc:
            user32.ReleaseDC(hwnd, hdc)
            raise CaptureError("CreateCompatibleDC 失败")
        bitmap = gdi32.CreateCompatibleBitmap(hdc, win_w, win_h)
        if not bitmap:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hdc)
            raise CaptureError("CreateCompatibleBitmap 失败")

        self._user32 = user32
        self._gdi32 = gdi32
        self._hwnd = hwnd
        self._hdc_win = hdc
        self._mem_dc = mem_dc
        self._bitmap = bitmap
        self._old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
        self._size = (win_w, win_h)
        self._client_size = (client_w, client_h)
        self._offset = (offset_x, offset_y)
        self._roi = roi
        # 预分配 BGRA 缓冲：GetDIBits 直接写进 numpy 内存，避免每帧分配
        self._buf = np.empty((win_h, win_w, 4), dtype=np.uint8)
        self._info = make_bitmap_info(win_w, win_h)

    def _grab(self) -> tuple[np.ndarray, float] | None:
        if (
            self._user32 is None
            or self._gdi32 is None
            or self._mem_dc is None
            or self._bitmap is None
            or self._buf is None
            or self._info is None
        ):
            raise CaptureError(
                "PrintWindow 后端尚未打开", code=ErrorCode.NOT_IMPLEMENTED
            )
        user32 = self._user32
        gdi32 = self._gdi32
        if not user32.IsWindow(self._hwnd):
            raise CaptureError(
                f"窗口已销毁: hwnd=0x{self._hwnd:X}", code=ErrorCode.CAPTURE_TIMEOUT
            )
        if not user32.PrintWindow(self._hwnd, self._mem_dc, PW_RENDERFULLCONTENT):
            raise CaptureError(
                "PrintWindow 返回 0（窗口可能最小化、被独占或使用了 DRM）",
                code=ErrorCode.CAPTURE_TIMEOUT,
            )

        width, height = self._size
        copied = gdi32.GetDIBits(
            self._mem_dc,
            self._bitmap,
            0,
            height,
            self._buf.ctypes.data,
            ctypes.byref(self._info),
            DIB_RGB_COLORS,
        )
        if not copied:
            raise CaptureError("GetDIBits 失败", code=ErrorCode.CAPTURE_TIMEOUT)

        offset_x, offset_y = self._offset
        x0, y0, w, h = self._roi
        left = max(0, min(offset_x + x0, width - 1))
        top = max(0, min(offset_y + y0, height - 1))
        right = max(left + 1, min(offset_x + x0 + w, width))
        bottom = max(top + 1, min(offset_y + y0 + h, height))
        frame = self._buf[top:bottom, left:right, :3].copy()  # BGRA → BGR
        return frame, time.perf_counter()

    def _close(self) -> None:
        gdi32 = self._gdi32
        user32 = self._user32
        # 每一步都容错：close() 可能被重复调用，或在半开状态下调用
        if gdi32 is not None and self._mem_dc is not None and self._old_bitmap:
            # 先把原对象选回去，否则 DeleteObject 对"仍被选中的位图"无效
            with suppress(Exception):
                gdi32.SelectObject(self._mem_dc, self._old_bitmap)
        if gdi32 is not None and self._bitmap is not None:
            with suppress(Exception):
                gdi32.DeleteObject(self._bitmap)
        if gdi32 is not None and self._mem_dc is not None:
            with suppress(Exception):
                gdi32.DeleteDC(self._mem_dc)
        if user32 is not None and self._hdc_win is not None:
            with suppress(Exception):
                user32.ReleaseDC(self._hwnd, self._hdc_win)

        self._user32 = None
        self._gdi32 = None
        self._hwnd = 0
        self._hdc_win = None
        self._mem_dc = None
        self._bitmap = None
        self._old_bitmap = None
        self._buf = None
        self._info = None
        self._size = (0, 0)
        self._client_size = (0, 0)
        self._offset = (0, 0)
        self._roi = (0, 0, 1, 1)
