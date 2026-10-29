"""GUI 入口：`yqrec gui` / `python -m yqrec.app`（P4）。

设计约束：
- **PySide6 只在 `run_gui()` 内部导入**，保证 `import yqrec` 在无 Qt 环境仍可用；
- Per-Monitor DPI aware 必须在创建 `QApplication` **之前**声明（03 §9 坑 11）；
- PyInstaller 打包后资源在 `sys._MEIPASS/assets`，通过 `YQREC_ASSETS` 告知 `instruments.py`。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from yqrec.logging import get_logger, setup_logging

_log = get_logger("app")


def enable_dpi_awareness() -> None:
    """声明 Per-Monitor DPI aware（失败则退回系统级 DPI aware）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception as exc:  # Win7 / 被禁用
        _log.debug("SetProcessDpiAwareness 不可用: %s", exc)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:
        _log.debug("SetProcessDPIAware 不可用: %s", exc)


def prepare_assets_path() -> None:
    """打包运行（frozen）时把 `YQREC_ASSETS` 指向 exe 内置资源目录。"""
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidate = base / "assets"
    if candidate.exists():
        os.environ.setdefault("YQREC_ASSETS", str(candidate))
        _log.info("frozen 资源目录: %s", candidate)


def _ensure_std_streams() -> None:
    """窗口化 exe（console=False）里 `sys.stdout/stderr` 是 None，logging 会报错。

    指向 `os.devnull` 后 logging 可正常工作，同时日志仍会写到 `%APPDATA%/YQ_moTools/logs`。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except Exception:
                pass


def _detach_cwd() -> None:
    r"""把进程的"当前工作目录"从 exe 目录挪到临时目录。

    为什么会需要：**搜狗输入法**会把自己的 `debug.log` 写进宿主进程的当前工作目录
    （内容全是 `sgime_skin.vN.filemap.sogouime` / `KernelParameterSetter … scheme=Sogou`
    这类），于是交付目录里凭空多出一个跟本程序毫无关系的 debug.log —— 用户会以为那是
    我们的日志，还会怀疑"我们的日志是不是被它覆盖了"。我们没法让 IME 不写，只能把 cwd 挪走。

    安全性：本程序所有路径都是自己算的绝对路径（数据目录 / `YQREC_ASSETS` / 谱面默认目录 /
    主题缓存），不依赖 cwd，所以挪走没有副作用。
    """
    for name in ("TEMP", "TMP", "TMPDIR"):
        raw = os.environ.get(name)
        if not raw:
            continue
        candidate = Path(os.path.expandvars(raw.strip().strip('"')))
        if not candidate.is_absolute():
            continue
        target = candidate / "yqrec-cwd"
        try:
            target.mkdir(parents=True, exist_ok=True)
            if Path.cwd() == target:
                return
            os.chdir(target)
        except OSError:
            continue
        _log.info("工作目录已挪到 %s（避免输入法把 debug.log 写进程序目录）", target)
        return


def _log_dir() -> Path:
    """日志目录：跟数据目录同名（`config.DATA_DIR_NAME`），改产品名时不跟着改。"""
    # 必须绝对导入：打包 exe 里本模块以 `__main__`（无父包）运行，相对导入
    # `from .config import …` 会 ImportError → 文件日志被 except 静默跳过
    # （2026-10-22 实测：打包版从未建过 %APPDATA%/YQ_moTools/logs）。
    #
    # 2026-10-23：改走 `config.data_dir()` —— 它保证返回**绝对路径**（APPDATA 为空/相对时
    # 依次退到注册表、主目录），不会再出现"日志写进 exe 所在目录"的情况。
    from yqrec.config import data_dir

    return data_dir() / "logs"


def _file_log_path() -> str:
    """当前生效的文件日志路径；没装上文件 handler 时返回空串。"""
    import logging

    for handler in logging.getLogger("yqrec").handlers:
        if isinstance(handler, logging.FileHandler):
            return str(getattr(handler, "baseFilename", "") or "")
    return ""


def _fallback_candidates() -> list[Path]:
    """存档目录写不进去时的候选（按顺序试）：`<exe>\\data`、`%LOCALAPPDATA%\\YQ_moTools`。"""
    from yqrec.config import local_data_dir

    out: list[Path] = [local_data_dir()]
    local = os.environ.get("LOCALAPPDATA")
    if local and Path(local).is_absolute():
        out.append(Path(local) / "YQ_moTools")
    return out


def _has_records(path: Path) -> bool:
    """这个目录里已经有"用户攒下来的东西"（标定档或设置）？"""
    try:
        return (path / "profiles" / "default.json").exists() or (path / "settings.json").exists()
    except OSError:
        return False


def _sticky_data_dir(primary: Path, setting: str) -> tuple[str, str]:
    """主目录**能写**时，别急着切过去：备选目录里已有存档就继续用它。

    为什么需要（2026-10-29 提权后暴露）：数据目录的可写性会随**权限**翻转 ——
    普通权限下 `%APPDATA%` 写不进去（安全软件按未信任程序拦），启动就降级到
    `<exe>\\data`；一旦自提权成功、`%APPDATA%` 又变得可写，主目录就"赢"了，
    用户看到的是**标定档没了、窗口大小和乐器回到默认**（其实数据还在旧目录里）。

    规则很窄：主目录里**还没有**任何存档、而某个备选目录里**已经有**，才粘住旧目录。
    主目录已有存档就照常以它为准（用户真搬过数据的那次不会被拽回来）。
    """
    if _has_records(primary):
        return ("", "")
    for candidate in _fallback_candidates():
        if candidate == primary or not _has_records(candidate):
            continue
        if probe_writable_quiet(candidate):
            os.environ["YQREC_DATA_DIR"] = str(candidate)
            detail = (
                f"配置 archive_path = {setting or '（未配置，默认 Temp）'}\n"
                f"主目录：{primary}（这次能写了，但里面还没有存档）\n"
                f"继续使用：{candidate}（这里有上次的标定档/设置）\n"
                "（不想再用它，就把该目录里的 settings.json 与 profiles/ 删掉后重启）"
            )
            return ("沿用上次的存档目录（悬停看详情）", detail)
    return ("", "")


def probe_writable_quiet(path: Path) -> bool:
    """`config.probe_writable()` 的布尔版（只为"能不能写"这一个判断）。"""
    from yqrec.config import probe_writable

    try:
        return probe_writable(path) is None
    except Exception:
        return False


def _ensure_writable_data_dir() -> tuple[str, str]:
    """开跑前先"真写一次"存档目录；写不进去就换一个能写的。

    返回 `(短句, 详情)`：短句给窗口最下方的消息区（一行以内），详情给悬浮气泡与日志。
    为什么必须实测：ACL 看着完全正常（`Administrator:(F)`）也可能 `open(...,'a')`
    照样 `PermissionError`（2026-10-23 实机）。只查权限会给出"一切正常"的假结论。

    降级顺序：配置的目录 → `Local`（`<exe>\\data`）→ `%LOCALAPPDATA%\\YQ_moTools`。
    即 "Temp 写不进去自动降级为 Local" 就是这里实现的（候选表第一项就是 Local）。
    主目录这次**能写**但里面是空的、而备选目录已有存档时，继续用备选（见 `_sticky_data_dir`）。
    """
    from yqrec.config import archive_setting, data_dir, probe_writable

    try:
        primary = data_dir()
    except Exception as exc:
        return ("存档目录解析失败，本次不保存设置", f"data_dir() 抛错：{type(exc).__name__}: {exc}")
    error = probe_writable(primary)
    setting = archive_setting()
    if error is None:
        return _sticky_data_dir(primary, setting)
    for candidate in _fallback_candidates():
        if candidate == primary:
            continue
        if probe_writable(candidate) is None:
            os.environ["YQREC_DATA_DIR"] = str(candidate)
            detail = (
                f"配置 archive_path = {setting or '（未配置，默认 Temp）'}\n"
                f"目标目录：{primary}\n"
                f"写入失败：{error}\n"
                f"本次改用：{candidate}\n"
                "（设置/标定/日志都存这里；想回到 C 盘，请把本程序加入安全软件白名单，"
                "或把 archive_path 改成 Local 固定用程序目录）"
            )
            return (f"存档写不进 C 盘，已改用 {candidate.name} 目录（悬停看详情）", detail)
    detail = (
        f"配置 archive_path = {setting or '（未配置，默认 Temp）'}\n"
        f"目标目录：{primary}\n"
        f"写入失败：{error}\n"
        "备选目录也写不进去 → 本次的设置/标定/日志都无法保存。\n"
        "请检查安全软件（360 等）拦截，或把程序放到有写权限的目录。"
    )
    return ("存档目录不可写，本次不保存设置（悬停看详情）", detail)


def _elevate_early(argv: list[str] | None) -> tuple[str, str]:
    """启动第一步：需要的话把本进程换成**管理员实例**（见 `yqrec.elevate`）。

    为什么不提权就没法给游戏发键：原神 PC 端以管理员权限运行（从启动器继承），
    UIPI 会把普通权限进程注入的键鼠**静默丢弃** —— `SendInput` 返回成功但游戏没反应。
    BetterGI 因此每次启动都 `Verb="runas"` 重启自己，这里抄同款做法。

    拒绝 UAC / 提权失败都不阻断启动：录制识谱、试听、导出都不需要管理员。
    """
    from yqrec.elevate import ensure_admin

    try:
        return ensure_admin(argv)
    except Exception as exc:  # 提权只是尽力而为，绝不能反过来挡住整个程序
        _log.exception("自提权异常（继续以普通权限运行）")
        return ("failed", f"自提权异常：{type(exc).__name__}: {exc}")


def run_gui(argv: list[str] | None = None) -> int:
    """启动主窗，返回 Qt 事件循环退出码。"""
    enable_dpi_awareness()
    prepare_assets_path()
    _ensure_std_streams()
    from yqrec.elevate import Elevation, status_text

    admin_status, admin_note = _elevate_early(argv)
    if admin_status == Elevation.RELAUNCHED:
        # 高权限实例已经起来了，本进程什么都不做直接退出（否则会出现两个窗口）
        return 0
    _detach_cwd()
    data_note, data_detail = _ensure_writable_data_dir()
    if not data_note and admin_status in (Elevation.CANCELLED, Elevation.FAILED):
        # 没有管理员权限时最要紧的不是写盘，而是"往游戏里注入的键会被系统丢弃"
        data_note = "未获得管理员权限：向以管理员运行的游戏注入按键会失效"
        data_detail = admin_note or "自提权未成功，请右键程序 → 以管理员身份运行"
    log_dir_text = ""
    log_failed = False
    try:
        log_dir = _log_dir()
        log_dir_text = str(log_dir)
        setup_logging(log_dir)
        log_failed = not _file_log_path()
    except Exception:  # 目录不可写时退回仅控制台
        import traceback

        log_failed = True
        log_dir_text = log_dir_text or "（日志目录解析失败）"
        try:  # 文件日志装不上时把异常留档到 %TEMP%（不留档就完全不可见）
            (Path(os.environ.get("TEMP", ".")) / "yqrec_log_fail.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        setup_logging()

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow, instrument_icon

    _log.info("权限状态: %s", status_text(admin_status, admin_note))
    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("YQ_moTools")
    app.setApplicationDisplayName("YQ_moTools")
    app.setOrganizationName("YQ_moTools")
    icon = instrument_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    window = MainWindow()
    window.show()
    _report_paths(
        window,
        log_failed=log_failed,
        log_dir_text=log_dir_text,
        note=data_note,
        detail=data_detail,
        admin=status_text(admin_status, admin_note),
    )
    _run_injection_selftest(window)
    _log.info("GUI 启动完成")
    return int(app.exec())


def _run_injection_selftest(window: object) -> None:
    """`record_click != 0` 时做一次"模拟键鼠能不能进系统输入队列"的自检。

    只在这个开关打开时跑（用户打算注入时），动作是"光标挪 40px 再挪回"，几乎无感。
    结论进日志；失败时界面出一句 warn（气泡里有会话/桌面/提权/沙箱，方便甩给排查）。
    """
    try:
        from yqrec.instruments import load_record_click

        if load_record_click() == 0:
            return
        from yqrec.inputsim import self_test

        ok, detail = self_test()
    except Exception:
        _log.exception("输入自检异常（跳过）")
        return
    report = getattr(window, "show_injection_selftest", None)
    if callable(report):
        report(ok=ok, detail=detail)


def _report_paths(
    window: object,
    *,
    log_failed: bool,
    log_dir_text: str = "",
    note: str = "",
    detail: str = "",
    admin: str = "",
) -> None:
    """启动自检：把路径写进日志；写不了就在窗口最下方的消息区说清楚（不弹窗）。

    "东西写不进 C 盘存档"这类问题在旧版里是完全无声的：日志 handler 装不上、
    settings.json 写失败都只留一行 warning，而那行 warning 本身就写不进磁盘。
    结果用户只能看到"设置不生效 / 乐器变回旧的"。
    """
    from yqrec.config import archive_setting, config_path, data_dir, profile_path

    actual_log = _file_log_path()
    log_text = actual_log or log_dir_text or "-"
    if not actual_log and log_dir_text:
        log_text += "（未能安装文件 handler）"
    try:  # 「录制前激活窗口/点击」读的就是这份 JSON，把实际生效的模式一起写进日志
        from yqrec.instruments import instruments_path, load_record_click

        click_text = f"{instruments_path()}（record_click={load_record_click()}）"
    except Exception as exc:  # 配置坏了也要能看见
        click_text = f"读取失败: {exc}"
    try:  # 注入相关的环境（会话/桌面/提权/虚拟屏）：排查"注入了没反应"的第一手信息
        from yqrec.inputsim import environment_report

        env_text = environment_report()
    except Exception as exc:
        env_text = f"读取失败: {exc}"
    rows: list[tuple[str, str]] = [
        ("存档配置", archive_setting() or "（未配置，默认 Temp）"),
        ("数据目录", str(data_dir())),
        ("设置", str(config_path())),
        ("标定档", str(profile_path("default"))),
        ("日志", log_text),
        ("乐器配置", click_text),
        ("工作目录", str(Path.cwd())),
        ("输入环境", env_text),
        ("权限", admin or "-"),
    ]
    for label, path in rows:
        _log.info("路径[%s] = %s", label, path)
    if note:
        _log.warning("存档目录异常：%s ｜ %s", note, detail.replace("\n", " "))
    report = getattr(window, "show_startup_diagnostics", None)
    if callable(report):
        report(rows=rows, log_failed=log_failed, note=note, detail=detail)



if __name__ == "__main__":  # pragma: no cover - GUI 入口
    raise SystemExit(run_gui())
