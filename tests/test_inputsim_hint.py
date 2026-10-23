"""注入诊断：目标窗口/权限判定的回归（2026-10-23 用户报"日志有点击记录但游戏没反应"）。

用户当时的日志是 `录制启动空格注入: 窗口 0x1059a` —— 而那个句柄其实是 **msedge**。
也就是说 `SendInput` 完全成功，只是打到别的窗口上了。为了让这类问题一眼可见，
`inputsim` 新增了 `describe()`（窗口→进程名+标题）与 `injection_hint()`（UIPI 判定）。
"""

from __future__ import annotations

import pytest

from yqrec import inputsim


def test_injection_hint_warns_when_target_is_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """目标以管理员运行、本程序没有 → 必须给出"注入会被丢弃"的提示。"""
    monkeypatch.setattr(inputsim, "window_process", lambda _hwnd: (4242, "GenshinImpact.exe"))
    monkeypatch.setattr(inputsim, "is_elevated", lambda pid: pid == 4242)
    hint = inputsim.injection_hint(0x1234)
    assert "管理员" in hint
    assert "GenshinImpact.exe" in hint


def test_injection_hint_silent_when_same_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """双方权限一致（都提权或都没提权）→ 不提示。"""
    monkeypatch.setattr(inputsim, "window_process", lambda _hwnd: (4242, "YuanShen.exe"))
    monkeypatch.setattr(inputsim, "is_elevated", lambda _pid: True)
    assert inputsim.injection_hint(0x1234) == ""


def test_injection_hint_silent_when_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    """读不到目标权限（反作弊保护）→ 不误报，交给日志里的进程名判断。"""
    monkeypatch.setattr(inputsim, "window_process", lambda _hwnd: (4242, "Yuanshen.exe"))
    monkeypatch.setattr(inputsim, "is_elevated", lambda _pid: None)
    assert inputsim.injection_hint(0x1234) == ""


def test_describe_includes_process_and_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志行要能看出"打到谁身上"：句柄 + 进程名 + 标题。"""
    monkeypatch.setattr(inputsim, "window_process", lambda _hwnd: (7, "msedge.exe"))
    monkeypatch.setattr(inputsim, "window_title", lambda _hwnd: "哔哩哔哩")
    text = inputsim.describe(0x1059A)
    assert "0x1059a" in text and "msedge.exe" in text and "哔哩哔哩" in text
    assert inputsim.describe(0) == "0x0（无窗口）"
