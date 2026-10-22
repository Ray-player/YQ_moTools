"""Win32 窗口辅助：枚举、客户区/屏幕坐标换算（03 §3.1）。

只在 Windows 下可用；非 Windows 导入本模块会抛 RuntimeError（延迟到调用时）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    visible: bool


def _require_windows() -> None:
    if _user32 is None:
        raise RuntimeError("窗口相关功能仅支持 Windows")


def get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区 (left, top, right, bottom)，屏幕坐标。"""
    _require_windows()
    rect = wt.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetClientRect 失败: hwnd={hwnd}")
    pt = wt.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    return (pt.x, pt.y, pt.x + w, pt.y + h)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    _require_windows()
    rect = wt.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect 失败: hwnd={hwnd}")
    return (rect.left, rect.top, rect.right, rect.bottom)


def find_window(title_substr: str) -> int | None:
    """按标题子串找第一个可见窗口。"""
    _require_windows()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_substr.lower() in buf.value.lower():
            found.append(int(hwnd))
            return False
        return True

    _user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def is_window_alive(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsWindow(hwnd))
