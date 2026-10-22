"""日志初始化：控制台 + 按天滚动文件（保留 7 天）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured = False


def setup_logging(log_dir: Path | str | None = None, *, level: int = logging.INFO) -> logging.Logger:
    """配置根 logger，可重复调用（幂等）。"""
    global _configured
    root = logging.getLogger("yqrec")
    if _configured:
        return root

    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            d / "yqrec.log", when="midnight", backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(file_handler)

    # 第三方库：dxcam 停止时会把实测采集帧率写进日志（"Screen Capture FPS: x"），
    # 它是判断"源只有 30 帧"最硬的证据，必须落进同一份文件。
    for name in ("dxcam", "dxcam.dxcam", "dxcam.core"):
        third = logging.getLogger(name)
        third.setLevel(level)
        third.propagate = False
        for handler in root.handlers:
            if handler not in third.handlers:
                third.addHandler(handler)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    """`get_logger("capture")` → `yqrec.capture`。"""
    return logging.getLogger(f"yqrec.{name}")
