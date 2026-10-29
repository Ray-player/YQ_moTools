"""启动自提权：非管理员就 ``runas`` 重启自己，然后让原进程退出（抄 BetterGI）。

为什么必须这样 —— 这是"注入有日志、游戏没反应"的根因：

Windows 的 **UIPI**（User Interface Privilege Isolation）只允许*同级或更高级别*的进程
往一个窗口投递键鼠消息。原神 PC 端是从米哈游启动器拉起来的子进程（启动器要装反作弊
驱动/做更新，本身以管理员运行），于是游戏**继承了高完整性令牌**；普通权限的本程序
``SendInput`` / ``PostMessage`` 全被系统**静默丢弃** —— 返回值是成功，``GetLastError()``
是 0，游戏纹丝不动。BetterGI 的 README 把这件事写得很直白：

    为什么需要管理员权限？因为游戏是以管理员权限启动的，
    软件不以管理员权限启动的话没有权限模拟鼠标点击。

它的做法（``RuntimeHelper.EnsureElevated`` → ``Process.Start(Verb="runas")``）就是本模块
抄的东西：不自欺地"检测到权限不足然后提示用户"，而是**直接把自己换成高权限实例**。

三个必须注意的边界：

- **只对打包版自动重启**。源码运行时 ``sys.executable`` 是 python.exe，重启出来的是
  "裸解释器 + 丢掉的 ``-m yqrec``"，只会添乱 —— 开发时自己开一个管理员终端跑。
- **绝不循环重启**。``ShellExecuteW`` 在"系统关掉 UAC 但账号不是管理员"这类环境下
  可能返回成功而子进程仍是普通权限，所以重启前先给环境变量打标记，子进程看到标记
  就不再尝试（``YQREC_ELEVATE_TRIED``）。
- **用户拒绝是合法结果**。取消 UAC（错误码 1223）不报错、不退出，继续以普通权限运行
  （录制识谱并不需要管理员，只有向游戏注入才需要），由界面的消息区提示后果。

这一步发生在日志系统装好之前，所以结论会额外落一行到 `%TEMP%\\yqrec_elevate.log`
（见 `_breadcrumb()`）—— 否则"双击没反应"会完全无从查起。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
from pathlib import Path

from .logging import get_logger

_log = get_logger("elevate")

SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223
"""UAC 被取消的错误码（多数机器上 `ShellExecuteW` 的**返回码**是 5，1223 在 last error 里）。"""

SE_ERR_ACCESSDENIED = 5
"""`ShellExecuteW` 返回码：拒绝访问（UAC 点了"否"就是这个）。"""

ENV_NO_ELEVATE = "YQREC_NO_ELEVATE"
"""置 1 表示本次不要自提权（调试/自动化测试用）。"""

ENV_ELEVATE_TRIED = "YQREC_ELEVATE_TRIED"
"""置 1 表示本进程是"提权尝试后的子进程"，绝不再试（防无限重启）。"""


def _breadcrumb(text: str) -> None:
    """把提权这一步落一行到 `%TEMP%\\yqrec_elevate.log`（尽力而为，失败就算了）。

    为什么要它：自提权发生在**日志系统装好之前**（日志目录要等提权后再探测），
    而这里恰好是最容易"静默失败"的一步 —— 父进程拉起子进程后自己就退出了，
    一旦子进程起不来，用户看到的就是"双击没反应"。留一行面包屑，事后能查。
    """
    try:
        import time
        from pathlib import Path as _Path

        target = _Path(os.environ.get("TEMP", ".")) / "yqrec_elevate.log"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {text}\n")
    except Exception:  # 面包屑不能反过来影响启动
        pass


class Elevation:
    """`ensure_admin()` 的三种结论（用字符串常量是为了日志/测试里好读）。"""

    ADMIN = "admin"
    """本来就在管理员权限下，无需动作。"""

    RELAUNCHED = "relaunched"
    """已拉起高权限实例，**当前进程应当立刻退出**。"""

    CANCELLED = "cancelled"
    """用户拒绝了 UAC。"""

    FAILED = "failed"
    """拉起失败（返回码异常/异常/已尝试过），继续以普通权限运行。"""

    SKIPPED = "skipped"
    """主动跳过（源码运行 / 环境变量禁止 / 读不到自己的令牌）。"""


def is_admin() -> bool | None:
    """当前进程是否提权；读不到令牌返回 None（保守：不当成管理员）。"""
    from .inputsim import is_elevated

    try:
        return is_elevated(ctypes.windll.kernel32.GetCurrentProcessId())
    except Exception as exc:  # pragma: no cover - 极端环境
        _log.debug("读取自身提权状态失败: %s", exc)
        return None


def is_frozen() -> bool:
    """是否打包运行（PyInstaller）。"""
    return bool(getattr(sys, "frozen", False))


def relaunch_args(argv: list[str] | None = None) -> list[str]:
    """高权限实例要带的命令行参数（默认沿用当前进程的）。"""
    if argv is not None:
        return [str(item) for item in argv]
    return [str(item) for item in sys.argv[1:]]


def shell_execute_runas(exe: str, params: str, workdir: str) -> int:
    """``ShellExecuteW(None, "runas", exe, params, workdir, SW_SHOWNORMAL)``；返回码 >32 才算成功。

    单独拆成函数是为了让单测能替换它（真跑会弹 UAC，测试里绝不能真弹）。
    """
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wt.HWND,
        wt.LPCWSTR,
        wt.LPCWSTR,
        wt.LPCWSTR,
        wt.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    value = shell32.ShellExecuteW(None, "runas", exe, params, workdir, SW_SHOWNORMAL)
    rc = int(value or 0)
    if rc <= 32:  # 失败时返回 SE_ERR_* 码，真正的原因在 last error 里
        _log.debug("ShellExecuteW(runas) rc=%s last_error=%s", rc, ctypes.get_last_error())
    return rc


def ensure_admin(argv: list[str] | None = None) -> tuple[str, str]:
    """必要时把自己重启成管理员；返回 `(结论, 说明)`，说明为空串表示无需提示。

    调用方拿到 ``Elevation.RELAUNCHED`` 后**必须立刻退出**（高权限实例已经在跑），
    其余结论都可以继续执行。
    """
    if os.environ.get(ENV_NO_ELEVATE):
        return (Elevation.SKIPPED, f"{ENV_NO_ELEVATE} 已设置，跳过自提权")

    admin = is_admin()
    if admin is True:
        return (Elevation.ADMIN, "")
    if admin is None:
        return (Elevation.SKIPPED, "读不到本进程的权限令牌，未尝试自提权")
    if os.environ.get(ENV_ELEVATE_TRIED):
        # 上一次已经提过权却仍是普通权限 → 再弹一次 UAC 只会陷入死循环
        return (Elevation.FAILED, "已尝试过自提权，但当前仍是普通权限")
    if not is_frozen():
        return (
            Elevation.SKIPPED,
            "源码运行不自动提权（请在管理员终端里运行，或用打包版）",
        )

    exe = str(Path(sys.executable))
    workdir = str(Path(exe).parent)
    params = subprocess.list2cmdline(relaunch_args(argv))
    os.environ[ENV_ELEVATE_TRIED] = "1"  # 子进程继承环境：它不会再来一轮
    _log.info("以管理员权限重启：%s %s", exe, params)
    _breadcrumb(f"relaunch-as-admin: {exe} {params}")
    try:
        rc = shell_execute_runas(exe, params, workdir)
    except Exception as exc:
        _log.warning("自提权失败（继续以普通权限运行）: %s", exc)
        _breadcrumb(f"relaunch-exception: {type(exc).__name__}: {exc}")
        return (Elevation.FAILED, f"以管理员权限重启失败：{type(exc).__name__}: {exc}")
    if rc in (ERROR_CANCELLED, SE_ERR_ACCESSDENIED):
        # 必须排在 rc > 32 之前：1223 本身大于 32，否则会被当成"成功"
        _log.info("未获得管理员权限（UAC 被取消/拒绝，返回码 %s），继续以普通权限运行", rc)
        _breadcrumb(f"relaunch-denied: rc={rc}")
        return (Elevation.CANCELLED, "用户取消了管理员权限请求")
    if rc > 32:
        _breadcrumb(f"relaunch-ok: rc={rc}")
        return (Elevation.RELAUNCHED, "已以管理员权限重新启动")
    _log.warning("ShellExecuteW(runas) 返回 %s，提权未成功", rc)
    _breadcrumb(f"relaunch-failed: rc={rc}")
    return (Elevation.FAILED, f"以管理员权限重启失败（ShellExecuteW 返回 {rc}）")


def status_text(status: str, note: str = "") -> str:
    """给日志/启动诊断用的一句话。"""
    labels = {
        Elevation.ADMIN: "管理员",
        Elevation.RELAUNCHED: "已提权重启（本进程应退出）",
        Elevation.CANCELLED: "普通用户（用户拒绝了 UAC）",
        Elevation.FAILED: "普通用户（提权失败）",
        Elevation.SKIPPED: "普通用户（未尝试提权）",
    }
    label = labels.get(status, status)
    return f"{label} —— {note}" if note else label
