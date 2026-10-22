"""j-HDR 后端与回退链（2026-10-22）。

锁四件事：
1. `pick_monitor()` 跳过虚拟桌面、只认**完整包含**区域的单块屏（跨屏直接拒绝）；
2. `HdrBackend` 的 name / supports / is_available 契约；
3. `_grab()` 的异常映射：`InitialFrameTimeout` → None（算丢帧，不逼降级），
   `AccessLost` / `DimensionsChanged` → RuntimeError（让管理器回退或重建）；
4. `order_candidates()` 的回退链：选 j-HDR 时 hdr→dxgi→gdi，选 dxgi 时仍是 dxgi→gdi。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from yqrec.capture.hdr_backend import HdrBackend, pick_monitor
from yqrec.capture.manager import (
    CaptureManager,
    backend_label,
    default_backends,
    order_candidates,
)
from yqrec.types import Rect, ScreenTarget


# --------------------------------------------------------------------------- #
# 显示器挑选
# --------------------------------------------------------------------------- #
def _mon(index, rect, *, virtual=False, hdr=False, name="DISPLAY"):
    return SimpleNamespace(
        index=index,
        rect=rect,
        is_virtual_desktop=virtual,
        hdr_enabled=hdr,
        hdr_supported=hdr,
        friendly_name=name,
        device_name=f"\\\\.\\{name}",
    )


@pytest.fixture
def dual_monitors():
    """0 = 虚拟桌面（-2560,0 4480x1440），1 = 主屏 1920x1080，2 = 副屏（x 为负）。"""
    return [
        _mon(0, (-2560, 0, 4480, 1440), virtual=True),
        _mon(1, (0, 0, 1920, 1080), name="24B1"),
        _mon(2, (-2560, 0, 2560, 1440), name="KKTV"),
    ]


def test_pick_monitor_skips_virtual_desktop(dual_monitors) -> None:
    mon = pick_monitor(dual_monitors, Rect(100, 100, 400, 300))
    assert mon.index == 1


def test_pick_monitor_handles_negative_origin(dual_monitors) -> None:
    mon = pick_monitor(dual_monitors, Rect(-2000, 200, 400, 300))
    assert mon.index == 2


def test_pick_monitor_rejects_region_spanning_two_screens(dual_monitors) -> None:
    # 从副屏跨到主屏：单块屏都不完整包含
    with pytest.raises(ValueError, match="单块显示器"):
        pick_monitor(dual_monitors, Rect(-100, 100, 400, 300))


def test_pick_monitor_rejects_region_outside_all(dual_monitors) -> None:
    with pytest.raises(ValueError):
        pick_monitor(dual_monitors, Rect(9000, 9000, 100, 100))


# --------------------------------------------------------------------------- #
# 后端契约
# --------------------------------------------------------------------------- #
def test_backend_name_and_supports() -> None:
    be = HdrBackend()
    assert be.name == "hdr"
    assert backend_label("hdr") == "j-HDR"
    assert be.supports(ScreenTarget(monitor=0, rect=Rect(0, 0, 8, 8)))
    assert HdrBackend.is_available() is True  # 依赖已装（pyproject 里已声明）


# --------------------------------------------------------------------------- #
# _grab 的异常映射
# --------------------------------------------------------------------------- #
def _install_fake_hdrcapture(monkeypatch):
    class CaptureError(RuntimeError):
        pass

    class InitialFrameTimeout(CaptureError):
        pass

    class AccessLost(CaptureError):
        pass

    class DimensionsChanged(CaptureError):
        pass

    class InvalidMonitorIndex(CaptureError):
        pass

    mod = SimpleNamespace(
        CaptureError=CaptureError,
        InitialFrameTimeout=InitialFrameTimeout,
        AccessLost=AccessLost,
        DimensionsChanged=DimensionsChanged,
        InvalidMonitorIndex=InvalidMonitorIndex,
    )
    monkeypatch.setitem(sys.modules, "hdrcapture", mod)
    return mod


class _StubCap:
    def __init__(self, exc: Exception | None = None, frame=None) -> None:
        self._exc = exc
        self._frame = frame
        self.closed = False

    def grab_region(self, left, top, width, height):  # noqa: ANN001, ANN201
        if self._exc is not None:
            raise self._exc
        return self._frame

    def close(self) -> None:
        self.closed = True


def _opened_backend(cap) -> HdrBackend:  # noqa: ANN001
    be = HdrBackend()
    be._cap = cap
    be._region = (0, 0, 4, 2)
    return be


def test_grab_returns_none_when_first_present_missing(monkeypatch) -> None:
    mod = _install_fake_hdrcapture(monkeypatch)
    be = _opened_backend(_StubCap(exc=mod.InitialFrameTimeout("no present")))
    assert be._grab() is None  # 不抛，避免连续 20 次把管理器逼去降级


def test_grab_raises_on_access_lost(monkeypatch) -> None:
    mod = _install_fake_hdrcapture(monkeypatch)
    be = _opened_backend(_StubCap(exc=mod.AccessLost("hdr toggled")))
    with pytest.raises(RuntimeError, match="会话失效"):
        be._grab()


def test_grab_raises_on_dimensions_changed(monkeypatch) -> None:
    mod = _install_fake_hdrcapture(monkeypatch)
    be = _opened_backend(_StubCap(exc=mod.DimensionsChanged("resolution")))
    with pytest.raises(RuntimeError, match="会话失效"):
        be._grab()


def test_grab_converts_bgr_bytes_to_array(monkeypatch) -> None:
    _install_fake_hdrcapture(monkeypatch)
    pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])  # 4x2 单通道？这里用 2x2x3 更直观
    frame = SimpleNamespace(bgr=pixels, width=2, height=2)  # 2*2*3 = 12 ≠ 8
    be = _opened_backend(_StubCap(frame=frame))
    with pytest.raises(ValueError):
        be._grab()  # reshape 不上就报错，说明我们确实按 width*height*3 解释

    frame = SimpleNamespace(bgr=bytes(range(12)), width=2, height=2)
    be = _opened_backend(_StubCap(frame=frame))
    arr, ts = be._grab()
    assert arr.shape == (2, 2, 3)
    assert arr.dtype == np.uint8
    assert arr[0, 0].tolist() == [0, 1, 2]
    assert isinstance(ts, float)


# --------------------------------------------------------------------------- #
# 回退链
# --------------------------------------------------------------------------- #
def _names(cls_list) -> list[str]:  # noqa: ANN001
    return [getattr(c, "name", c.__name__) for c in cls_list]


def test_fallback_chain_for_jhdr_is_hdr_then_dxgi_then_gdi() -> None:
    from yqrec.capture.dxgi_backend import DxgiBackend
    from yqrec.capture.gdi_backend import GdiBackend

    ordered = order_candidates([DxgiBackend, GdiBackend, HdrBackend], "hdr")
    assert _names(ordered)[:3] == ["hdr", "dxgi", "gdi"]


def test_fallback_chain_for_dxgi_stays_dxgi_then_gdi() -> None:
    from yqrec.capture.dxgi_backend import DxgiBackend
    from yqrec.capture.gdi_backend import GdiBackend

    ordered = order_candidates([HdrBackend, DxgiBackend, GdiBackend], "dxgi")
    assert _names(ordered)[:2] == ["dxgi", "gdi"]
    assert _names(ordered)[2] == "hdr"  # 不会被偷偷提到 gdi 前面


def test_fallback_chain_for_gdi_only() -> None:
    from yqrec.capture.dxgi_backend import DxgiBackend
    from yqrec.capture.gdi_backend import GdiBackend

    assert _names(order_candidates([DxgiBackend, HdrBackend, GdiBackend], "gdi"))[0] == "gdi"


def test_no_preference_keeps_default_order() -> None:
    classes = default_backends()
    assert order_candidates(classes, None) == classes
    assert order_candidates(classes, "auto") == classes


def test_default_order_puts_hdr_first() -> None:
    """M3e：默认顺序 = j-HDR 打头，再 dxgi、gdi（CLI 的 `--backend auto` 也走这条）。

    断言用 `candidates()`（已按目标类型过滤）而不是原始类表 —— 类表里 wgc 排在 gdi 前面，
    但它只支持 WindowTarget，屏幕区域根本不会选它。
    """
    manager = CaptureManager()
    names = _names(manager.candidates(ScreenTarget(monitor=0, rect=Rect(0, 0, 8, 8))))
    assert names[:3] == ["hdr", "dxgi", "gdi"], names


# --------------------------------------------------------------------------- #
# 管理器：hdr 打不开时按链回退
# --------------------------------------------------------------------------- #
class _FakeBackend:
    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.opened = False

    @classmethod
    def is_available(cls) -> bool:
        return True

    def supports(self, target) -> bool:  # noqa: ANN001, ARG002
        return True

    def open(self, target, *, target_fps: int = 60) -> None:  # noqa: ANN001, ARG002
        if self._fail:
            raise RuntimeError("打不开")
        self.opened = True

    def close(self) -> None:
        self.opened = False


def _fake(name: str, *, fail: bool):  # noqa: ANN201
    return type(f"Fake_{name}", (_FakeBackend,), {"name": name, "__init__": lambda self: _FakeBackend.__init__(self, fail=fail)})


def test_manager_falls_back_hdr_to_dxgi() -> None:
    manager = CaptureManager(backends=[_fake("hdr", fail=True), _fake("dxgi", fail=False), _fake("gdi", fail=False)])
    backend = manager.start(ScreenTarget(monitor=0, rect=Rect(0, 0, 8, 8)), prefer="hdr")
    assert backend.name == "dxgi"
    assert any(err.startswith("hdr:") for err in manager.start_errors)
    manager.stop()


def test_manager_falls_back_hdr_to_gdi_when_dxgi_also_fails() -> None:
    manager = CaptureManager(
        backends=[_fake("hdr", fail=True), _fake("dxgi", fail=True), _fake("gdi", fail=False)]
    )
    backend = manager.start(ScreenTarget(monitor=0, rect=Rect(0, 0, 8, 8)), prefer="hdr")
    assert backend.name == "gdi"
    assert [err.split(":")[0] for err in manager.start_errors] == ["hdr", "dxgi"]
    manager.stop()
