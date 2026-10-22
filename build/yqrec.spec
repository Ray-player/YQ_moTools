# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 主交付（dist/YQ_moTools/YQ_moTools.exe）。

用法（项目根）：
    .venv/Scripts/python.exe -m PyInstaller --noconfirm --clean build/yqrec.spec

体积控制要点：
- 打包 `assets/instruments.json` + 图标 + **`assets/audio`（试听要用的 21×n 个 mp3，约 5.5MB）**；
- 排除掉 WebEngine / Quick / 3D / Multimedia / Charts 等未用模块；
- `pygame` 从 v2 起**必须打包**（M2b 试听），不要再加回 EXCLUDES。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


def _find_project(start: Path) -> Path:
    """向上找含 `pyproject.toml` 的目录（不依赖 SPECPATH 的具体语义）。"""
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return start.resolve()


ROOT = Path(SPECPATH).resolve()  # build/ 目录（或 spec 所在目录）
PROJECT = _find_project(ROOT)  # 项目根
APP_NAME = "YQ_moTools"
"""产物名（exe 与 dist 目录同名）：Windows 上叫 `YQ_moTools.exe`。"""

ICON = PROJECT / "assets" / "icons" / "mo_motools.ico"
"""应用图标：由 `tools/make_icon.py` 从 `mo_icon.png` 生成（四角圆角透明，多尺寸）。"""
VERSION_FILE = PROJECT / "build" / "version_info.txt"

EXCLUDES = [
    # 本项目不用到的重型依赖（注意：pygame 是试听要用的，**不能**排除）
    "tkinter",
    "unittest",
    "pydoc",
    "pydoc_data",
    "test",
    # dxcam 的 winrt 后端（我们只用 backend="dxgi"；它依赖未安装的 winrt/windows-capture，
    # 静态分析会顺着 core/backend.py 的函数级 import 把它带进来，这里才是真正的剪除点）
    "dxcam.core.winrt_duplicator",
    # Qt 里没用到的大件（WebEngine 一个就上百 MB）
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
]

a = Analysis(
    [str(PROJECT / "src" / "yqrec" / "app.py")],
    pathex=[str(PROJECT / "src")],
    binaries=[],
    datas=[
        (str(PROJECT / "assets" / "instruments.json"), "assets"),
        (str(ICON), "assets/icons"),
        (str(PROJECT / "assets" / "audio"), "assets/audio"),
    ],
    # **dxcam / hdrcapture 都是延迟导入**（`capture/dxgi_backend._open`、`capture/hdr_backend._open`），
    # PyInstaller 静态分析看不到它们 → 不写这里就会缺包 → 对应后端不可用 →
    # 静默回退到 GDI（帧率骤降、短按被跳过）。M2q.4 实测踩过（dxcam）。
    #
    # dxcam 的子模块收法（2026-10-22 优化）：
    # - `backend="dxgi"` 运行时实际只加载 25 个模块（实测 sys.modules）；
    # - 唯一多余的是 `core.winrt_duplicator`：它只在 `backend="winrt"` 时**函数内**懒加载
    #   （还依赖未安装的 `winrt`/`windows-capture`）。注意：光在 hiddenimports 里过滤没用，
    #   PyInstaller 的静态分析会顺着 `core/backend.py` 里的函数级 import 再把它捞回来，
    #   所以还必须同时加进下面的 EXCLUDES 才真能剪掉；
    # - 保留 `collect_submodules`（dxcam 升版新增模块时不会漏）而不是硬编码清单
    #   —— 硬编码在 dxcam 升版后漏模块 = 又一次静默回退 GDI。
    #
    # 想进一步瘦身（comtypes 有 51 个子模块、运行时只用 22 个），正路是让 j-HDR 顶上主后端，
    # 再把 dxcam + comtypes 整块从包里去掉（hdrcapture 是单个 pyd，零第三方依赖）。
    hiddenimports=collect_submodules(
        "dxcam", filter=lambda name: not name.endswith("winrt_duplicator")
    )
    + ["comtypes", "comtypes.client", "comtypes.automation", "hdrcapture"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=str(VERSION_FILE),
)

# --- 体积瘦身：删掉用不到的重型二进制 / 资源（GUI 是纯 raster 控件，无 QML/多媒体）---
DROP_BINARIES = (
    "opencv_videoio_ffmpeg",  # 只做统计取色，不读视频（视频 I/O 只有 FileBackend 用）
    "opengl32sw",  # Qt 的软件 OpenGL 兜底，QWidget 光栅渲染用不到
    "Qt6Quick",  # QML 运行时（未使用 QML）
    "Qt6Qml",
    "Qt6Pdf",
    "Qt6VirtualKeyboard",
)
DROP_DATA_PREFIXES = ("pyside6/translations", "pyside6/qml")


def _norm(name: str) -> str:
    return name.replace("\\", "/").lower()


a.binaries = [item for item in a.binaries if not any(key in item[0] for key in DROP_BINARIES)]
a.datas = [
    item
    for item in a.datas
    if not _norm(item[0]).startswith(DROP_DATA_PREFIXES)
]

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
