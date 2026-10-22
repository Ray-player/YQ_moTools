"""UI 子包（P3/P4）：主窗、框选、标定、HUD、导出对话框。

注意：本子包内各模块在**模块级**导入 PySide6，因此只能由 `yqrec.app.run_gui()`
延迟导入，保证 `import yqrec` 在无 Qt 环境下仍然可用。
"""

from __future__ import annotations

__all__ = ["CaptureWorker", "KeysPanel", "MainWindow", "PreviewWidget", "RegionPicker"]
