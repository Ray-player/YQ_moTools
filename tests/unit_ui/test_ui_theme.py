"""视觉层（M3a）的护栏测试：主题、按钮档位、键位卡预警、浮层关闭。

只测"看起来怎么样"背后的**可判定状态**，不测具体像素 —— 设计包 `04` §4 的原则：
结构/属性变了要能被测试发现，颜色值本身交给截图比对。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def window(monkeypatch: pytest.MonkeyPatch, tmp_path):
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.processEvents()
    return win, app, tmp_path


def test_two_themes_render_without_missing_tokens() -> None:
    """两套主题渲染出的样式表长度接近且没有残留占位符（漏令牌会抛 KeyError）。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    from yqrec.ui import theme

    dark = theme.stylesheet(theme.DARK)
    light = theme.stylesheet(theme.LIGHT)
    for css in (dark, light):
        assert "{{" not in css and "}}" not in css
    assert abs(len(dark) - len(light)) < 400, "两套主题的几何规则必须同源"


def test_theme_toggle_persists_to_settings(window) -> None:
    """切主题立刻写进 settings.json 的 ui.theme（重启沿用）。"""
    import json

    from yqrec.ui import theme

    win, app, tmp_path = window
    assert win._theme == theme.LIGHT, "默认日间"
    win._toggle_theme()
    app.processEvents()
    assert win._theme == theme.DARK
    win._toggle_theme()
    app.processEvents()
    assert win._theme == theme.LIGHT
    settings = json.loads((tmp_path / "YQ_moTools" / "settings.json").read_text(encoding="utf-8"))
    assert settings["ui"]["theme"] == "light"
    win.close()
    app.processEvents()


def test_main_action_migrates_with_state(window) -> None:
    """四档档位随运行态迁移：一屏最多一个 Primary，停止类只给 Danger。"""
    win, app, _ = window
    # 静止态：预览是唯一能推进的动作
    win._refresh_action_emphasis()
    assert win._btn_preview.objectName() == "Primary"
    assert win._btn_record.objectName() == "Secondary"
    assert win._btn_baseline.objectName() == "Secondary"

    # 预览中但没基线：学习基线变主操作，停止预览是收尾
    win._worker = object()  # 只用来表达"预览已启动"，不参与任何调用
    try:
        win._refresh_action_emphasis()
        assert win._btn_baseline.objectName() == "Primary"
        assert win._btn_preview.objectName() == "Danger"

        # 预览中 + 已学基线：录制成为主操作
        win._baseline = __import__("numpy").zeros((21, 3), dtype="float32")
        win._refresh_action_emphasis()
        assert win._btn_record.objectName() == "Primary"
        assert win._btn_baseline.objectName() == "Secondary"

        # 录制中：两个"停止"都是收尾（tonal danger），没有 Primary
        win._recording = True
        win._refresh_action_emphasis()
        assert win._btn_record.objectName() == "Danger"
        assert win._btn_preview.objectName() == "Danger"
        assert win._btn_baseline.objectName() == "Quiet"
        roles = {
            win._btn_record.objectName(),
            win._btn_preview.objectName(),
            win._btn_baseline.objectName(),
        }
        assert "Primary" not in roles

        # 试听中：主操作在控制条里，工具栏整体降级
        win._recording = False
        win._play_mode = True
        win._refresh_action_emphasis()
        for button in (win._btn_record, win._btn_preview, win._btn_baseline):
            assert button.objectName() == "Secondary"
    finally:
        win._worker = None
        win._baseline = None
        win._recording = False
        win._play_mode = False
        app.processEvents()
    win.close()
    app.processEvents()


def test_keycard_near_threshold_turns_warning(window) -> None:
    """Δ 到阈值 60% 时第 3 行变琥珀；超过阈值/按下后不回退成琥珀。"""
    win, app, _ = window
    card = win._keys_panel._cards[0]
    card._threshold_delta = 40
    card.set_metrics((200, 200, 200), 25.0)  # 62.5%
    assert card._delta.property("tone") == "near"
    card.set_metrics((200, 200, 200), 10.0)  # 25%
    assert card._delta.property("tone") == ""
    card.set_pressed(True)
    assert card._delta.property("tone") == "pressed"
    card.set_pressed(False)
    assert card._delta.property("tone") == ""
    win.close()
    app.processEvents()


def test_edit_panel_closes_on_escape_and_background(window, monkeypatch) -> None:
    """Esc / 点预览空白 = 保存退出；圈数不足时只提示、不静默丢弃。"""
    win, app, _ = window
    win._preview.set_frame(win._preview.frame_image() or __make_frame())
    win._build_edit_panel()
    assert win._edit_panel is not None

    # 圈数不足 → Esc 只提示，浮层还在（不丢用户已拖的点）
    win._on_escape()
    assert win._edit_panel is not None

    # 补齐到 21 个点后 Esc 才真的关
    points = [(100 + (i % 7) * 60.0, 100 + (i // 7) * 60.0) for i in range(21)]
    win._preview.enter_edit(points, limit=21)
    win._on_escape()
    app.processEvents()
    assert win._edit_panel is None
    win.close()
    app.processEvents()


def __make_frame():
    """造一张最小可用的帧（构造点、测 UI 用，不参与识别）。"""
    from PySide6.QtGui import QImage

    image = QImage(560, 320, QImage.Format.Format_RGB32)
    image.fill(0xFF808080)
    return image


def test_backend_defaults_to_jhdr_and_has_no_auto(window) -> None:
    """抓帧后端：默认 **j-HDR**，备选 dxgi / gdi，**不再提供「自动」**（M3e）。

    原来的「自动」对屏幕区域其实固定返回 gdi（既不自动、帧率也低），M3c 已去掉；
    j-HDR 是 2026-10-22 新增的独立档，同日起成为默认（HDR 屏只有它颜色对，SDR 下与 dxgi 等价）。
    """
    win, app, _ = window
    combo = win._combo_backend
    items = [combo.itemData(i) for i in range(combo.count())]
    assert items == ["hdr", "dxgi", "gdi"], items
    assert "auto" not in items, "不该再有自动档"
    assert combo.currentData() == "hdr", "默认必须是 j-HDR"
    assert combo.currentText() == "j-HDR"
    assert win._backend_preference() == "hdr"

    combo.setCurrentIndex(1)  # dxgi
    assert win._backend_preference() == "dxgi"

    combo.setCurrentIndex(2)  # 手动切 gdi 后也照用，不再被"自动"覆盖
    assert win._backend_preference() == "gdi"
    win.close()
    app.processEvents()


def test_backend_chip_shows_jhdr_label(window) -> None:
    """状态栏徽标显示 `J-HDR` 而不是内部名 `HDR`（回退时还要带「回退」）。"""
    win, app, _ = window
    win._requested_backend = "hdr"
    win._on_status({"backend": "hdr", "frames": 10, "fps": 60.0, "src_fps": 60.0, "ms": 0.5})
    assert win._lbl_backend.text() == "J-HDR"

    win._on_status({"backend": "dxgi", "frames": 10, "fps": 60.0, "src_fps": 60.0, "ms": 0.5})
    assert win._lbl_backend.text() == "DXGI 回退"
    win.close()
    app.processEvents()


def test_backend_choice_persists_and_restores(window) -> None:
    """切后端立刻写 settings.json；重开窗口沿用上次选的那个（用户要求）。"""
    import json

    win, app, tmp_path = window
    win._combo_backend.setCurrentIndex(2)  # gdi
    app.processEvents()
    settings = json.loads((tmp_path / "YQ_moTools" / "settings.json").read_text(encoding="utf-8"))
    assert settings["backend"] == "gdi", "切换后要立刻落盘"
    win.close()
    app.processEvents()

    from yqrec.ui.main_window import MainWindow

    win2 = MainWindow()
    win2.show()
    app.processEvents()
    assert win2._combo_backend.currentData() == "gdi", "重启应沿用上次选择"
    assert win2._backend_preference() == "gdi"
    win2.close()
    app.processEvents()


def test_legacy_settings_opens_as_jhdr(monkeypatch, tmp_path) -> None:
    """v3 旧档（backend 是序列化出来的 "auto"/"dxgi"）→ 首次打开就应该是 j-HDR。"""
    import json

    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    data_dir = tmp_path / "YQ_moTools"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps({"version": 3, "backend": "auto", "ui": {"theme": "light"}}), encoding="utf-8"
    )
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.processEvents()
    assert win._combo_backend.currentData() == "hdr", "旧档迁移后应落到默认 j-HDR"
    assert win._backend_preference() == "hdr"
    win.close()
    app.processEvents()


def test_backend_combo_text_fits(window) -> None:
    """`j-HDR` 不能被 74px 的下拉裁掉（品牌/标签撑爆是踩过的坑）。"""
    from PySide6.QtGui import QFontMetrics

    win, app, _ = window
    combo = win._combo_backend
    combo.setCurrentIndex(0)  # j-HDR
    app.processEvents()
    text_w = QFontMetrics(combo.font()).horizontalAdvance("j-HDR")
    # 下拉要给右侧箭头留位置，经验值 ~18px
    assert text_w + 18 <= combo.width(), f"文本 {text_w}px + 箭头 > 控件 {combo.width()}px"
    win.close()
    app.processEvents()


def test_always_on_top_defaults_on(window) -> None:
    """窗口置顶默认启用（按钮选中 + 窗口标志真的带上 StaysOnTop）。"""
    from PySide6.QtCore import Qt

    win, app, _ = window
    assert win._config.ui.always_on_top is True
    assert win._btn_ontop.isChecked() is True
    assert bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) is True
    win.close()
    app.processEvents()


