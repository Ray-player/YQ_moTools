"""领域错误与错误码（见 03 §6）。"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    CAPTURE_OPEN = "E_CAPTURE_OPEN"
    CAPTURE_BLACK = "E_CAPTURE_BLACK"
    CAPTURE_TIMEOUT = "E_CAPTURE_TIMEOUT"
    KEYGRID_MISMATCH = "E_KEYGRID_MISMATCH"
    CALIB_LOW_SNR = "E_CALIB_LOW_SNR"
    PARSE = "E_PARSE"
    EXPORT = "E_EXPORT"
    CONFIG = "E_CONFIG"
    NOT_IMPLEMENTED = "E_NOT_IMPLEMENTED"


class YQRecError(Exception):
    """所有可预期错误的基类；UI 层据此显示可读提示。"""

    def __init__(self, code: ErrorCode, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" + (f" ({self.detail})" if self.detail else "")


class CaptureError(YQRecError):
    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.CAPTURE_OPEN, detail: str | None = None) -> None:
        super().__init__(code, message, detail=detail)


class ParseError(YQRecError):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(ErrorCode.PARSE, message, detail=detail)


class KeyGridMismatch(YQRecError, ValueError):
    """检测到的圆数与乐器键数不符；上层应转手动点选。"""

    def __init__(self, message: str) -> None:
        YQRecError.__init__(self, ErrorCode.KEYGRID_MISMATCH, message)


class NotImplementedYet(YQRecError):
    """M0 骨架期的显式占位：调用即报，避免静默错误。"""

    def __init__(self, what: str, *, phase: str = "P1") -> None:
        super().__init__(ErrorCode.NOT_IMPLEMENTED, f"{what} 尚未实现", detail=f"计划阶段: {phase}")
