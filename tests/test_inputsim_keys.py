"""键事件构造：`_key_input()` 必须 **VK + 扫描码双填**（对齐 BetterGI）。

背景：旧实现是 `wVk=0` + `KEYEVENTF_SCANCODE`（只发扫描码），系统要按**当前键盘布局**
反查虚拟键，布局不是 US-QWERTY 或输入法激活时可能推出别的字符；`GetKeyState(VK)` 也
查不到按下态。BetterGI 的 `InputBuilder.AddKeyDown` 一直是双填，这里照它改。

这些用例只用替换 `_dispatch` 拦截构造好的 `INPUT`，**不碰真实键鼠**。
"""

from __future__ import annotations

import pytest

from yqrec import inputsim


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list:
    """拦截 `_dispatch`：拿到即将交给 SendInput 的 INPUT，并假装发送成功。"""
    items: list = []

    def fake_dispatch(inp):  # noqa: ANN001 - 与真实签名一致
        items.append(inp)
        return True

    monkeypatch.setattr(inputsim, "_dispatch", fake_dispatch)
    return items


def _scan_of(vk: int) -> int:
    return int(inputsim.user32.MapVirtualKeyW(int(vk), 0)) & 0xFF


def test_key_down_fills_vk_and_scan_code(captured: list) -> None:
    """按下事件：wVk = 虚拟键码，wScan = 扫描码，且**不带** SCANCODE 位。"""
    assert inputsim._send_key(inputsim.virtual_key("A"))
    (inp,) = captured
    assert inp.ki.wVk == 0x41, "VK 必须填（GetKeyState/普通窗口靠它）"
    assert inp.ki.wScan == _scan_of(0x41), "扫描码也必须填（RawInput/DirectInput 靠它）"
    assert inp.ki.dwFlags & inputsim.KEYEVENTF_SCANCODE == 0, "双填不走 SCANCODE 模式"
    assert inp.ki.dwFlags & inputsim.KEYEVENTF_KEYUP == 0


def test_key_up_sets_keyup_flag_and_keeps_both_codes(captured: list) -> None:
    assert inputsim._send_key(inputsim.virtual_key("Q"), inputsim.KEYEVENTF_KEYUP)
    (inp,) = captured
    assert inp.ki.wVk == 0x51
    assert inp.ki.wScan == _scan_of(0x51)
    assert inp.ki.dwFlags & inputsim.KEYEVENTF_KEYUP


def test_scan_code_only_mode_still_available(captured: list) -> None:
    """`KEYEVENTF_SCANCODE` 的老路径保留（只发扫描码，wVk 置 0）作兜底。"""
    assert inputsim._send_key(0x41, inputsim.KEYEVENTF_SCANCODE)
    (inp,) = captured
    assert inp.ki.wVk == 0
    assert inp.ki.wScan == _scan_of(0x41)
    assert inp.ki.dwFlags & inputsim.KEYEVENTF_SCANCODE


def test_press_space_uses_dual_fill_by_default(captured: list) -> None:
    """空格默认走双填（扫描码照旧带着，DirectInput 的游戏也认），不再只发扫描码。"""
    assert inputsim.press_space(hold_ms=0)
    down, up = captured
    for inp in (down, up):
        assert inp.ki.wVk == inputsim.VK_SPACE
        assert inp.ki.wScan == _scan_of(inputsim.VK_SPACE)
        assert inp.ki.dwFlags & inputsim.KEYEVENTF_SCANCODE == 0
    assert up.ki.dwFlags & inputsim.KEYEVENTF_KEYUP


def test_send_key_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """SendInput 失败（被安全软件/UIPI 丢弃）必须返回 False，交给上层记账。"""
    monkeypatch.setattr(inputsim, "_dispatch", lambda _inp: False)
    assert inputsim._send_key(0x41) is False
