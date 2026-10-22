"""读取各活动显示器的高级色彩（HDR）状态与 SDR 白电平（ctypes 调 DisplayConfig）。

比 IDXGIOutput6 更可靠：IDXGIOutput6 在部分驱动/虚拟显示上 QI 会 E_NOINTERFACE，
而 DisplayConfig 这条路 Win10 1703+ 一律可用。

运行：.venv/Scripts/python.exe out/probe_hdr_disp.py
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int32, c_uint32, c_ubyte, Structure

user32 = ctypes.WinDLL("user32", use_last_error=True)

QDC_ONLY_ACTIVE_PATHS = 2
GET_SOURCE_NAME = 1
GET_ADVANCED_COLOR_INFO = 9
GET_SDR_WHITE_LEVEL = 11


class LUID(Structure):
    _fields_ = [("LowPart", c_uint32), ("HighPart", c_int32)]


class PATH_SOURCE_INFO(Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("statusFlags", c_uint32),
    ]


class PATH_TARGET_INFO(Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("outputTechnology", c_uint32),
        ("rotation", c_uint32),
        ("scaling", c_uint32),
        ("refreshRateNum", c_uint32),
        ("refreshRateDen", c_uint32),
        ("statusFlags", c_uint32),
    ]


class PATH_INFO(Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE_INFO), ("targetInfo", PATH_TARGET_INFO), ("flags", c_uint32)]


class MODE_INFO(Structure):
    _fields_ = [
        ("infoType", c_uint32),
        ("id", c_uint32),
        ("adapterId", LUID),
        ("pad", c_ubyte * 48),
    ]


class DEVICE_INFO_HEADER(Structure):
    _fields_ = [("type", c_uint32), ("size", c_uint32), ("adapterId", LUID), ("id", c_uint32)]


class ADVANCED_COLOR_INFO(Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("value", c_uint32),
        ("colorEncoding", c_uint32),
        ("bitsPerColorChannel", c_uint32),
    ]


class SDR_WHITE_LEVEL(Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("sdrWhiteLevel", c_uint32)]


class SOURCE_NAME(Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("flags", c_uint32),
        ("viewGdiDeviceName", ctypes.c_wchar * 64),
    ]


def main() -> int:
    np = c_uint32()
    nm = c_uint32()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, byref(np), byref(nm)) != 0:
        print("GetDisplayConfigBufferSizes failed")
        return 1
    paths = (PATH_INFO * np.value)()
    modes = (MODE_INFO * nm.value)()
    hr = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, byref(np), paths, byref(nm), modes, None)
    if hr != 0:
        print(f"QueryDisplayConfig failed hr={hr}")
        return 1

    for i in range(np.value):
        p = paths[i]
        aci = ADVANCED_COLOR_INFO()
        aci.header.type = GET_ADVANCED_COLOR_INFO
        aci.header.size = ctypes.sizeof(ADVANCED_COLOR_INFO)
        aci.header.adapterId = p.targetInfo.adapterId
        aci.header.id = p.targetInfo.id
        hr1 = user32.DisplayConfigGetDeviceInfo(byref(aci))

        wl = SDR_WHITE_LEVEL()
        wl.header.type = GET_SDR_WHITE_LEVEL
        wl.header.size = ctypes.sizeof(SDR_WHITE_LEVEL)
        wl.header.adapterId = p.targetInfo.adapterId
        wl.header.id = p.targetInfo.id
        hr2 = user32.DisplayConfigGetDeviceInfo(byref(wl))

        sn = SOURCE_NAME()
        sn.header.type = GET_SOURCE_NAME
        sn.header.size = ctypes.sizeof(SOURCE_NAME)
        sn.header.adapterId = p.sourceInfo.adapterId
        sn.header.id = p.sourceInfo.id
        user32.DisplayConfigGetDeviceInfo(byref(sn))

        sup = bool(aci.value & 1)
        en = bool(aci.value & 2)
        wide = bool(aci.value & 4)
        forced = bool(aci.value & 8)
        nits = wl.sdrWhiteLevel * 80.0 / 1000.0
        print(
            f"{sn.viewGdiDeviceName or '?':<12} HDR支持={sup} 已启用={en} 广色域={wide} 强制关={forced} "
            f"位深={aci.bitsPerColorChannel or '-'} SDR白={wl.sdrWhiteLevel / 1000.0:.3f} scRGB ({nits:.0f} nits) "
            f"[hr {hr1}/{hr2}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
