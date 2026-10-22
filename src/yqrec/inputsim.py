"""前台键鼠注入（借鉴 BetterGI 的 SendInput 通道，见 01 §15 技术总结）。

只实现本项目当前需要的能力：

- **模拟一次鼠标左键点击**（`record_click` 模式 2）；
- **把窗口激活为前台后模拟一次空格键**（`record_click` 模式 1），
  激活流程抄 MAA ``ForegroundUtils.h`` 的三段式（提层 → SetForegroundWindow
  → 复核，失败再 TOPMOST↔NOTOPMOST 强提）。

后续要扩展时保持三条约定：

- 坐标一律用**全局物理像素**（与框选区域/截图后端同口径）；
  进程已声明 Per-Monitor DPI aware，SetCursorPos/SendInput 按物理像素解析。
- 扩展键位要带 extended 位（空格/左键均非扩展键，暂无此问题）。
- 注入失败只记 warning 返回 False，绝不抛异常打断业务流。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

from .logging import get_logger

_log = get_logger("inputsim")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_SPACE = 0x20
HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send_mouse(flags: int) -> bool:
    """投递一条鼠标事件；失败记 warning 返回 False。"""
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi.dwFlags = flags
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        _log.warning("SendInput(0x%x) 失败: winerror=%s", flags, ctypes.get_last_error())
        return False
    return True


def _send_key(vk: int, flags: int) -> bool:
    """投递一条键盘事件；失败记 warning 返回 False。"""
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki.wVk = vk
    inp.ki.dwFlags = flags
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        _log.warning("SendInput(VK=0x%x, 0x%x) 失败: winerror=%s", vk, flags, ctypes.get_last_error())
        return False
    return True


def left_click(x: int, y: int, *, hold_ms: int = 60, restore_cursor: bool = True) -> bool:
    """在全局物理像素 ``(x, y)`` 处模拟一次左键按下→抬起。

    - 先把光标移到目标点（注入坐标点击的前提），点击后**恢复光标原位**
      （借鉴 MAA WithCursorPos 的做法，尽量不打扰用户）。
    - ``hold_ms`` 是按下与抬起之间的间隔（过短有些程序会把 down/up 判成滚屏/拖动）。
    - 返回 True = down/up 都成功；False = 中途失败（光标仍会尽量复位）。
    """
    x, y = int(x), int(y)
    saved: tuple[int, int] | None = None
    if restore_cursor:
        pt = wt.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            saved = (pt.x, pt.y)
        else:
            _log.warning("GetCursorPos 失败: winerror=%s（点击后无法恢复光标）", ctypes.get_last_error())

    if not user32.SetCursorPos(x, y):
        _log.warning("SetCursorPos(%d, %d) 失败: winerror=%s", x, y, ctypes.get_last_error())
        _restore(saved)
        return False

    ok = True
    try:
        ok = _send_mouse(MOUSEEVENTF_LEFTDOWN) and ok
        time.sleep(max(0, int(hold_ms)) / 1000.0)
        ok = _send_mouse(MOUSEEVENTF_LEFTUP) and ok
    finally:
        _restore(saved)
    return ok


def _restore(saved: tuple[int, int] | None) -> None:
    """光标复位；失败只记 warning（复位失败不该影响业务判断）。"""
    if saved is None:
        return
    if not user32.SetCursorPos(saved[0], saved[1]):
        _log.warning("恢复光标到 (%d, %d) 失败: winerror=%s", *saved, ctypes.get_last_error())


def press_space(*, hold_ms: int = 60) -> bool:
    """向前台窗口模拟一次空格按下→抬起（SendInput，VK_SPACE 非扩展键）。

    **调用方必须先确认目标窗口确实在前台**（见 ``activate_window``）——
    空格落到本程序焦点控件上会触发按钮（如再次点中「录制」立即停录）。
    """
    ok = _send_key(VK_SPACE, 0)
    time.sleep(max(0, int(hold_ms)) / 1000.0)
    ok = _send_key(VK_SPACE, KEYEVENTF_KEYUP) and ok
    return ok


def window_at(x: int, y: int) -> int:
    """返回全局物理像素点 ``(x, y)`` 处的**顶层窗口**句柄（找不到返回 0）。

    WindowFromPoint 可能命中子窗口/宿主控件，用 GetAncestor(GA_ROOT) 归一到顶层。
    """
    pt = wt.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        _log.warning("WindowFromPoint(%d, %d) 无命中", x, y)
        return 0
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root or hwnd)


def is_own_window(hwnd: int) -> bool:
    """判断窗口是否属于**本进程**（命中自己时不能激活/注入，否则空格会打到自己）。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return bool(pid.value) and pid.value == kernel32.GetCurrentProcessId()


def activate_window(hwnd: int) -> bool:
    """把 ``hwnd`` 激活为前台键盘焦点；**返回前用 GetForegroundWindow 复核**。

    抄 MAA ``ForegroundUtils.h`` 三段式：SetWindowPos(HWND_TOP) 提层 →
    SetForegroundWindow → 复核；失败再 TOPMOST↔NOTOPMOST 强提一次
    （部分前台锁定场景下能绕过）。注意 TOPMOST 只保证窗口在最前、
    不保证键盘焦点，所以**最终以复核结果为准**。

    本项目在「用户刚点完录制按钮」的时机调用（调用进程持有最后输入事件），
    命中 Microsoft 放行条件，首次 SetForegroundWindow 大概率直接成功。
    """
    if not hwnd:
        return False
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return True
    # 强提：TOPMOST↔NOTOPMOST 翻转后再试一次（MAA 同款兜底）
    topmost_flag = user32.GetWindowLong(hwnd, -20) & 0x8  # GWL_EXSTYLE, WS_EX_TOPMOST
    insert_after = HWND_NOTOPMOST if topmost_flag else HWND_TOPMOST
    user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    ok = bool(user32.SetForegroundWindow(hwnd)) and user32.GetForegroundWindow() == hwnd
    if not ok:
        _log.warning("activate_window(0x%x) 复核失败（前台锁定未放行）", hwnd)
    return ok
