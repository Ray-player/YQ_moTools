"""数据目录解析 + 乐器选择持久化（2026-10-23 用户实机反馈回归）。

两个坑一起锁住：

1. **数据目录必须是绝对路径**。旧实现 `os.environ.get("APPDATA") or Path.home()` 在
   `APPDATA` 为空/被写成相对路径时会把日志、设置、标定全写到**当前工作目录**（= exe
   旁边），用户看到的是"运行目录凭空多出 logs"，而 C 盘存档从此不再更新。
2. **乐器选择要进 `settings.json`**。旧版只在标定档里存 instrument，换乐器却没重新
   标定的话，启动时被标定档里的旧乐器拽回去（"乐器一直固定在老旧的诗琴"）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yqrec.config import DATA_DIR_NAME, config_path, data_dir, load_config, save_config, default_config


# --------------------------------------------------------------------------- #
# 1. 数据目录解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("broken", ["", "relative/xx", "  ", '"C:relative"'])
def test_data_dir_never_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    """APPDATA 为空 / 相对路径时，绝不返回相对路径（宁可退到注册表或主目录）。"""
    monkeypatch.setenv("APPDATA", broken)
    d = data_dir()
    assert d.is_absolute(), f"APPDATA={broken!r} 时数据目录变成了相对路径: {d}"
    assert d.name == DATA_DIR_NAME
    assert d != Path.cwd()


def test_data_dir_honours_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`YQREC_DATA_DIR` 显式覆盖（便携版 / 测试隔离用）。"""
    monkeypatch.setenv("YQREC_DATA_DIR", str(tmp_path / "portable"))
    assert data_dir() == tmp_path / "portable"


# --------------------------------------------------------------------------- #
# 2. 乐器选择持久化
# --------------------------------------------------------------------------- #
@pytest.fixture()
def make_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app, tmp_path


def _write_profile(tmp_path: Path, instrument: str) -> None:
    profile = {
        "name": "default",
        "instrument": instrument,
        "backend": "hdr",
        "ref_size": [800, 400],
        "created": "2026-10-22T10:00:00",
        "target": {"kind": "screen", "monitor": 0, "rect": [0, 0, 800, 400]},
        "keygrid": {
            "points": [[0.1 + 0.04 * i, 0.8, 50 / 1920] for i in range(21)],
            "key_count": 21,
        },
        "thresholds": {},
        "baseline": [[210, 205, 200]] * 21,
    }
    target = tmp_path / DATA_DIR_NAME / "profiles" / "default.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")


def _write_settings(tmp_path: Path, instrument: str) -> None:
    cfg = default_config()
    cfg.ui.instrument = instrument
    save_config(cfg, tmp_path / DATA_DIR_NAME / "settings.json")


def test_saved_instrument_wins_over_profile(make_window) -> None:
    """settings.json 里选过 jhzq，标定档是 ljdsq → 保持 jhzq，且不套用 ljdsq 的标定。"""
    app, tmp_path = make_window
    _write_settings(tmp_path, "jhzq")
    _write_profile(tmp_path, "ljdsq")

    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    assert window._spec.id == "jhzq", "用户选过的乐器不能被标定档拽回去"
    assert window._grid is None and window._baseline is None, "别的乐器的键位/基线不能套过来"
    assert window._region is None, "别的乐器的框选区域也不该套过来"
    window.close()
    app.processEvents()


def test_switching_instrument_is_persisted(make_window) -> None:
    """换乐器 → 写进 settings.json 的 ui.instrument，下次启动直接用它。"""
    app, tmp_path = make_window
    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    window._load_instrument("xljq")
    app.processEvents()
    assert window._spec.id == "xljq"
    saved = load_config(config_path())
    assert saved.ui.instrument == "xljq"
    window.close()
    app.processEvents()


def test_old_settings_without_instrument_follows_profile(make_window) -> None:
    """老档（settings.json 没有 instrument）→ 沿用旧行为：按标定档切乐器并恢复标定。"""
    app, tmp_path = make_window
    (tmp_path / DATA_DIR_NAME).mkdir(parents=True, exist_ok=True)
    _write_profile(tmp_path, "ljdsq")
    save_config(default_config(), config_path())  # 等价于旧版写的 settings.json

    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    assert window._spec.id == "ljdsq"
    assert window._baseline is not None, "老用户的标定不能因为加了这个字段就丢"
    window.close()
    app.processEvents()
