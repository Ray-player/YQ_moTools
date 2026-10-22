"""文件后端：读 mp4 / PNG 序列，用于回归测试（无需真机；03 §4.1）。

时间戳 = 帧序号 / fps（确定性，便于对比期望结果）。
注意：mp4 的 4:2:0 色度失真会污染颜色判据，**只用于流程验证，不用于标定阈值**。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..errors import CaptureError
from ..types import CaptureTarget, ScreenTarget
from .base import BaseBackend


class FileBackend(BaseBackend):
    name = "file"

    def __init__(self, path: str | Path, fps: float = 60.0) -> None:
        super().__init__()
        self._path = Path(path)
        self._fps = fps
        self._cap = None
        self._images: list[Path] = []
        self._idx = 0

    @classmethod
    def is_available(cls) -> bool:
        try:
            import cv2  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, ScreenTarget)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:  # noqa: ARG002
        if not self._path.exists():
            raise CaptureError(f"文件不存在: {self._path}")
        self._idx = 0
        if self._path.is_dir():
            self._images = sorted(
                p for p in self._path.iterdir() if p.suffix.lower() in {".png", ".jpg", ".bmp"}
            )
            if not self._images:
                raise CaptureError(f"目录中没有图片: {self._path}")
            return
        import cv2

        cap = cv2.VideoCapture(str(self._path))
        if not cap.isOpened():
            raise CaptureError(f"无法打开视频: {self._path}")
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 1:
            self._fps = float(fps)
        self._cap = cap

    def _grab(self) -> tuple[np.ndarray, float] | None:
        if self._images:
            if self._idx >= len(self._images):
                return None
            import cv2

            img = cv2.imread(str(self._images[self._idx]))
            ts = self._idx / self._fps
            self._idx += 1
            return img, ts
        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        ts = self._idx / self._fps
        self._idx += 1
        return frame, ts

    def _close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._images = []
