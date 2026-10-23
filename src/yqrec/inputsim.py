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
from pathlib import Path

from .logging import get_logger

_log = get_logger("inputsim")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
UOI_NAME = 2
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_SPACE = 0x20
HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
"""UIPI 判定用：目标进程是否提权（管理员）。不匹配时系统会**静默丢弃**注入的键鼠。"""

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.GetTokenInformation.argtypes = [
    wt.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wt.UINT
kernel32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
kernel32.WTSGetActiveConsoleSessionId.restype = wt.DWORD
user32.GetThreadDesktop.argtypes = [wt.DWORD]
user32.GetThreadDesktop.restype = wt.HANDLE
user32.GetUserObjectInformationW.argtypes = [
    wt.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND

# `GetWindowLong` 只是 32 位 SDK 的宏，64 位 user32 里没有这个导出名（只有 …W / …PtrW）。
# 老代码直接 `user32.GetWindowLong(...)` 在 64 位下会 AttributeError —— 只在"强提兜底"分支
# 才会走到，所以线上没暴露（首次 SetForegroundWindow 通常就成功）。
_get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_get_window_long.argtypes = [wt.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_ssize_t


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


def _send_mouse(flags: int, dx: int = 0, dy: int = 0) -> bool:
    """投递一条鼠标事件；失败记 warning 返回 False。"""
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.dwFlags = flags
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        _log.warning("SendInput(0x%x) 失败: winerror=%s", flags, ctypes.get_last_error())
        return False
    return True


def _virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面（所有显示器并集）的 `(left, top, width, height)`。"""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def _cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def move_cursor(x: int, y: int) -> bool:
    """把光标移到全局物理像素 `(x, y)`；先走 SendInput 绝对移动，再退回 SetCursorPos。

    为什么不能只靠 SetCursorPos：实测（2026-10-23 用户实机）它会**静默失败**
    （返回 0、`GetLastError()` 还是 0），鼠标纹丝不动 —— 这类环境下
    `SendInput(MOUSEEVENTF_ABSOLUTE|MOUSEEVENTF_VIRTUALDESK)` 仍然有效，因为它走的是
    **输入队列**（和真实鼠标同一条路），不依赖调用进程对桌面的直接访问权。
    """
    left, top, width, height = _virtual_screen()
    if width > 1 and height > 1:
        nx = int(round((x - left) * 65535 / (width - 1)))
        ny = int(round((y - top) * 65535 / (height - 1)))
        nx = max(0, min(65535, nx))
        ny = max(0, min(65535, ny))
        if _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny):
            return True
    if user32.SetCursorPos(int(x), int(y)):
        return True
    _log.warning(
        "移动光标到 (%d, %d) 失败：SendInput 绝对移动 + SetCursorPos 都没成功"
        "（winerror=%s）",
        x,
        y,
        ctypes.get_last_error(),
    )
    return False


def _send_key(vk: int, flags: int) -> bool:
    """投递一条键盘事件；失败记 warning 返回 False。

    `flags` 含 `KEYEVENTF_SCANCODE` 时按**扫描码**发（`wVk` 必须为 0）—— DirectInput /
    RawInput 的游戏（原神这类 Unity 游戏）常常只认扫描码，VK-only 的注入会被它们无视。
    """
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki.wVk = 0 if flags & KEYEVENTF_SCANCODE else vk
    inp.ki.dwFlags = flags
    if flags & KEYEVENTF_SCANCODE:
        inp.ki.wScan = int(user32.MapVirtualKeyW(vk, 0))  # MAPVK_VK_TO_VSC
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        _log.warning("SendInput(VK=0x%x, 0x%x) 失败: winerror=%s", vk, flags, ctypes.get_last_error())
        return False
    return True


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


def process_name(pid: int) -> str:
    """进程名（读不到返回空串；受保护进程会被拒）。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
    except Exception:  # pragma: no cover - 极端情况
        return ""
    finally:
        kernel32.CloseHandle(handle)
    return ""


def is_elevated(pid: int) -> bool | None:
    """进程是否以管理员（高完整性）运行；读不到返回 None（受保护进程）。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            info = _TOKEN_ELEVATION()
            size = wt.DWORD(ctypes.sizeof(info))
            ok = advapi32.GetTokenInformation(
                token,
                TokenElevation,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(size),
            )
            return bool(info.TokenIsElevated) if ok else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def window_process(hwnd: int) -> tuple[int, str]:
    """窗口所属进程 `(pid, 进程名)`；失败返回 `(0, "")`。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return (int(pid.value), process_name(pid.value)) if pid.value else (0, "")


def window_title(hwnd: int, limit: int = 60) -> str:
    """窗口标题（诊断日志用；太长截断，避免一整行被标题占满）。"""
    length = user32.GetWindowTextLengthW(int(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(int(hwnd), buf, length + 1)
    text = buf.value.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def foreground_window() -> int:
    """当前前台窗口句柄（0 = 没有前台窗口）。"""
    return int(user32.GetForegroundWindow() or 0)


def describe(hwnd: int) -> str:
    """`0x1059a msedge.exe「标题…」`：日志里写清"打到谁身上了"，一行够用。"""
    if not hwnd:
        return "0x0（无窗口）"
    pid, name = window_process(hwnd)
    title = window_title(hwnd)
    tail = f"「{title}」" if title else ""
    return f"0x{hwnd:x} {name or '?'}{tail}"


def injection_hint(hwnd: int) -> str:
    """判断"注入了也不会生效"的典型情形，返回给界面的提示；正常返回空串。

    最常见的一种：**目标进程以管理员运行，本程序没有** —— Windows 的 UIPI 会把低完整性
    进程注入的键鼠**静默丢弃**，`SendInput` 依然返回成功，于是日志里有记录、游戏没反应。
    （2026-10-23 用户实机就是这么回事：日志有"注入"行，但游戏没动。）
    """
    pid, name = window_process(hwnd)
    if not pid:
        return ""
    target = is_elevated(pid)
    if target is None:
        return ""
    ours = is_elevated(kernel32.GetCurrentProcessId())
    if target and not ours:
        label = name or f"PID {pid}"
        return (
            f"目标程序「{label}」以管理员权限运行，本程序没有 → "
            "注入的键鼠会被系统丢弃（右键本程序→以管理员身份运行）"
        )
    return ""


def left_click(x: int, y: int, *, hold_ms: int = 60, restore_cursor: bool = True) -> bool:
    """在全局物理像素 ``(x, y)`` 处模拟一次左键按下→抬起。

    - 先把光标移到目标点（注入坐标点击的前提），点击后**恢复光标原位**
      （借鉴 MAA WithCursorPos 的做法，尽量不打扰用户）。
    - ``hold_ms`` 是按下与抬起之间的间隔（过短有些程序会把 down/up 判成滚屏/拖动）。
    - 返回 True = down/up 都成功；False = 中途失败（光标仍会尽量复位）。

    光标移动走 `move_cursor()`（SendInput 绝对移动优先），并在移动后**复核光标位置**：
    有些环境里 `SetCursorPos` 静默失败（返回 0、错误码 0），只靠返回值会误判成"点过了"。
    """
    x, y = int(x), int(y)
    saved: tuple[int, int] | None = _cursor_pos() if restore_cursor else None

    if not move_cursor(x, y):
        _restore(saved)
        return False
    actual = _cursor_pos()
    if abs(actual[0] - x) > 2 or abs(actual[1] - y) > 2:
        # 光标没到位就**放弃点击**：这时候 down/up 会落在"当前光标处"，
        # 可能误点别的按钮（比"点不中"更糟）。
        _log.warning(
            "光标没到 (%d, %d)，实际停在 %s —— 输入被拦截，已放弃这次点击", x, y, actual
        )
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
    restore = saved
    move_cursor(restore[0], restore[1])


def environment_report() -> str:
    """一句话环境自检：会话号 / 桌面名 / 是否提权 / 虚拟屏尺寸。

    这条能解释掉一大类"注入无效"：进程不在**活动输入桌面**上（远程桌面、另一个会话、
    锁屏/UAC 安全桌面）时，`SetCursorPos` 会静默失败、注入也进不了真实输入队列。
    """
    session = wt.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session))
    console = int(kernel32.WTSGetActiveConsoleSessionId())
    desktop = "?"
    handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    if handle:
        buf = ctypes.create_unicode_buffer(64)
        needed = wt.DWORD()
        if user32.GetUserObjectInformationW(
            handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
        ):
            desktop = buf.value or "?"
    left, top, width, height = _virtual_screen()
    elevated = is_elevated(kernel32.GetCurrentProcessId())
    flag = "是" if elevated else ("否" if elevated is False else "未知")
    import os

    markers = [
        name
        for name in ("SANDBOX_CENTER_IPC_ADDRESS", "WORKBUDDY_FS_PROTECTION_ROLE")
        if os.environ.get(name)
    ]
    sandbox = "、".join(markers) if markers else "无"
    return (
        f"会话={session.value}/活动会话={console if console != 0xFFFFFFFF else '无'} "
        f"桌面={desktop} 提权={flag} 虚拟屏=({left},{top},{width},{height}) "
        f"光标={_cursor_pos()} 沙箱={sandbox}"
    )


def press_space(*, hold_ms: int = 60, use_scan_code: bool = True) -> bool:
    """向前台窗口模拟一次空格按下→抬起。

    **调用方必须先确认目标窗口确实在前台**（见 ``activate_window``）——
    空格落到本程序焦点控件上会触发按钮（如再次点中「录制」立即停录）。

    `use_scan_code=True` 走扫描码（DirectInput / RawInput 的游戏才认，见 `_send_key`）。
    """
    flags = KEYEVENTF_SCANCODE if use_scan_code else 0
    ok = _send_key(VK_SPACE, flags)
    time.sleep(max(0, int(hold_ms)) / 1000.0)
    ok = _send_key(VK_SPACE, flags | KEYEVENTF_KEYUP) and ok
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
    topmost_flag = int(_get_window_long(hwnd, -20) or 0) & 0x8  # GWL_EXSTYLE, WS_EX_TOPMOST
    insert_after = HWND_NOTOPMOST if topmost_flag else HWND_TOPMOST
    user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    ok = bool(user32.SetForegroundWindow(hwnd)) and user32.GetForegroundWindow() == hwnd
    if not ok:
        _log.warning("activate_window(0x%x) 复核失败（前台锁定未放行）", hwnd)
    return ok
