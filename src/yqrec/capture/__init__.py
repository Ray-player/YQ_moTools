"""捕获子包：多后端 + 管理器 + 调试环形缓冲。"""

from .base import BaseBackend, CaptureBackend
from .manager import CaptureManager, ProbeResult

__all__ = ["BaseBackend", "CaptureBackend", "CaptureManager", "ProbeResult"]
