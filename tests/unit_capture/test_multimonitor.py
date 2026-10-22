"""多显示器 / 后端回退（M2q.6）。

背景：dxcam 默认绑定**主输出**，且 `region` 必须是该输出的**局部坐标**。区域框在副屏时
（全局 x 为负）会被 `_validate_region` 拒绝 → dxgi 打开失败 → 静默回退 gdi。
用户看到的现象就是"后端选 dxgi、状态栏却是 gdi"。这里锁三件事：

1. `local_region()` 的全局→局部换算与边界夹取；
2. `find_output()` 按区域中心挑对输出（含旧版 dxcam 的兜底）；
3. `CaptureManager.start_errors` 能说清"为什么回退了"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yqrec.capture.dxgi_backend import find_output, local_region
from yqrec.capture.manager import CaptureManager
from yqrec.types import Rect, ScreenTarget


# --------------------------------------------------------------------------- #
# 坐标换算
# --------------------------------------------------------------------------- #


def test_local_region_on_primary_output_is_identity() -> None:
    rect = Rect(100, 50, 400, 300)
    assert local_region(rect, (0, 0), (1920, 1080)) == (100, 50, 500, 350)


def test_local_region_on_secondary_output_shifts_by_origin() -> None:
    """副屏在左边：全局 x 是负的，换成局部坐标后必须是正的（dxcam 的硬要求）。"""
    rect = Rect(-2400, 200, 1000, 600)
    assert local_region(rect, (-2560, 0), (2560, 1440)) == (160, 200, 1160, 800)


def test_local_region_clamps_to_output_bounds() -> None:
    """区域跨出输出边界时夹进来，绝不能返回负数或超出 width/height。"""
    rect = Rect(-2600, -50, 600, 300)  # 左边越出副屏 40px、上边越出 50px
    # 全局 x -2600..-2000 → 局部 0..560（左边被夹掉 40px，右边仍在屏内）
    assert local_region(rect, (-2560, 0), (2560, 1440)) == (0, 0, 560, 250)


def test_local_region_rejects_non_overlapping_rect() -> None:
    with pytest.raises(ValueError):
        local_region(Rect(100, 100, 200, 200), (-2560, 0), (2560, 1440))


# --------------------------------------------------------------------------- #
# 选输出
# --------------------------------------------------------------------------- #


def _fake_dxcam(outputs: list[tuple[int, int, int, int]]) -> object:
    """`outputs` = 每个输出的 (left, top, right, bottom)，造一个最小 dxcam 替身。"""

    def create(*, device_idx: int, output_idx: int, **_kwargs):
        if device_idx != 0 or output_idx >= len(outputs):
            raise IndexError("no such output")
        left, top, right, bottom = outputs[output_idx]
        desc = SimpleNamespace(
            DesktopCoordinates=SimpleNamespace(left=left, top=top, right=right, bottom=bottom)
        )
        return SimpleNamespace(
            _output=SimpleNamespace(desc=desc),
            width=right - left,
            height=bottom - top,
        )

    return SimpleNamespace(create=create)


def test_find_output_picks_the_monitor_under_the_region_center() -> None:
    dxcam = _fake_dxcam([(0, 0, 1920, 1080), (-2560, 0, 0, 1440)])
    assert find_output(dxcam, 800, 500)[:2] == (0, 0)
    device, output, origin, size = find_output(dxcam, -1400, 500)
    assert (device, output) == (0, 1)
    assert origin == (-2560, 0)
    assert size == (2560, 1440)


def test_find_output_raises_when_no_output_matches() -> None:
    dxcam = _fake_dxcam([(0, 0, 1920, 1080)])
    with pytest.raises(RuntimeError):
        find_output(dxcam, 5000, 5000)


def test_find_output_falls_back_when_geometry_is_missing() -> None:
    """旧版 dxcam 没有 `_output.desc` → 返回 output_idx=None，调用方走老路子。"""
    cam = SimpleNamespace(width=1920, height=1080)
    dxcam = SimpleNamespace(create=lambda **_kw: cam)
    assert find_output(dxcam, 100, 100)[1] is None


# --------------------------------------------------------------------------- #
# 回退原因
# --------------------------------------------------------------------------- #


class _BadBackend:
    name = "dxgi"

    @classmethod
    def is_available(cls) -> bool:
        return True

    def supports(self, target) -> bool:  # noqa: ANN001
        return True

    def open(self, target, *, target_fps: int = 60) -> None:  # noqa: ANN001
        raise ValueError("Invalid Region: Region should be in 1920x1080")

    def latest(self, timeout_ms: int = 100):
        return None

    def close(self) -> None:
        pass

    def stats(self):
        raise NotImplementedError


class _GoodBackend(_BadBackend):
    name = "gdi"

    def open(self, target, *, target_fps: int = 60) -> None:  # noqa: ANN001
        return None


def test_start_errors_explains_the_fallback() -> None:
    manager = CaptureManager(backends=[_BadBackend, _GoodBackend])
    backend = manager.start(ScreenTarget(monitor=1, rect=Rect(0, 0, 100, 100)), prefer="dxgi")
    assert backend.name == "gdi"
    errors = manager.start_errors
    assert len(errors) == 1 and errors[0].startswith("dxgi:"), errors
    assert "Invalid Region" in errors[0], "要带上真实原因，不能只说'失败了'"


def test_start_clears_errors_between_calls() -> None:
    manager = CaptureManager(backends=[_BadBackend, _GoodBackend])
    target = ScreenTarget(monitor=0, rect=Rect(0, 0, 100, 100))
    manager.start(target, prefer="dxgi")
    assert manager.start_errors
    manager.start(target, prefer="gdi")
    assert manager.start_errors == [], "换后端重试时不能残留上一次的失败原因"
