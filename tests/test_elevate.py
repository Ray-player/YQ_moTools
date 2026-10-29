"""启动自提权：抄 BetterGI 的 `RuntimeHelper.EnsureElevated`。

为什么需要它：原神 PC 端以管理员权限运行，UIPI 会把普通权限进程注入的键鼠**静默丢弃**
（`SendInput` 返回成功、游戏没反应）。BetterGI 的解法是每次启动都 `Verb="runas"`
重启自己，这里验的就是同一套判断顺序。

`shell_execute_runas` 一律被替换：真跑会弹 UAC 对话框，测试里绝不能真弹。
"""

from __future__ import annotations

import os

import pytest

from yqrec import elevate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """清掉两个开关，并在用例结束后把 `ensure_admin` 写进去的标记也擦干净。"""
    monkeypatch.delenv(elevate.ENV_NO_ELEVATE, raising=False)
    monkeypatch.delenv(elevate.ENV_ELEVATE_TRIED, raising=False)
    yield
    os.environ.pop(elevate.ENV_ELEVATE_TRIED, None)
    os.environ.pop(elevate.ENV_NO_ELEVATE, None)


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """记录 `shell_execute_runas` 的调用，并默认返回 33（>32 = 成功）。"""
    recorded: list[tuple[str, str, str]] = []
    monkeypatch.setattr(elevate, "_breadcrumb", lambda _text: None)  # 别往 %TEMP% 里写真文件

    def fake(exe: str, params: str, workdir: str) -> int:
        recorded.append((exe, params, workdir))
        return 33

    monkeypatch.setattr(elevate, "shell_execute_runas", fake)
    return recorded


def test_already_admin_does_nothing(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setattr(elevate, "is_admin", lambda: True)
    status, note = elevate.ensure_admin([])
    assert status == elevate.Elevation.ADMIN
    assert note == ""
    assert calls == [], "已经是管理员就不该再启动一个实例"


def test_relaunches_when_frozen_and_not_admin(
    monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    monkeypatch.setattr(elevate, "is_admin", lambda: False)
    monkeypatch.setattr(elevate, "is_frozen", lambda: True)
    monkeypatch.setattr(elevate.sys, "executable", r"C:\tools\YQ_moTools\YQ_moTools.exe")
    status, _note = elevate.ensure_admin(["gui"])
    assert status == elevate.Elevation.RELAUNCHED
    (exe, params, workdir), = calls
    assert exe.endswith("YQ_moTools.exe")
    assert params == "gui"
    assert workdir.endswith("YQ_moTools")
    assert os.environ.get(elevate.ENV_ELEVATE_TRIED) == "1", "必须打标记，防止子进程再弹一次"


def test_cancelled_uac_keeps_running(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setattr(elevate, "is_admin", lambda: False)
    monkeypatch.setattr(elevate, "is_frozen", lambda: True)
    monkeypatch.setattr(elevate, "shell_execute_runas", lambda *_a: elevate.ERROR_CANCELLED)
    status, note = elevate.ensure_admin([])
    assert status == elevate.Elevation.CANCELLED
    assert "取消" in note


def test_never_loops_after_a_tried_relaunch(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """上一次提权过却仍是普通权限 → 不再尝试（否则每次启动都要过一遍 UAC 还进不去）。"""
    monkeypatch.setattr(elevate, "is_admin", lambda: False)
    monkeypatch.setattr(elevate, "is_frozen", lambda: True)
    monkeypatch.setenv(elevate.ENV_ELEVATE_TRIED, "1")
    status, _note = elevate.ensure_admin([])
    assert status == elevate.Elevation.FAILED
    assert calls == []


def test_source_run_is_skipped(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """源码运行（python.exe）不重启：`sys.executable` 是解释器，重启只会丢掉 `-m yqrec`。"""
    monkeypatch.setattr(elevate, "is_admin", lambda: False)
    monkeypatch.setattr(elevate, "is_frozen", lambda: False)
    status, note = elevate.ensure_admin([])
    assert status == elevate.Elevation.SKIPPED
    assert "源码" in note
    assert calls == []


def test_env_opt_out(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setenv(elevate.ENV_NO_ELEVATE, "1")
    monkeypatch.setattr(elevate, "is_admin", lambda: False)
    monkeypatch.setattr(elevate, "is_frozen", lambda: True)
    status, _note = elevate.ensure_admin([])
    assert status == elevate.Elevation.SKIPPED
    assert calls == []


def test_unknown_elevation_state_does_not_relaunch(
    monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    """读不到自己的令牌（受保护进程）→ 保守地不重启，别把用户卡在 UAC 循环里。"""
    monkeypatch.setattr(elevate, "is_admin", lambda: None)
    monkeypatch.setattr(elevate, "is_frozen", lambda: True)
    status, _note = elevate.ensure_admin([])
    assert status == elevate.Elevation.SKIPPED
    assert calls == []


def test_status_text_is_human_readable() -> None:
    assert "管理员" in elevate.status_text(elevate.Elevation.ADMIN)
    text = elevate.status_text(elevate.Elevation.FAILED, "ShellExecuteW 返回 5")
    assert "普通用户" in text and "ShellExecuteW 返回 5" in text
