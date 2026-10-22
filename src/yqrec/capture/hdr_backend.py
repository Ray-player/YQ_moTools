"""j-hdrcapture 后端（HDR 屏首选，见 01 §14 技术总结）。

为什么需要它：HDR（或 Win11 24H2 的 ACM）开启后，DWM 把桌面合成为线性 scRGB 的
FP16 表面。`IDXGIOutput1::DuplicateOutput`（dxcam 走的）和 GDI BitBlt 都会被系统
**截断**成 8bit，高光糊成一片白 → 我们"键帽压暗差值"的判据直接失效（不是黑屏）。

`hdrcapture` 用 `IDXGIOutput5::DuplicateOutput1` 拿 FP16，再按**每块屏的 SDR 白电平**
在 GPU 上归一：

- SDR 内容逐像素不变（实测与 GDI 完全一致，所以换它不会动到现有基线）；
- 高于白点的高光按"最亮通道"整体缩放，保住色相（不是逐通道 clip）。

三个必须知道的点：
1. **DXGI 同一输出只允许一个 duplication 会话** —— 本后端与 dxgi(dxcam) 不能同时打开。
   管理器切换时会先 `close()` 旧的，不要并行 new 两个。
2. **区域必须完整落在单块显示器内**。跨屏时库会退化去合成整个虚拟桌面（慢、尺寸大），
   这里直接拒绝并让管理器回退到 dxgi。
3. **首帧要等真实 present**，桌面静止/显示器休眠时可能等不到 → 返回 None（计丢帧），
   不当成错误，避免误触发管理器降级。
"""

from __future__ import annotations

import time

import numpy as np

from ..logging import get_logger
from ..types import CaptureTarget, Rect, ScreenTarget
from .base import BaseBackend

_log = get_logger("capture.hdr")

# 每次 grab 等 present 的预算：60fps 下一帧 16.7ms，够用又不至于在静止画面卡太久。
_TIMEOUT_MS = 16


def pick_monitor(monitors, rect: Rect):  # noqa: ANN001, ANN201 - 传库对象，便于测试替身
    """挑出完整包含 `rect` 的物理显示器。

    `hdrcapture` 的 `monitors[0]` 是**虚拟桌面**（`is_virtual_desktop=True`），不能用来
    `grab` 单个区域，必须跳过。

    Raises:
        ValueError: 没有单块显示器能完整包含该区域（跨屏 / 在已断开的屏上）。
    """
    for mon in monitors:
        if getattr(mon, "is_virtual_desktop", False):
            continue
        x, y, w, h = mon.rect
        if (
            x <= rect.left
            and y <= rect.top
            and rect.right <= x + w
            and rect.bottom <= y + h
        ):
            return mon
    raise ValueError(
        f"区域 {rect.left},{rect.top},{rect.width}x{rect.height} 未完整落在单块显示器内"
        f"（跨屏时 j-HDR 会退化成合成整个虚拟桌面，这里直接拒绝）"
    )


class HdrBackend(BaseBackend):
    name = "hdr"

    def __init__(self) -> None:
        super().__init__()
        self._cap = None
        self._region: tuple[int, int, int, int] | None = None
        self._monitor: dict[str, object] = {}
        self._has_frame = False

    @classmethod
    def is_available(cls) -> bool:
        try:
            import hdrcapture  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, ScreenTarget)

    @property
    def hdr_enabled(self) -> bool:
        """当前区域所在显示器是否处于 HDR（UI/日志用；未打开时为 False）。"""
        return bool(self._monitor.get("hdr_enabled", False))

    @property
    def monitor_info(self) -> dict[str, object]:
        return dict(self._monitor)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:
        import hdrcapture

        assert isinstance(target, ScreenTarget)
        del target_fps  # 由桌面 present 驱动，没有可用的 pacing 参数

        r = target.rect
        cap = hdrcapture.Capture(timeout_ms=_TIMEOUT_MS)
        try:
            mon = pick_monitor(cap.monitors, r)
        except Exception:
            cap.close()
            raise

        self._cap = cap
        self._region = (r.left, r.top, r.width, r.height)
        self._monitor = {
            "index": mon.index,
            "name": getattr(mon, "friendly_name", "") or mon.device_name,
            "rect": tuple(mon.rect),
            "hdr_enabled": bool(mon.hdr_enabled),
            "hdr_supported": bool(mon.hdr_supported),
        }
        self._has_frame = False
        _log.info(
            "j-HDR 显示器: %s %s HDR=%s（支持=%s）→ 区域 %s",
            self._monitor["index"],
            self._monitor["name"],
            self._monitor["hdr_enabled"],
            self._monitor["hdr_supported"],
            self._region,
        )
        if not self._monitor["hdr_enabled"]:
            _log.info("j-HDR：当前显示器未开 HDR，走 SDR 直通（与 GDI 逐像素一致）")

    def _grab(self) -> tuple[np.ndarray, float] | None:
        import hdrcapture

        assert self._cap is not None and self._region is not None
        left, top, width, height = self._region
        try:
            frame = self._cap.grab_region(left, top, width, height)
        except hdrcapture.InitialFrameTimeout:
            # 还没有过真实 present（静止桌面 / 显示器休眠）：算丢帧，不算错误，
            # 否则连续 20 次就会把管理器逼到降级。
            return None
        except (hdrcapture.AccessLost, hdrcapture.DimensionsChanged) as exc:
            # 开关 HDR、改分辨率、拔插显示器都会走到这里：抛出去让管理器回退/重建。
            raise RuntimeError(f"j-HDR 会话失效（{type(exc).__name__}）: {exc}") from exc

        self._has_frame = True
        # `.bgr` 已是紧凑 BGR；copy 出来保证可写、且脱离库内部缓冲。
        arr = np.frombuffer(frame.bgr, dtype=np.uint8).reshape(frame.height, frame.width, 3).copy()
        return arr, time.perf_counter()

    def _close(self) -> None:
        cap, self._cap = self._cap, None
        self._region = None
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass
