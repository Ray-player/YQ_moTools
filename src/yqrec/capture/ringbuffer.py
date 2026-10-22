"""调试用帧环形缓冲（L2，默认关闭；03 §3.4）。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FrameRecord:
    ts: float
    frame: np.ndarray


class FrameRingBuffer:
    """保存最近 N 帧（引用不复制，调用方保证不原地修改帧）。

    内存参考：1600x300x3 ≈ 1.4MB/帧；N=120 时约 170MB，故默认仅调试开启。
    """

    def __init__(self, maxlen: int = 120) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen 必须为正")
        self._buf: deque[FrameRecord] = deque(maxlen=maxlen)

    def push(self, ts: float, frame: np.ndarray) -> None:
        self._buf.append(FrameRecord(ts=ts, frame=frame))

    def latest(self) -> FrameRecord | None:
        return self._buf[-1] if self._buf else None

    def snapshot(self) -> list[FrameRecord]:
        return list(self._buf)

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def maxlen(self) -> int:
        assert self._buf.maxlen is not None
        return self._buf.maxlen
