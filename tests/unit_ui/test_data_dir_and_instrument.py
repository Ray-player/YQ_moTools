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


def test_probe_writable_detects_bad_dir(tmp_path: Path) -> None:
    """目标是个**文件**（无法当目录用）时必须报错，而不是假装可写。"""
    from yqrec.config import probe_writable

    assert probe_writable(tmp_path) is None, "正常目录应可写"
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert probe_writable(blocker) is not None


# --------------------------------------------------------------------------- #
# 1b. `archive_path`（instruments.json 里的存档路径开关）
# --------------------------------------------------------------------------- #
def _use_archive_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object) -> None:
    """把 `YQREC_ASSETS` 指到一个只含 archive_path 的最小 instruments.json。"""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "instruments.json").write_text(
        json.dumps({"archive_path": value}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("YQREC_ASSETS", str(assets))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("YQREC_DATA_DIR", raising=False)


def test_archive_path_local_goes_next_to_exe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`Local` → `<exe目录>/data`（源码运行时 = 项目根/data）。"""
    from yqrec.config import exe_dir, local_data_dir

    _use_archive_path(monkeypatch, tmp_path, "Local")
    assert data_dir() == exe_dir() / "data" == local_data_dir()


def test_archive_path_temp_goes_to_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`Temp` → `%APPDATA%\\YQ_moTools`（降级逻辑在 app 层，这里只验解析）。"""
    _use_archive_path(monkeypatch, tmp_path, "Temp")
    assert data_dir() == tmp_path / "roaming" / DATA_DIR_NAME


def test_archive_path_absolute_is_used_as_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """绝对路径直接用（不再拼 `YQ_moTools` 子目录）。"""
    target = tmp_path / "my_save"
    _use_archive_path(monkeypatch, tmp_path, str(target))
    assert data_dir() == target


def test_archive_path_relative_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """相对路径/空串视为非法 → 回落默认 %APPDATA%（绝不写到 cwd）。"""
    _use_archive_path(monkeypatch, tmp_path, "relative/save")
    assert data_dir() == tmp_path / "roaming" / DATA_DIR_NAME
    _use_archive_path(monkeypatch, tmp_path, "")
    assert data_dir() == tmp_path / "roaming" / DATA_DIR_NAME


def test_archive_path_env_override_still_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`YQREC_DATA_DIR` 优先级高于 archive_path（测试隔离 / 自动降级都靠它）。"""
    _use_archive_path(monkeypatch, tmp_path, "Local")
    monkeypatch.setenv("YQREC_DATA_DIR", str(tmp_path / "forced"))
    assert data_dir() == tmp_path / "forced"


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


# --------------------------------------------------------------------------- #
# 3. 消息区（窗口最下方）的过长日志 → 悬浮气泡显示完整内容
# --------------------------------------------------------------------------- #
def test_long_toast_gets_wrapped_tooltip(make_window) -> None:
    """超过 260px 的消息要挂气泡，并把长行折开（Qt 纯文本 tooltip 不会自己折行）。"""
    app, _tmp = make_window
    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    message = "存档写不进 C 盘，已改用 data 目录" * 6
    window._toast(message, "warn")
    app.processEvents()
    tip = window._lbl_toast.toolTip()
    assert tip, "长消息必须有悬浮气泡"
    assert "\n" in tip, "气泡内容要折行，否则仍是一长条"
    # 折行会吃掉断点处的空格，比较时忽略空白即可（内容本身不能丢字）
    assert message.replace(" ", "") in tip.replace("\n", "").replace(" ", "")
    window.close()
    app.processEvents()


def test_short_toast_has_no_tooltip(make_window) -> None:
    """放得下的短消息不挂气泡（避免鼠标一停就弹）。"""
    app, _tmp = make_window
    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    window._toast("已保存", "info")
    app.processEvents()
    assert window._lbl_toast.toolTip() == ""
    window.close()
    app.processEvents()


def test_toast_detail_forces_tooltip(make_window) -> None:
    """短句 + detail（例如失败原因）→ 气泡给 detail，消息区只留短句。"""
    app, _tmp = make_window
    from yqrec.ui.main_window import MainWindow

    window = MainWindow()
    window._toast("存档不可写", "warn", detail="原因：PermissionError: 拒绝访问")
    app.processEvents()
    assert window._lbl_toast.text() == "存档不可写"
    assert "PermissionError" in window._lbl_toast.toolTip()
    window.close()
    app.processEvents()


def test_startup_diagnostics_does_not_pop_dialog(make_window, monkeypatch) -> None:
    """存档写不进去时**不弹窗**，只在消息区给提示（用户明确要求）。"""
    app, _tmp = make_window
    import yqrec.ui.main_window as mw

    def _boom(*_args, **_kwargs):  # 弹窗被调到就说明违反了要求
        raise AssertionError("不应该弹 QMessageBox")

    monkeypatch.setattr(mw.QMessageBox, "warning", _boom)

    window = mw.MainWindow()
    window.show_startup_diagnostics(
        rows=[("数据目录", "E:/tmp/data")],
        log_failed=False,
        note="存档写不进 C 盘，已改用 data 目录（悬停看详情）",
        detail="原因：PermissionError: [WinError 5] 拒绝访问",
    )
    app.processEvents()
    assert window._lbl_toast.text().startswith("存档写不进 C 盘")
    assert "WinError 5" in window._lbl_toast.toolTip()
    window.close()
    app.processEvents()
