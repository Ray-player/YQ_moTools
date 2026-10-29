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


def test_sticky_data_dir_keeps_old_dir_when_primary_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """提权后主目录突然能写，但里面是空的 → 继续用已有存档的旧目录。

    否则用户看到的是"标定档没了、窗口大小回默认"（数据其实还在旧目录里）。
    """
    from yqrec import app

    primary = tmp_path / "appdata"
    fallback = tmp_path / "exe_data"
    primary.mkdir()
    (fallback / "profiles").mkdir(parents=True)
    (fallback / "profiles" / "default.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_fallback_candidates", lambda: [fallback])
    monkeypatch.setattr(app, "probe_writable_quiet", lambda _p: True)
    monkeypatch.delenv("YQREC_DATA_DIR", raising=False)

    note, detail = app._sticky_data_dir(primary, "Temp")
    try:
        assert os.environ.get("YQREC_DATA_DIR") == str(fallback)
        assert note and str(fallback) in detail
    finally:
        os.environ.pop("YQREC_DATA_DIR", None)


def test_sticky_data_dir_yields_to_primary_with_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """主目录自己已经有存档 → 以主目录为准（用户真搬过数据时不能被拽回去）。"""
    from yqrec import app

    primary = tmp_path / "appdata"
    fallback = tmp_path / "exe_data"
    (primary / "profiles").mkdir(parents=True)
    (primary / "profiles" / "default.json").write_text("{}", encoding="utf-8")
    fallback.mkdir()
    (fallback / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_fallback_candidates", lambda: [fallback])
    monkeypatch.setattr(app, "probe_writable_quiet", lambda _p: True)
    monkeypatch.delenv("YQREC_DATA_DIR", raising=False)

    note, _detail = app._sticky_data_dir(primary, "Temp")
    assert note == ""
    assert os.environ.get("YQREC_DATA_DIR") is None


def test_sticky_data_dir_keeps_old_dir_when_primary_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """提权后主目录突然能写，但里面是空的 → 继续用已有存档的旧目录。

    否则用户看到的是"标定档没了、窗口大小回默认"（数据其实还在旧目录里）。
    """
    from yqrec import app

    primary = tmp_path / "appdata"
    fallback = tmp_path / "exe_data"
    primary.mkdir()
    (fallback / "profiles").mkdir(parents=True)
    (fallback / "profiles" / "default.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_fallback_candidates", lambda: [fallback])
    monkeypatch.setattr(app, "probe_writable_quiet", lambda _p: True)
    monkeypatch.delenv("YQREC_DATA_DIR", raising=False)

    note, detail = app._sticky_data_dir(primary, "Temp")
    try:
        assert os.environ.get("YQREC_DATA_DIR") == str(fallback)
        assert note and str(fallback) in detail
    finally:
        os.environ.pop("YQREC_DATA_DIR", None)


def test_sticky_data_dir_yields_to_primary_with_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """主目录自己已经有存档 → 以主目录为准（用户真搬过数据时不能被拽回去）。"""
    from yqrec import app

    primary = tmp_path / "appdata"
    fallback = tmp_path / "exe_data"
    (primary / "profiles").mkdir(parents=True)
    (primary / "profiles" / "default.json").write_text("{}", encoding="utf-8")
    fallback.mkdir()
    (fallback / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_fallback_candidates", lambda: [fallback])
    monkeypatch.setattr(app, "probe_writable_quiet", lambda _p: True)
    monkeypatch.delenv("YQREC_DATA_DIR", raising=False)

    note, _detail = app._sticky_data_dir(primary, "Temp")
    assert note == ""
    assert os.environ.get("YQREC_DATA_DIR") is None
