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


def _log_dir() -> Path:
    """日志目录：跟数据目录同名（`config.DATA_DIR_NAME`），改产品名时不跟着改。"""
    # 必须绝对导入：打包 exe 里本模块以 `__main__`（无父包）运行，相对导入
    # `from .config import …` 会 ImportError → 文件日志被 except 静默跳过
    # （2026-10-22 实测：打包版从未建过 %APPDATA%/YQ_moTools/logs）。
    from yqrec.config import DATA_DIR_NAME

    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / DATA_DIR_NAME / "logs"


def run_gui(argv: list[str] | None = None) -> int:
    """启动主窗，返回 Qt 事件循环退出码。"""
    enable_dpi_awareness()
    prepare_assets_path()
    _ensure_std_streams()
    try:
        setup_logging(_log_dir())
    except Exception:  # 目录不可写时退回仅控制台
        import traceback

        try:  # 文件日志装不上时把异常留档到 %TEMP%（不留档就完全不可见）
            (Path(os.environ.get("TEMP", ".")) / "yqrec_log_fail.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        setup_logging()

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow, instrument_icon

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("YQ_moTools")
    app.setApplicationDisplayName("YQ_moTools")
    app.setOrganizationName("YQ_moTools")
    icon = instrument_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    window = MainWindow()
    window.show()
    _log.info("GUI 启动完成")
    return int(app.exec())


if __name__ == "__main__":  # pragma: no cover - GUI 入口
    raise SystemExit(run_gui())
