"""把"当前工作目录"从程序目录挪走（2026-10-23 用户问 debug.log）。

背景：搜狗输入法会把自己的 debug.log 写进**宿主进程的 cwd**，于是交付目录里多出一个
跟本程序无关的 debug.log（内容全是 `sgime_skin…` / `scheme=Sogou`）。程序没法让 IME
不写，只能把 cwd 挪到临时目录。这里锁住两条：能挪、且不会把相对路径写回程序目录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _clean_temp_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.delenv(name, raising=False)


def test_detach_cwd_moves_to_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_temp_vars(monkeypatch)
    monkeypatch.setenv("TEMP", str(tmp_path))

    from yqrec.app import _detach_cwd

    original = Path.cwd()
    try:
        _detach_cwd()
        assert Path.cwd() == tmp_path / "yqrec-cwd"
        Path("debug.log").write_text("ime noise", encoding="utf-8")
        assert (tmp_path / "yqrec-cwd" / "debug.log").exists(), "相对路径应落在临时目录"
    finally:
        os.chdir(original)


def test_detach_cwd_ignores_relative_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEMP 是相对路径时不动 cwd（宁可保持原样，也不往怪位置挪）。"""
    _clean_temp_vars(monkeypatch)
    monkeypatch.setenv("TEMP", "relative/tmp")

    from yqrec.app import _detach_cwd

    before = Path.cwd()
    _detach_cwd()
    assert Path.cwd() == before
