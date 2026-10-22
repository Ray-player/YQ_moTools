"""M1 GUI 最小冒烟（只保 3 条，不做虚拟测试堆砌）。

1. `thresholds_from_baseline` 的核心算术：产出 `max_drop` 阈值（通道最大跌幅 ≥ Δ 判按下）；
2. `import yqrec` / `import yqrec.cli` **不得**把 PySide6 拖进来（打包与无 Qt 环境的前提）；
3. 有 Qt 时离屏构造主窗，确认 21 张键卡与标题正确（无 Qt 自动 skip）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from yqrec.instruments import load_instrument
from yqrec.types import KeyGrid, KeyPoint, KeyThreshold, PressEvent
from yqrec.vision.thresholds import thresholds_from_baseline


def test_thresholds_from_baseline_uses_max_channel_drop() -> None:
    # 显式把判据设成 max_drop（JSON 里现在默认是 weighted，但这条分支仍要保证可用）
    spec = replace(
        load_instrument("xlyq"), detection=replace(load_instrument("xlyq").detection, metric="max_drop")
    )
    baseline = np.zeros((spec.key_count, 3), dtype=np.float32)
    baseline[:, 0] = 200.0
    baseline[:, 1] = 60.0
    baseline[:, 2] = 30.0

    thresholds = thresholds_from_baseline(baseline, spec, 40)

    assert set(thresholds) == {area.code for area in spec.areas}
    threshold = thresholds["main"]
    assert isinstance(threshold, KeyThreshold)
    assert threshold.mode == "max_drop", "真机白盘→棕盘跌幅不均，必须用最大跌幅判按"
    assert (threshold.base_r, threshold.base_g, threshold.base_b) == (200, 60, 30)
    assert threshold.delta == 40

    channels = spec.areas[0].channels
    # R 跌 50（≥Δ）→ 按下；只跌 20（<Δ）→ 未按下
    assert threshold.accepts((150, 60, 30), channels) is True
    assert threshold.accepts((180, 60, 30), channels) is False
    # 任一通道跌够 Δ 都算按下（G 从 60 跌到 20，正好 Δ=40）
    assert threshold.accepts((200, 20, 30), channels) is True
    assert threshold.max_drop((150, 60, 30)) == 50


def test_thresholds_from_baseline_supports_chroma() -> None:
    """色偏判据：基线 (200,60,30)，`绿相对红` 变强 30 即判按下（不受整体亮度影响）。"""
    fwzs = load_instrument("fwzs")
    spec = replace(fwzs, detection=replace(fwzs.detection, metric="chroma"))
    baseline = np.zeros((spec.key_count, 3), dtype=np.float32)
    baseline[:, 0] = 200.0
    baseline[:, 1] = 60.0
    baseline[:, 2] = 30.0

    threshold = thresholds_from_baseline(baseline, spec, 25)["main"]
    assert threshold.mode == "chroma"
    assert (threshold.shift_from, threshold.shift_to) == (0, 1), "风物之诗是 R→G"

    # G-R 从 -140 变成 -110（+30）→ 按下
    assert threshold.chroma_shift((200, 90, 30)) == pytest.approx(30)
    assert threshold.accepts((200, 90, 30)) is True
    # 整体变暗 40（三通道同降）→ 色偏不变，不应判按下
    assert threshold.chroma_shift((160, 20, 30)) == pytest.approx(0)
    # 只涨 20 → 不够
    assert threshold.accepts((200, 80, 30)) is False



def test_gui_imports_are_lazy() -> None:
    """`import yqrec` / `import yqrec.cli` 之后 sys.modules 里不应有 PySide6。"""
    code = (
        "import sys, yqrec, yqrec.cli, yqrec.app\n"
        "print('PySide6' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", "PySide6 必须由 run_gui() 内部延迟导入"


def test_main_window_constructs_offscreen(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    # 窗口关闭时会写 settings.json（尺寸记忆），必须隔离，否则污染真实配置
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window.windowTitle() == "YQ_moTools"
    assert len(window._keys_panel._cards) == 21
    # M2a：录制按钮默认不可点、保存按钮默认不可点
    assert window._btn_record.text() == "录制谱面"
    assert window._btn_record.isEnabled() is False
    assert window._btn_save_score.isEnabled() is False
    # M2a.3：置顶按钮默认开启（图标非空）
    assert window._btn_ontop.isChecked() is True
    assert window._btn_ontop.icon().isNull() is False
    # M2a.2：窄长条竖排 —— 键位状态 / 预览 / 录制内容 三段纵向排列
    # M3a：每段外面包了一张 SectionCard（浅色主题靠 1px 边框分区）
    central = window.centralWidget()
    assert central.orientation() == Qt.Orientation.Vertical
    assert central.count() == 3
    from PySide6.QtWidgets import QFrame

    from yqrec.ui.preview import PreviewWidget

    for index in range(3):
        assert central.widget(index).objectName() == "SectionCard"
    assert central.widget(1).findChild(PreviewWidget) is window._preview, "预览必须在键位状态下方"
    assert isinstance(central.widget(1), QFrame)
    window.close()
    app.processEvents()


def test_always_on_top_default_and_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """窗口置顶：默认开、可切换、状态写进 settings.json 并在下次启动沿用。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))  # 隔离真实 %APPDATA%

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._btn_ontop.isChecked() is True, "置顶默认启用"
    assert bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert window._btn_ontop.toolTip().startswith("窗口置顶：开")

    window._btn_ontop.setChecked(False)
    assert not (window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), "取消后标志要摘掉"
    saved = json.loads((tmp_path / "YQ_moTools" / "settings.json").read_text(encoding="utf-8"))
    assert saved["ui"]["always_on_top"] is False
    window.close()

    again = MainWindow()
    assert again._btn_ontop.isChecked() is False, "重启要沿用上次关掉的状态"
    assert not (again.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    again.close()
    app.processEvents()


def test_save_as_bgi_and_load_it_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spec_fwzs
) -> None:
    """M2c：下拉框选 BetterGI → 保存成 BGI 曲谱 → 再读回来能试听。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.recording import RecordingBuffer
    from yqrec.types import PressEvent
    from yqrec.ui.main_window import MainWindow

    buf = RecordingBuffer(spec_fwzs)
    for index in range(12):
        buf.push_frame(index * 0.03)
    for frame, keys in ((0, ("A",)), (3, ("D", "F")), (9, ("G",))):
        for key in keys:
            buf.push_events([PressEvent(key=key, down=True, ts=frame * 0.03)])
    recording = buf.finish()

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._combo_format.count() == 3
    assert window._combo_format.itemData(0) == "tick"
    assert window._combo_format.itemData(1) == "bgi_midi"
    assert window._combo_format.itemData(2) == "bgi_yuanqin"

    # 文本框是保存的唯一来源：模拟"录制完成后文本框里已有谱面"
    window._record_result = recording
    window._playback_score = recording.score
    window._set_record_text(recording.tick_text)
    assert window._btn_save_score.isEnabled()
    window._combo_format.setCurrentIndex(1)
    target = tmp_path / "谱面_bgi_test.json"
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target), ""),
    )
    window._save_score()

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["type"] == "midi"
    assert saved["name"] == "谱面_bgi_test"
    assert saved["instrument"] == "风物之诗琴"
    assert bgi_sheet.parse_sheet(saved).note_count == recording.note_count

    # 再换 yuanqin 存一份：同一份录制，两种 BGI 类型都能出
    window._combo_format.setCurrentIndex(2)
    target_yq = tmp_path / "谱面_bgi_yq.json"
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target_yq), ""),
    )
    window._save_score()
    yq_saved = json.loads(target_yq.read_text(encoding="utf-8"))
    assert yq_saved["type"] == "yuanqin"
    assert bgi_sheet.parse_sheet(yq_saved).note_count == recording.note_count

    # 再读回来：自动识别为 BGI、填充文本、可试听
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(target), ""),
    )
    window._load_score_file()
    assert "BetterGI" in window._lbl_record_info.text()
    assert window._record_view.toPlainText()
    assert window._btn_play.isEnabled()

    window._worker = None
    window.close()
    app.processEvents()


def test_load_yuanqin_sheet(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """读取文件也能打开 `type: yuanqin`（五线谱制谱器）的 BGI 曲谱。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    sheet = Path(__file__).resolve().parent / "data" / "bgi" / "pugongying_yuanqin.json"
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(sheet), ""),
    )
    window._load_score_file()

    assert "459" in window._lbl_record_info.text()
    assert window._record_view.toPlainText()
    assert window._btn_play.isEnabled()
    assert window._btn_save_score.isEnabled(), "读取来的谱面也能另存"
    window.close()
    app.processEvents()


def test_window_size_is_remembered_across_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """缩放窗口后记住尺寸，重启沿用（用户要求）。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()

    window.resize(620, 700)  # 高度下限由布局 minimumSizeHint 决定（约 686）
    app.processEvents()
    window.close()
    app.processEvents()

    settings = json.loads((tmp_path / "YQ_moTools" / "settings.json").read_text(encoding="utf-8"))
    saved = (settings["ui"]["window_width"], settings["ui"]["window_height"])
    assert saved == (620, 700), saved

    # 模拟重启：新窗口应直接套用上次尺寸
    again = MainWindow()
    again.show()
    app.processEvents()
    assert (again.width(), again.height()) == saved
    again.close()
    app.processEvents()


def test_window_size_is_clamped_to_usable_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """记忆值非法/超屏时不能原样 resize：过小夹到最小，过大夹到屏幕内。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.config import default_config, save_config
    from yqrec.ui.main_window import MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH, MainWindow

    app = QApplication.instance() or QApplication([])

    tiny = default_config()
    tiny.ui.window_width, tiny.ui.window_height = 200, 200
    save_config(tiny)
    small_window = MainWindow()
    small_window.show()
    app.processEvents()
    assert small_window.width() >= MIN_WINDOW_WIDTH
    assert small_window.height() >= MIN_WINDOW_HEIGHT
    small_window.close()
    app.processEvents()

    huge = default_config()
    huge.ui.window_width, huge.ui.window_height = 5000, 5000
    save_config(huge)
    big_window = MainWindow()
    big_window.show()
    app.processEvents()
    screen = big_window.screen()
    if screen is not None:
        available = screen.availableGeometry()
        assert big_window.width() <= available.width()
        assert big_window.height() <= available.height()
    big_window.close()
    app.processEvents()
def test_suspicious_baseline_flags_pressed_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """学基线时若某些键正被按着，要能提示出来（否则后续永远检不到那些键）。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._spec = load_instrument("fwzs")

    idle = np.tile([246, 242, 231], (window._spec.key_count, 1)).astype(np.float32)
    window._baseline = idle.copy()
    assert window._suspicious_baseline_keys() == [], "全部松开时不该报警"

    # +3 / 1 / 2 / 5 对应的键是 E / A / S / G
    contaminated = idle.copy()
    for index in (2, 7, 8, 11):
        contaminated[index] = (163, 228, 206)
    window._baseline = contaminated
    flagged = set(window._suspicious_baseline_keys())
    assert flagged == {"E", "A", "S", "G"}, flagged

    window.close()
    app.processEvents()
def test_edit_panel_is_segmented_and_stays_inside_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """「手动点选」的浮层：三段式横排（拖动/新增/删除），缩窗后仍完整可见。

    M3a 起模式切换从"两个 checkable 按钮纵排"改成"三段式分段控件"，
    语义完全对应 `EDIT_MODE_DRAG/ADD/DELETE`，只是排列方式变了。
    """
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()

    window._build_edit_panel()
    panel = window._edit_panel
    assert panel is not None

    segments = (window._btn_edit_drag, window._btn_edit_add, window._btn_edit_delete)
    assert all(s is not None for s in segments)
    # 三段横排：同一行、x 递增；默认选中「拖动」
    assert segments[0].y() == segments[1].y() == segments[2].y()
    assert segments[0].x() < segments[1].x() < segments[2].x()
    assert segments[0].isChecked() and not segments[1].isChecked()
    # 互斥：切到「新增」后其它两段取消
    window._set_edit_mode("add")
    assert segments[1].isChecked() and not segments[0].isChecked()
    assert panel.width() <= window.width()

    def _inside() -> bool:
        rect = panel.geometry()
        return (
            rect.left() >= 0
            and rect.top() >= 0
            and rect.right() < window.width()
            and rect.bottom() < window.height()
        )

    assert _inside(), panel.geometry()

    # 缩到最小尺寸（受 minimumSizeHint 限制）后依然完整
    window.resize(360, 500)
    app.processEvents()
    assert _inside(), f"{window.width()}x{window.height()} -> {panel.geometry()}"

    window.close()
    app.processEvents()
def test_any_text_in_box_can_be_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spec_fwzs
) -> None:
    """文本框有值就能存：手输 / 读取 / 录制一视同仁，无需先录制。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.formats import bgi_sheet
    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert not window._btn_save_score.isEnabled(), "空文本框不能存"

    # 用户手打（不是录制来的，_record_result 仍为 None）
    window._record_view.setPlainText("A500F")
    assert window._record_result is None
    assert window._btn_save_score.isEnabled()
    assert window._playback_score is not None, "文本改动后试听谱面要跟着重建"

    target = tmp_path / "手输_tick.json"
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target), ""),
    )
    window._save_score()
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["tick"] == {"unit": "ms", "text": "A500F"}
    assert record["capture"]["duration_ms"] == 500

    # 同一段文本换 BGI midi 再存一份
    window._combo_format.setCurrentIndex(1)
    target_bgi = tmp_path / "手输_midi.json"
    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target_bgi), ""),
    )
    window._save_score()
    sheet = json.loads(target_bgi.read_text(encoding="utf-8"))
    assert sheet["type"] == "midi"
    assert bgi_sheet.parse_sheet(sheet).note_count == 2

    # 清空 → 按钮回到禁用
    window._record_view.setPlainText("")
    assert not window._btn_save_score.isEnabled()
    assert window._playback_score is None

    window.close()
    app.processEvents()


def test_score_dialog_dir_remembers_last_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """保存/读取对话框记住上次目录，并跨重启生效（存 settings.json）。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    first = window._score_dialog_dir()
    assert Path(first).name == "YQ_moTools", "首次应落默认目录"

    custom = tmp_path / "我的谱面"
    custom.mkdir()
    window._remember_score_dir(custom)
    assert window._score_dialog_dir() == str(custom)

    saved = json.loads((tmp_path / "YQ_moTools" / "settings.json").read_text(encoding="utf-8"))
    assert saved["paths"]["last_score_dir"] == str(custom)
    window.close()

    again = MainWindow()
    assert again._score_dialog_dir() == str(custom), "重启要沿用上次目录"

    shutil.rmtree(custom)  # 目录被删/移走后不能死在坏路径上
    assert again._score_dialog_dir() != str(custom)
    again.close()
    app.processEvents()


def test_load_json_fills_text_and_playback_highlights(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spec_fwzs
) -> None:
    """M2b：读取 JSON → 文本框显示 → 试听启用 → 高亮跟随到对应和弦。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.recording import RecordingBuffer
    from yqrec.scorefile import save_record
    from yqrec.types import PressEvent
    from yqrec.ui.main_window import MainWindow

    buf = RecordingBuffer(spec_fwzs)
    for index in range(12):
        buf.push_frame(index * 0.03)
    for frame, keys in ((0, ("A",)), (3, ("D", "F")), (9, ("G",))):
        for key in keys:
            buf.push_events([PressEvent(key=key, down=True, ts=frame * 0.03)])
    recording = buf.finish()
    path = save_record(tmp_path / "谱面_tick_test.json", recording, spec_fwzs)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._btn_play.isEnabled() is False, "没有谱面时不能试听"

    monkeypatch.setattr(
        "yqrec.ui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(path), ""),
    )
    window._load_score_file()

    assert window._record_view.toPlainText() == recording.tick_text
    assert "已读取" in window._lbl_record_info.text()
    assert window._btn_play.isEnabled(), "读取后预览未开 → 应可试听"

    # 模拟播放到第 2 个和弦：高亮应正好落在 "DF" 上
    # （M2q.9 起"是否在试听"看 _play_mode，播放/暂停由 _paused 区分）
    window._play_mode = True
    window._paused = False
    window._highlight_note(1)
    selections = window._record_view.extraSelections()
    assert len(selections) == 1
    assert selections[0].cursor.selectedText() == "DF"

    window._play_mode = False
    window.close()
    app.processEvents()


def test_playback_controller_maps_chords_to_token_order(
    monkeypatch: pytest.MonkeyPatch, spec_fwzs
) -> None:
    """Player 回调用的是 events 下标（含休止），控制器要换算成和弦序号。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from yqrec.recording import RecordingBuffer
    from yqrec.types import PressEvent
    from yqrec.ui.playback import PlaybackController

    QApplication.instance() or QApplication([])
    buf = RecordingBuffer(spec_fwzs)
    for index in range(12):
        buf.push_frame(index * 0.03)
    for frame in (0, 3, 6, 9):
        buf.push_events([PressEvent(key="A", down=True, ts=frame * 0.03)])
    recording = buf.finish()

    controller = PlaybackController(audio_enabled=False)
    controller.load(recording.score, spec_fwzs)
    fired: list[int] = []
    controller.note_played.connect(fired.append)
    for event_index in range(len(recording.score.events)):
        controller._emit_note(event_index)  # noqa: SLF001 - 直接验证换算

    assert fired == list(range(recording.note_count)), fired
    controller.close()


def test_record_buttons_follow_baseline_and_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spec_fwzs
) -> None:
    """录制链路（不碰真捕获）：按钮启用条件 + 停止后出谱 + 保存按钮可用。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    # 同上：closeEvent 会写窗口尺寸，隔离 APPDATA
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.recording import RecordingBuffer
    from yqrec.ui.main_window import MainWindow

    buf = RecordingBuffer(spec_fwzs)
    for index in range(5):
        buf.push_frame(index * 0.016)
    buf.push_events([PressEvent(key="A", down=True, ts=0.0)])
    buf.push_events([PressEvent(key="F", down=True, ts=0.048)])
    recording = buf.finish()

    class _FakeWorker:
        def start_recording(self) -> bool:
            return True

        def stop_recording(self):
            return recording

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._spec = spec_fwzs
    window._grid = KeyGrid(
        points=tuple(KeyPoint(u=0.5, v=0.5, r=0.02) for _ in range(spec_fwzs.key_count)),
        key_count=spec_fwzs.key_count,
    )
    window._worker = _FakeWorker()

    window._refresh_record_enabled()
    assert not window._btn_record.isEnabled(), "没学基线不能录"

    window._baseline = np.zeros((spec_fwzs.key_count, 3), dtype=np.float32)
    window._refresh_record_enabled()
    assert window._btn_record.isEnabled(), "基线 + 预览就绪后应可录制"

    window._start_recording()
    assert window._recording is True
    assert window._btn_record.text() == "停止录制"
    assert window._record_view.isReadOnly(), "录制中录入框必须只读，避免手改被覆盖"
    assert window._combo_instrument.isEnabled() is False

    window._finalize_recording()
    assert window._recording is False
    assert not window._record_view.isReadOnly(), "停止录制后应能继续编辑"
    assert window._combo_instrument.isEnabled() is True
    assert window._btn_record.text() == "录制谱面"
    assert window._record_view.toPlainText() == "A48F"  # M2f：间隔记毫秒（3 帧 × 16ms）
    assert window._btn_save_score.isEnabled(), "录到音后应能保存"

    window._worker = None  # 替身没有 stop/wait，关窗前摘掉
    window.close()
    app.processEvents()


def test_thresholds_from_baseline_weighted_is_the_default() -> None:
    """v1.20 默认判据：加权（只算变暗方向）+ 逐通道死区。"""
    spec = load_instrument("xlyq")
    baseline = np.zeros((spec.key_count, 3), dtype=np.float32)
    baseline[:, 0] = 200.0
    baseline[:, 1] = 200.0
    baseline[:, 2] = 200.0

    threshold = thresholds_from_baseline(baseline, spec, 40)["main"]
    assert threshold.mode == "weighted"
    assert sum(threshold.weights) == pytest.approx(1.0)
    assert threshold.tolerance == 8

    # 三通道各跌 60、死区 8 → 得分 52 ≥ 40
    assert threshold.weighted_score((140, 140, 140)) == pytest.approx(52.0)
    assert threshold.accepts((140, 140, 140)) is True
    # 只跌到死区以内（各跌 6）→ 得分 0，不判按下
    assert threshold.weighted_score((194, 194, 194)) == pytest.approx(0.0)
    # 变亮（背景变亮）不计入"变暗"方向
    assert threshold.weighted_score((230, 230, 230)) == pytest.approx(0.0)


def test_thresholds_from_baseline_weighted_is_the_default() -> None:
    """v1.20 默认判据：加权（只算变暗方向）+ 逐通道死区。"""
    spec = load_instrument("xlyq")
    baseline = np.zeros((spec.key_count, 3), dtype=np.float32)
    baseline[:, 0] = 200.0
    baseline[:, 1] = 200.0
    baseline[:, 2] = 200.0

    threshold = thresholds_from_baseline(baseline, spec, 40)["main"]
    assert threshold.mode == "weighted"
    assert sum(threshold.weights) == pytest.approx(1.0)
    assert threshold.tolerance == 8

    # 三通道各跌 60、死区 8 → 得分 52 ≥ 40
    assert threshold.weighted_score((140, 140, 140)) == pytest.approx(52.0)
    assert threshold.accepts((140, 140, 140)) is True
    # 只跌到死区以内（各跌 6）→ 得分 0，不判按下
    assert threshold.weighted_score((194, 194, 194)) == pytest.approx(0.0)
    # 变亮（背景变亮）不计入"变暗"方向
    assert threshold.weighted_score((230, 230, 230)) == pytest.approx(0.0)


def test_record_click_warmup_gated_by_global_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """record_click（instruments.json 全局 int 模式）门控：0 不注入 / 无区域跳过 / 2 点击。

    永远不触发真实 SendInput —— inputsim 函数被替身接管，只记录调用。
    """
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui import main_window

    calls: list[tuple[int, int]] = []

    def fake_click(x: int, y: int, **_kwargs) -> bool:
        calls.append((int(x), int(y)))
        return True

    monkeypatch.setattr(main_window.inputsim, "left_click", fake_click)

    app = QApplication.instance() or QApplication([])
    window = main_window.MainWindow()
    try:
        # 1) 模式 0（禁用）→ 即使有区域也不注入
        monkeypatch.setattr(main_window, "load_record_click", lambda: 0)
        window._region = (100, 200, 640, 360)
        window._record_click_warmup()
        assert calls == []

        # 2) 模式 2 但还没框选区域 → 跳过
        monkeypatch.setattr(main_window, "load_record_click", lambda: 2)
        window._region = None
        window._record_click_warmup()
        assert calls == []

        # 3) 模式 2 + 已框选 → 点在区域中心（左+宽/2, 上+高/2）
        window._region = (100, 200, 640, 360)
        window._record_click_warmup()
        assert calls == [(100 + 320, 200 + 180)]
    finally:
        window.close()
        app.processEvents()


def test_record_click_focus_space_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """模式 1（激活窗口+空格）：激活成功才发空格 / 失败坚决不发 / 命中自己不发。"""
    pytest.importorskip("PySide6.QtWidgets", reason="未安装 PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from yqrec.ui import main_window

    app = QApplication.instance() or QApplication([])
    window = main_window.MainWindow()
    try:
        monkeypatch.setattr(main_window, "load_record_click", lambda: 1)
        window._region = (100, 200, 640, 360)

        # a) 激活成功 → 空格发出
        monkeypatch.setattr(main_window.inputsim, "window_at", lambda x, y: 0x1234)
        monkeypatch.setattr(main_window.inputsim, "is_own_window", lambda hwnd: False)
        monkeypatch.setattr(main_window.inputsim, "activate_window", lambda hwnd: True)
        pressed: list[int] = []
        monkeypatch.setattr(main_window.inputsim, "press_space", lambda **kw: pressed.append(1) or True)
        window._record_click_warmup()
        assert pressed == [1]

        # b) 激活失败 → 坚决不发空格（防止空格落回本程序误停录制）
        monkeypatch.setattr(main_window.inputsim, "activate_window", lambda hwnd: False)
        pressed.clear()
        window._record_click_warmup()
        assert pressed == []

        # c) 区域中心命中本程序窗口 → 不激活不发
        monkeypatch.setattr(main_window.inputsim, "is_own_window", lambda hwnd: True)
        pressed.clear()
        window._record_click_warmup()
        assert pressed == []
    finally:
        window.close()
        app.processEvents()
