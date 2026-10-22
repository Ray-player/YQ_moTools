"""`settings.json` 的默认值与版本迁移（M2q.7）。

两件事必须锁住：

1. **首次运行（无 `%APPDATA%\\YQRec\\settings.json`）的默认窗口状态**要等于用户实机调好的
   那一套（窄长条 + 右下贴边），否则每次重装/换机都要重新摆窗口；
2. **v1 → v2 迁移**：旧文件里的 `record.min_hold_ms = 25` 是当时的固定值（不是用户选择），
   必须重置为 0，否则 M2q.4「单帧即确认」的修复会被文件里的旧值悄悄吃掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yqrec.config import (
    CONFIG_VERSION,
    DEFAULT_MIN_HOLD_MS,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    DEFAULT_WINDOW_X,
    DEFAULT_WINDOW_Y,
    default_config,
    load_config,
    save_config,
)


def test_defaults_match_last_known_good_window() -> None:
    """无缓存数据时的默认窗口 = 用户上次运行调好的尺寸与位置。"""
    ui = default_config().ui
    assert (ui.window_width, ui.window_height) == (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
    assert (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT) == (558, 913), "窄长条竖排"
    assert (ui.window_x, ui.window_y) == (DEFAULT_WINDOW_X, DEFAULT_WINDOW_Y)
    assert (DEFAULT_WINDOW_X, DEFAULT_WINDOW_Y) == (1393, 50), "主屏右下贴边"
    assert ui.window_maximized is False
    assert ui.always_on_top is True, "贴游戏用，默认置顶"


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    """配置文件不存在 → 完全用默认值（不是空配置、也不是报错）。"""
    cfg = load_config(tmp_path / "not_there.json")
    assert cfg.ui.window_width == DEFAULT_WINDOW_WIDTH
    assert (cfg.ui.window_x, cfg.ui.window_y) == (DEFAULT_WINDOW_X, DEFAULT_WINDOW_Y)
    assert cfg.version == CONFIG_VERSION


def test_default_backend_is_jhdr() -> None:
    """M3e：默认截图方式 = j-HDR（HDR 屏只有它颜色对；SDR 下与 dxgi 等价）。"""
    assert default_config().backend == "hdr"
    assert load_config(Path("不存在.json")).backend == "hdr"


def test_v3_backend_migrated_to_hdr(tmp_path: Path) -> None:
    """v3 及更早档里的 backend 是序列化出来的旧默认值（UI 从不回写）→ 强制对齐 hdr。"""
    data = {"version": 3, "backend": "dxgi", "ui": {"theme": "dark"}}
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.backend == "hdr", "旧值不可信，按版本号重置为新的默认后端"
    assert cfg.ui.theme == "dark", "v4 迁移不该顺手改主题（v2→v3 才管主题）"


def test_v4_backend_choice_is_kept(tmp_path: Path) -> None:
    """v4 起 backend 才是真实选择：用户选 gdi 就必须原样读回，不能被迁移覆盖。"""
    data = {"version": 4, "backend": "gdi"}
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_config(p).backend == "gdi"


def test_v1_resets_min_hold_but_keeps_window(tmp_path: Path) -> None:
    """旧档：`min_hold_ms` 归 0（旧固定值已失去意义），其他用户改动一律保留。"""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "record": {"min_hold_ms": 25.0, "min_gap_ms": 45.0},
                "ui": {"window_width": 700, "window_height": 900, "window_x": 10, "window_y": 20},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.record.min_hold_ms == DEFAULT_MIN_HOLD_MS == 0.0
    assert cfg.record.min_gap_ms == 45.0, "只迁这一项，别的用户配置别乱动"
    assert (cfg.ui.window_width, cfg.ui.window_height) == (700, 900)
    assert (cfg.ui.window_x, cfg.ui.window_y) == (10, 20)
    assert cfg.version == CONFIG_VERSION, "迁移后落新版本，避免每次启动重复迁移"


def test_v2_keeps_user_min_hold(tmp_path: Path) -> None:
    """新档里用户自己调过的 `min_hold_ms` 必须尊重（特效误触时可手动加时间闸）。"""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"version": 2, "record": {"min_hold_ms": 40.0}}), encoding="utf-8"
    )
    assert load_config(p).record.min_hold_ms == pytest.approx(40.0)


def test_migration_is_not_written_back_until_save(tmp_path: Path) -> None:
    """迁移只在内存生效；`save_config()` 之后文件才变成新版本（不偷偷改用户文件）。"""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "record": {"min_hold_ms": 25.0}}), encoding="utf-8")
    cfg = load_config(p)
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == 1, "读的时候不写盘"

    save_config(cfg, p)
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["version"] == CONFIG_VERSION
    assert saved["record"]["min_hold_ms"] == 0.0
    assert saved["ui"]["window_width"] == DEFAULT_WINDOW_WIDTH, "保存时把默认窗口也带上了"


def test_v2_resets_theme_to_light(tmp_path: Path) -> None:
    """旧档里的 `theme="dark"` 是"旧默认值被序列化"，不是用户选择 → 升级后回日间。

    M3a 之前界面上**没有**主题切换入口，所以 v2 档不可能存在"用户主动选的夜间"。
    这条不修的话，升级后首次启动会直接进夜间（用户实机反馈过）。
    """
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "ui": {"theme": "dark", "window_width": 645, "always_on_top": True},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.ui.theme == "light", "升级后默认必须回到日间"
    assert cfg.ui.window_width == 645, "只迁主题，窗口尺寸等用户改动一律保留"
    assert cfg.ui.always_on_top is True
    assert cfg.version == CONFIG_VERSION


def test_v3_keeps_user_chosen_dark(tmp_path: Path) -> None:
    """M3a 之后用户主动切到夜间：档里是 v3，迁移不得再覆盖。"""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"version": 3, "ui": {"theme": "dark"}}), encoding="utf-8"
    )
    assert load_config(p).ui.theme == "dark"


def test_default_theme_is_light(tmp_path: Path) -> None:
    """没有配置档（首次运行）→ 日间。"""
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.ui.theme == "light"

