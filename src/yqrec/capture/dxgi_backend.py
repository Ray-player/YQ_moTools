"""DXGI Desktop Duplication 后端（屏幕区域首选，03 §4.1）。

**多显示器要点（M2q.6）**：dxcam 的 `create()` 默认只绑定**主输出**，且 `region` 必须是
**该输出的局部坐标**（`_validate_region` 要求 `0 <= left < right <= width`）。
我们框的是虚拟桌面全局坐标，所以区域落在副屏时（其全局 x 是负的）会直接
`ValueError: Invalid Region` → 打开失败 → 管理器静默回退到 GDI，
用户看到的就是"后端选了 dxgi、状态栏却是 gdi"。
本模块负责：按区域中心找到所属输出 → 换算成局部坐标 → 用那个 output 建相机。
"""

from __future__ import annotations

import numpy as np

from ..logging import get_logger
from ..types import CaptureTarget, Rect, ScreenTarget
from .base import BaseBackend

_log = get_logger("capture.dxgi")

_MAX_DEVICES = 4
_MAX_OUTPUTS = 8


def local_region(
    rect: Rect, origin: tuple[int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """全局矩形 → 某输出的局部 `(left, top, right, bottom)`，并夹进该输出边界。

    Args:
        rect: 全局虚拟桌面坐标下的目标区域。
        origin: 该输出左上角的全局坐标（DXGI `DesktopCoordinates.left/top`）。
        size: 该输出的分辨率（旋转后）`(width, height)`。

    Returns:
        `(left, top, right, bottom)`，满足 `0 <= left < right <= width`、`0 <= top < bottom <= height`
        —— 正是 dxcam `_validate_region` 要求的形状。

    Raises:
        ValueError: 区域与输出完全不相交。
    """
    ox, oy = int(origin[0]), int(origin[1])
    width, height = int(size[0]), int(size[1])
    left = max(0, rect.left - ox)
    top = max(0, rect.top - oy)
    right = min(width, rect.right - ox)
    bottom = min(height, rect.bottom - oy)
    if right <= left or bottom <= top:
        raise ValueError(
            f"区域 {rect.left},{rect.top},{rect.width}x{rect.height} 与该输出不相交"
            f"（原点 {ox},{oy}，尺寸 {width}x{height}）"
        )
    return (left, top, right, bottom)


def find_output(dxcam, cx: int, cy: int):  # noqa: ANN001, ANN201 - 传模块本身便于测试替身
    """找到包含全局点 `(cx, cy)` 的 DXGI 输出。

    Returns:
        `(device_idx, output_idx, origin, size)`；`(0, None, (0,0), (0,0))` 表示拿不到输出
        几何（旧版 dxcam）→ 调用方退回"主输出 + 全局坐标"的老路子。

    Raises:
        RuntimeError: 没有任何输出包含该点（区域在已断开的显示器上）。
    """
    for device_idx in range(_MAX_DEVICES):
        exhausted = True
        for output_idx in range(_MAX_OUTPUTS):
            try:
                cam = dxcam.create(
                    device_idx=device_idx,
                    output_idx=output_idx,
                    output_color="BGR",
                    backend="dxgi",
                )
            except IndexError:  # 该设备没有更多输出
                break
            except Exception:  # 该输出暂时开不了（拔屏/独占），试下一个
                continue
            exhausted = False
            coords = getattr(
                getattr(getattr(cam, "_output", None), "desc", None),
                "DesktopCoordinates",
                None,
            )
            if coords is None:  # 拿不到几何 → 老路子
                return (0, None, (0, 0), (0, 0))
            if coords.left <= cx < coords.right and coords.top <= cy < coords.bottom:
                return (
                    device_idx,
                    output_idx,
                    (int(coords.left), int(coords.top)),
                    (int(cam.width), int(cam.height)),
                )
        if exhausted:  # 这个 device_idx 根本不存在 → 不用再往后试
            break
    raise RuntimeError(f"没有 DXGI 输出包含区域中心 ({cx}, {cy})；区域可能在已断开的显示器上")


class DxgiBackend(BaseBackend):
    name = "dxgi"

    def __init__(self) -> None:
        super().__init__()
        self._cam = None

    @classmethod
    def is_available(cls) -> bool:
        try:
            import dxcam  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, target: CaptureTarget) -> bool:
        return isinstance(target, ScreenTarget)

    def _open(self, target: CaptureTarget, target_fps: int) -> None:
        import dxcam

        assert isinstance(target, ScreenTarget)
        r = target.rect
        cx, cy = r.left + r.width // 2, r.top + r.height // 2
        device_idx, output_idx, origin, size = find_output(dxcam, cx, cy)

        if output_idx is None:  # 旧版 dxcam：沿用"主输出 + 全局坐标"
            _log.warning("dxcam 未提供输出几何，按主输出 + 全局坐标打开（多屏可能失败）")
            self._cam = dxcam.create(output_color="BGR", backend="dxgi")
            region = (r.left, r.top, r.right, r.bottom)
        else:
            region = local_region(r, origin, size)
            _log.info(
                "DXGI 输出: device=%d output=%d 原点=%s 尺寸=%s → 局部区域=%s",
                device_idx,
                output_idx,
                origin,
                size,
                region,
            )
            self._cam = dxcam.create(
                device_idx=device_idx,
                output_idx=output_idx,
                output_color="BGR",
                backend="dxgi",
            )
        # `target_fps=0` = 关掉 dxcam 的高精度周期定时器，直接跟着桌面的 present 走。
        # 为什么不用 60：DXGI 复制本身就被"桌面每秒 present 几次"限速，定时器再叠一层只会
        # 引入相位问题（定时器与 present 差半拍时会稳定丢掉一半帧）。实测（900×600、60Hz 屏）：
        # pacing 60 → 60.6fps，不 pacing → 63.8fps；30Hz 源下不 pacing 也不会被再砍一半。
        del target_fps  # 保留签名兼容，DXGI 侧不再使用
        self._cam.start(region=region, target_fps=0)

    def _grab(self) -> tuple[np.ndarray, float] | None:
        assert self._cam is not None
        got = self._cam.get_latest_frame(with_timestamp=True)
        if got is None:
            return None
        frame, ts = got
        return np.asarray(frame, dtype=np.uint8), float(ts)

    def _close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam = None
