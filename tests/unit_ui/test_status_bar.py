"""状态栏：把"处理帧率"和"源帧率"分开显示（M2q.5 / M3a 视觉重排）。

用户看到 FPS≈30 时，必须先分清是「画面本身只有 30 帧」还是「程序在丢帧」：
- `fps` = 我们处理了多少帧/秒；
- `src_ms` / `src_fps` = 后端时间戳给的真实源帧间隔。

M3a 起排版改成「后端徽标 + 主数值 + 右侧单条消息区」：
| 旧 | 新 |
|---|---|
| `后端: dxgi | FPS: 59.8 | 源: 59.9fps(16.7ms) | 单帧: 0.6ms | 帧: 500` | `[DXGI] 59.8 fps · 源 59.9 · 单帧 0.6ms · 500 帧` |
| `statusBar().showMessage(...)` | `_lbl_toast.text()`（与错误提示同一位置） |

**语义一个没少**：源帧率、单帧耗时、帧数都还在，只是分隔符与标签词变了。
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
    win.show()  # 未显示的窗口 resize 不会派发 resizeEvent（状态栏密度就刷不到）
    win.resize(700, 900)  # 宽窗：状态栏走"全量"档（≥620）
    app.processEvents()
    return win, app


def test_status_shows_source_frame_rate(window) -> None:
    win, app = window
    win._on_status(
        {"backend": "dxgi", "fps": 59.8, "src_ms": 16.7, "src_fps": 59.9, "ms": 0.6, "frames": 500}
    )
    assert win._lbl_backend.text() == "DXGI"
    text = win._lbl_status.text()
    assert "59.8 fps" in text
    assert "源 59.9" in text, text
    assert "单帧 0.6ms" in text, text
    assert "500 帧" in text, text
    win.close()
    app.processEvents()


def test_status_bar_drops_details_when_narrow(window) -> None:
    """窄窗按宽度优先级丢弃次要数值，但后端徽标与 fps 永远保留。"""
    win, app = window
    win.resize(520, 900)
    app.processEvents()
    win._on_status(
        {"backend": "dxgi", "fps": 59.8, "src_ms": 16.7, "src_fps": 59.9, "ms": 0.6, "frames": 500}
    )
    assert win._lbl_backend.text() == "DXGI"
    assert win._lbl_status.text() == "59.8 fps", win._lbl_status.text()
    win.close()
    app.processEvents()


def test_source_limited_hint_when_both_are_30(window) -> None:
    """源 30fps + 处理 30fps → 提示"是画面帧率上限，不是丢帧"。"""
    win, app = window
    assert win._src_limited_hinted is False
    win._on_status(
        {"backend": "dxgi", "fps": 30.1, "src_ms": 33.3, "src_fps": 30.0, "ms": 0.5, "frames": 200}
    )
    assert win._src_limited_hinted is True
    assert "源画面只有 30 帧" in win._lbl_toast.text(), win._lbl_toast.text()
    win.close()
    app.processEvents()


def test_backend_fallback_marks_the_chip(window) -> None:
    """用户选了 dxgi 却跑在 gdi 上 → 徽标要变琥珀并写「回退」。"""
    win, app = window
    win._requested_backend = "dxgi"
    win._on_status(
        {"backend": "gdi", "fps": 55.0, "src_ms": 16.7, "src_fps": 59.9, "ms": 0.8, "frames": 100}
    )
    assert win._lbl_backend.text() == "GDI 回退"
    assert win._lbl_backend.property("tone") == "warn"
    win.close()
    app.processEvents()


def test_no_source_hint_when_source_is_fast(window) -> None:
    """源 60fps 但只处理 30fps → 不能误报成"源限制"（那才是我们在丢帧）。"""
    win, app = window
    win._on_status(
        {"backend": "dxgi", "fps": 30.0, "src_ms": 16.7, "src_fps": 59.9, "ms": 0.5, "frames": 200}
    )
    assert win._src_limited_hinted is False
    win.close()
    app.processEvents()
