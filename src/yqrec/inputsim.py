"""键鼠注入（前台 SendInput / 后台 PostMessage，借鉴 BetterGI 的双通道，见 01 §15）。

能力：

- **模拟一次鼠标左键点击**（`record_click` 模式 2）；
- **把窗口激活为前台后模拟一次空格键**（`record_click` 模式 1），
  激活流程抄 MAA ``ForegroundUtils.h`` 的三段式（提层 → SetForegroundWindow
  → 复核，失败再 TOPMOST↔NOTOPMOST 强提）；
- **琴键演奏的字母键按下/抬起**（`ForegroundKeys` / `BackgroundKeys`，M5）：
  前台走 SendInput（虚拟键码 + 扫描码双填，见 `_key_input()`），打给**当前激活窗口**
  （激活的是本进程就这一拍不发，但**不中断**演奏，见 `interrupt_when_blocked`）；
  后台走 ``PostMessage(WM_KEYDOWN/UP)`` 到目标窗口，目标没了只能中断。
  两条通道的语义与 BetterGI 的 ``SendInputChannel`` / ``PostMessageChannel`` 对齐。

后续要扩展时保持三条约定：

- 坐标一律用**全局物理像素**（与框选区域/截图后端同口径）；
  进程已声明 Per-Monitor DPI aware，SetCursorPos/SendInput 按物理像素解析。
- 扩展键位要带 extended 位（空格/左键/字母键均非扩展键，暂无此问题）。
- 注入失败只记 warning 返回 False，绝不抛异常打断业务流。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
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
KEYEVENTF_EXTENDEDKEY = 0x0001
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


def _dispatch(inp: _INPUT) -> bool:
    """把一条 `INPUT` 交给 `SendInput`；成功返回 True。

    单独拆出来是为了**可测**：真机注入没法在单测里跑，但"构造出来的 INPUT 长什么样"
    必须能验（`_key_input()` 的字段单测就靠替换这个函数拿到入参）。
    """
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1


def _send_mouse(flags: int, dx: int = 0, dy: int = 0) -> bool:
    """投递一条鼠标事件；失败记 warning 返回 False。"""
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.dwFlags = flags
    if not _dispatch(inp):
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


def _key_input(vk: int, flags: int) -> _INPUT:
    """构造一条键盘 `INPUT`：**虚拟键码与扫描码同时填**（与 BetterGI `InputBuilder` 一致）。

    为什么要双填（2026-10-29 起，取代原来的"只发扫描码"）：

    - 旧写法是 `wVk=0` + `KEYEVENTF_SCANCODE` —— 系统要按**当前键盘布局**把扫描码
      反查成虚拟键。布局不是 US-QWERTY、或输入法处于激活态时，推出的可能是别的字符；
      而且 `GetKeyState(VK_X)` 这类查询看不到按下态（BetterGI 的收尾 `ReleaseAllKey`
      就是靠它判断的）。
    - 双填后两条信息都在：RawInput / DirectInput（原神这类 Unity 游戏）照旧读扫描码，
      普通窗口与 `GetAsyncKeyState` 读 VK，各取所需，也不依赖布局。
    - 扫描码字段**始终**写（`flags` 带 `KEYEVENTF_SCANCODE` 时只是额外把 `wVk` 置 0），
      所以"只认扫描码"的极端环境仍可用 `press_space(use_scan_code=True)` 兜底。

    扩展键（方向键、小键盘等）要靠 `KEYEVENTF_EXTENDEDKEY` 标记，扫描码被截到低 8 位
    时这个标志位是唯一的扩展信息 —— 琴键（A-Z）与空格都不是扩展键，当前用不到。
    """
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki.wScan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
    inp.ki.wVk = 0 if flags & KEYEVENTF_SCANCODE else int(vk) & 0xFFFF
    inp.ki.dwFlags = int(flags)
    return inp


def _send_key(vk: int, flags: int = 0) -> bool:
    """投递一条键盘事件；失败记 warning 返回 False。

    字段填法见 `_key_input()`：默认**VK + 扫描码双填**，`flags` 带 `KEYEVENTF_SCANCODE`
    时退回只发扫描码（老兜底路径）。DirectInput / RawInput 的游戏（原神这类 Unity 游戏）
    常常只认扫描码，而普通窗口只认 VK —— 双填两边都满足。
    """
    if not _dispatch(_key_input(vk, flags)):
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


def self_test(offset: int = 40) -> tuple[bool, str]:
    """把光标挪开 `offset` 像素再挪回来，验证"模拟输入到底能不能进系统输入队列"。

    返回 `(是否生效, 诊断详情)`。这是区分下面两种"没反应"的唯一可靠手段：

    - **指令没进队列**（安全软件/驱动拦截、没有输入桌面）：光标纹丝不动；
    - **进了队列但目标不理会**（打错窗口、目标不认合成输入）：光标正常移动。

    启动时（`record_click != 0`）跑一次，结果只写日志、失败才提示，不动声色。
    """
    before = _cursor_pos()
    left, top, width, height = _virtual_screen()
    target_x = before[0] + offset if before[0] + offset < left + width else before[0] - offset
    target = (target_x, before[1])
    moved_ok = move_cursor(*target)
    time.sleep(0.08)
    actual = _cursor_pos()
    moved = abs(actual[0] - target[0]) <= 2 and abs(actual[1] - target[1]) <= 2
    move_cursor(*before)  # 无论如何都复位
    detail = (
        f"光标 {before} → {actual}（期望 {target}，move_cursor 返回 {moved_ok}）\n"
        f"{environment_report()}"
    )
    return moved, detail


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


def press_space(*, hold_ms: int = 60, use_scan_code: bool = False) -> bool:
    """向前台窗口模拟一次空格按下→抬起。

    **调用方必须先确认目标窗口确实在前台**（见 ``activate_window``）——
    空格落到本程序焦点控件上会触发按钮（如再次点中「录制」立即停录）。

    `use_scan_code=False`（默认）走 `_key_input()` 的 **VK + 扫描码双填**：
    扫描码照旧带上，所以 DirectInput / RawInput 的游戏（原神）能认；同时 VK 也在，
    不再依赖当前键盘布局反查。要退回"只发扫描码"的老路径时传 True。
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


GW_HWNDNEXT = 2
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int


def is_app_window(hwnd: int) -> bool:
    """像不像"用户正在用的那个程序窗口"（排掉本程序、工具窗、托盘隐藏窗、最小化的）。"""
    if not hwnd or is_own_window(hwnd):
        return False
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):  # 最小化的窗口收不到按键，别当目标
        return False
    if user32.GetWindow(hwnd, GW_OWNER):  # 有 owner 的是对话框/工具窗
        return False
    style = int(_get_window_long(hwnd, GWL_EXSTYLE) or 0)
    if style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    return user32.GetWindowTextLengthW(hwnd) > 0


def is_window(hwnd: int) -> bool:
    """句柄是否还是个活着的窗口（记忆目标前复核用，防句柄被复用）。"""
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def key_target_window() -> int:
    """按键演奏要打给谁：**当前前台窗口**，否则 z-order 里它下面第一个正常窗口。

    为什么需要"下面那个"：用户点「演奏」/「播放」时前台是**本程序**（按钮在我们这儿），
    按"前台窗口"取会取到自己。而 z-order 里紧挨在下面的通常就是他刚切走的那个程序
    （游戏），比"必须先用框选区域反选回来"宽松得多 —— 用户明确要求别强制先框选。

    返回值可能是 0（桌面上没有任何候选窗口），调用方据此给提示。
    """
    foreground = int(foreground_window() or 0)
    if is_app_window(foreground):
        return foreground
    hwnd = user32.GetWindow(foreground, GW_HWNDNEXT) if foreground else 0
    seen = 0
    while hwnd and seen < 64:  # 防御性上限：z-order 极端情况下别死循环
        if is_app_window(int(hwnd)):
            return int(hwnd)
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
        seen += 1
    return 0


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


# --------------------------------------------------------------------------- #
# 按键演奏（M5）：字母键 → 前台 SendInput / 后台 PostMessage
# --------------------------------------------------------------------------- #

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_ACTIVATE = 0x0006

user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL


def _letter(key: str) -> str:
    """琴键归一化成单个大写字母（`'a'` / `'A'` → `'A'`）。"""
    text = str(key).strip()
    if len(text) != 1 or not text.isascii() or not text.isalpha():
        raise ValueError(f"不支持的琴键: {key!r}（琴键必须是 A-Z 单个字母）")
    return text.upper()


def virtual_key(key: str) -> int:
    """`'A'` → `0x41`：字母键的虚拟键码恰好等于其大写 ASCII 码。"""
    return ord(_letter(key))


def same_root_window(a: int, b: int) -> bool:
    """两个句柄是否属于同一个**顶层**窗口。

    前台注入前要拿 `GetForegroundWindow()` 与目标比对，但游戏常把键盘焦点交给
    某个子窗口/宿主控件，直接比句柄会误判成"焦点丢了"。统一归一到根窗口再比。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return int(user32.GetAncestor(int(a), GA_ROOT) or a) == int(
        user32.GetAncestor(int(b), GA_ROOT) or b
    )


def _key_lparam(vk: int, key_up: bool) -> int:
    """后台 `WM_KEYDOWN/UP` 的 lParam：重复次数 1 + 扫描码，抬起再置"前一状态/转换"位。

    与 BetterGI `PostMessageSimulator.MakeKeyLParam` 完全同构（字母键都不是扩展键，
    所以不需要第 24 位）。扫描码必须带：很多游戏只读扫描码判断按的是哪个键。
    """
    scan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
    value = 1 | (scan << 16)
    if key_up:
        value |= (1 << 30) | (1 << 31)
    return value


class ForegroundKeys:
    """前台通道：SendInput + 扫描码，按键打进**当前激活窗口**。

    语义（M6d 用户定义）：谁在前台就打给谁，程序**不替你抢焦点**；唯一必须挡住的是
    **打回本程序** —— 那串字母会进谱面文本框，当场改坏谱面。所以 `ready()` 只看
    "当前激活窗口是不是我们自己的进程"，`target_hwnd` 只用于展示/日志。

    挡住时**不中断演奏**，只是这一拍不发键（`interrupt_when_blocked = False`）：
    点「演奏」的瞬间前台必然是本程序，若因此暂停，前台模式永远起不来 ——
    用户切到游戏窗口后按键要能自然接上。
    """

    mode = "foreground"
    interrupt_when_blocked = False
    """发不出键时**不中断**演奏，只静默跳过（见类文档）。"""

    def __init__(self, target_hwnd: int = 0) -> None:
        self.target_hwnd = int(target_hwnd or 0)
        self._pressed: set[str] = set()
        self._pending_up: set[str] = set()
        """焦点不在目标窗口时没送出去的抬起（切回来后补发，见 `_flush_pending_up`）。"""
        self._lock = threading.Lock()

    def describe(self) -> str:
        if self.target_hwnd:
            return f"前台 SendInput（当前激活窗口，目标 {describe(self.target_hwnd)}）"
        return "前台 SendInput（当前激活窗口）"

    def ready(self) -> bool:
        """现在能不能发键：有激活窗口，且**不是本程序**。"""
        foreground = foreground_window()
        return bool(foreground) and not is_own_window(foreground)

    def _flush_pending_up(self) -> None:
        """把"当时没送出去的抬起"补发掉（调用方已确认 `ready()`）。

        焦点离开游戏后 `SendInput` 的 up 只会打到别的窗口，游戏那边就以为键还按着；
        回到游戏时先补这些 up，避免琴键卡死。多出来的 up 对游戏没有任何副作用。
        """
        for letter in list(self._pending_up):
            _send_key(virtual_key(letter), KEYEVENTF_KEYUP)
            self._pressed.discard(letter)
            self._pending_up.discard(letter)

    def key_down(self, key: str) -> bool:
        letter = _letter(key)
        with self._lock:
            if not self.ready():
                return False
            if self._pending_up:
                self._flush_pending_up()
            if letter in self._pressed:  # 同键重复 down 会被游戏当成系统自动重复，丢掉
                return True
            if not _send_key(virtual_key(letter)):
                return False
            self._pressed.add(letter)
            return True

    def key_up(self, key: str) -> bool:
        letter = _letter(key)
        with self._lock:
            if letter not in self._pressed:
                return True
            if not self.ready():
                # 现在发 up 只会打到别的窗口（游戏里那键还按着）→ 记账，切回来补发。
                # 注意**不从 `_pressed` 里删**：那键在游戏里确实还是按下状态。
                self._pending_up.add(letter)
                return False
            ok = _send_key(virtual_key(letter), KEYEVENTF_KEYUP)
            self._pressed.discard(letter)
            return ok

    def release_all(self) -> None:
        with self._lock:
            for letter in list(self._pressed):
                _send_key(virtual_key(letter), KEYEVENTF_KEYUP)
            self._pressed.clear()
            self._pending_up.clear()

    def force_release(self, keys) -> None:
        """对给定键**无条件**补发一次抬起（收尾保险）。

        游戏里漏一个抬起 = 那个琴键按死在按下状态，用户只能切窗口/重开乐器才能恢复。
        所以演奏收尾宁可多发一次无用的 up —— 多余的抬起对游戏没有任何副作用。
        """
        with self._lock:
            for key in keys:
                try:
                    letter = _letter(key)
                except ValueError:
                    continue
                _send_key(virtual_key(letter), KEYEVENTF_KEYUP)
            self._pressed.clear()

    def keys_down(self) -> tuple[str, ...]:
        """当前按住的键（诊断/测试用）。"""
        with self._lock:
            return tuple(sorted(self._pressed))


class BackgroundKeys:
    """后台通道：``PostMessage(WM_KEYDOWN/UP)`` 投递到目标窗口，不抢前台。

    与 BetterGI `PostMessageChannel` 一致：目标不在前台时，每次按 key 前先补一条
    ``WM_ACTIVATE``（否则很多游戏会把后台投递的消息直接丢掉）。
    代价是：只对"读窗口消息"的游戏生效，走 RawInput/DirectInput 的游戏后台收不到 ——
    所以两个通道都留着，让用户实机试。
    """

    mode = "background"
    interrupt_when_blocked = True
    """目标窗口没了就**只能**停（没地方投递），交 UI 停在暂停态（见 `KeyPlayer._run`）。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd or 0)
        self._pressed: set[str] = set()
        self._lock = threading.Lock()

    def describe(self) -> str:
        return f"后台 PostMessage → {describe(self.hwnd)}"

    def activate(self) -> bool:
        if not self.hwnd:
            return False
        return bool(user32.PostMessageW(self.hwnd, WM_ACTIVATE, 1, 0))

    def ready(self) -> bool:
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    def _post(self, letter: str, key_up: bool) -> bool:
        if not self.ready():
            return False
        vk = virtual_key(letter)
        message = WM_KEYUP if key_up else WM_KEYDOWN
        if not user32.PostMessageW(self.hwnd, message, vk, _key_lparam(vk, key_up)):
            _log.warning("PostMessage(0x%x, %s) 失败: winerror=%s", self.hwnd, letter, ctypes.get_last_error())
            return False
        return True

    def key_down(self, key: str) -> bool:
        letter = _letter(key)
        with self._lock:
            if letter in self._pressed:
                return True
            if not self.ready():
                return False
            if not same_root_window(foreground_window(), self.hwnd):
                self.activate()  # 后台收键前先"通知已激活"（BGI 同款）
            if not self._post(letter, False):
                return False
            self._pressed.add(letter)
            return True

    def key_up(self, key: str) -> bool:
        letter = _letter(key)
        with self._lock:
            if letter not in self._pressed:
                return True
            ok = self._post(letter, True)
            self._pressed.discard(letter)
            return ok

    def release_all(self) -> None:
        with self._lock:
            for letter in list(self._pressed):
                self._post(letter, True)
            self._pressed.clear()

    def force_release(self, keys) -> None:
        """对给定键**无条件**补发一次抬起（收尾保险，见 `ForegroundKeys.force_release`）。"""
        with self._lock:
            for key in keys:
                try:
                    letter = _letter(key)
                except ValueError:
                    continue
                self._post(letter, True)
            self._pressed.clear()

    def keys_down(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pressed))


def make_key_sink(mode: str, hwnd: int = 0) -> ForegroundKeys | BackgroundKeys:
    """按 `mode`（`"foreground"` / `"background"`）造一个按键通道；未知值当前台。"""
    if str(mode).lower() == BackgroundKeys.mode:
        return BackgroundKeys(hwnd)
    return ForegroundKeys(hwnd)
