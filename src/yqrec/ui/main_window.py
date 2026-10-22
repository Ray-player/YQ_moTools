"""主窗：工具栏 + 实时预览 + 21 键状态灯 + 状态栏（M1 MVP）。

设计要点：
- 所有捕获/采样/判按都在 `CaptureWorker` 线程，主线程只刷 UI；
- 捕获源**只有屏幕区域**（窗口模式已下线）：先「框选区域」→「自动检测键位 / 手动点选」→
  「学习基线」→「开始预览」→ 按键看灯；
- 「学习基线」在**没有任何键按下**时采 1.5s 求均值，用 `thresholds_from_baseline`
  生成 `max_drop` 阈值（通道最大跌幅 ≥ Δ 判按），灵敏度滑块可实时重算；
- 标定结果（区域 + 21 个键位 + 阈值）持久化到 `%APPDATA%\\YQ_moTools\\profiles\\default.json`，
  重启后直接点「开始预览」即可，无需重新标定；
- 「手动点选」进入**编辑模式**：圈可拖拽、可增删，保存退出后生效。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import inputsim
from ..capture.manager import CaptureManager, backend_availability, backend_label
from ..config import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    AppConfig,
    config_path,
    default_config,
    load_config,
    load_profile,
    save_config,
    save_profile,
)
from ..formats import bgi_sheet
from ..instruments import (
    assets_dir,
    instruments_mtime,
    load_instruments,
    load_record_click,
    load_sampling,
    update_sampling,
)
from ..logging import get_logger
from ..recording import Recording, tick_spans, tick_text_from_score
from ..scorefile import (
    DEFAULT_SCORE_FORMAT,
    load_record,
    record_default_dir,
    record_tick_text,
    save_record,
    save_record_text,
    score_from_record,
    suggested_filename,
)
from ..timeline.detector import DetectorConfig
from ..types import (
    InstrumentSpec,
    KeyGrid,
    KeyPoint,
    KeyThreshold,
    Profile,
    Rect,
    Score,
    ScreenTarget,
)
from ..vision.keygrid import (
    circle_map_note,
    correct_button_coordinates,
    detect_keys,
    detect_keys_color,
    keygrid_from_note_map,
    keygrid_from_points,
)
from ..vision.thresholds import chroma_axis, press_weights, thresholds_from_baseline
from . import theme
from .capture_worker import CaptureWorker, from_qimage, to_qimage
from .icons import media_icon, pin_icon, theme_toggle_icon
from .keys_panel import MAX_CHANNEL, KeysPanel
from .playback import PlaybackController
from .preview import EDIT_MODE_ADD, EDIT_MODE_DELETE, EDIT_MODE_DRAG, PreviewWidget
from .region_picker import RegionPicker
from .widgets import EditPanel, StatusDot, VLine, panel_shadow

_log = get_logger("ui.main_window")

KEYS_PER_ROW = 7
"""琴键每排 7 个（21 键=3 行，14 键=2 行）；仅用于判断"能否自动检测"。"""

PROFILE_NAME = "default"
"""标定持久化的配置档名（`%APPDATA%\\YQ_moTools\\profiles\\default.json`）。"""

BGI_MIDI_FORMAT = "bgi_midi"
BGI_YQ_FORMAT = "bgi_yuanqin"
"""保存格式下拉里两种 BetterGI 曲谱的标识（默认文件名会带这个段）。"""

BGI_FORMATS: dict[str, str] = {BGI_MIDI_FORMAT: "midi", BGI_YQ_FORMAT: "yuanqin"}
"""下拉标识 → BGI 的 `type` 字段。"""

RADIUS_MIN = 20
RADIUS_MAX_RING = 95
RADIUS_MAX_SQUARE = 150
"""「半径」滑条范围（百分之一 R）：环形 = 内环半径，方块 = 中心到四边的距离（半边长）。"""

RADIUS_TIP = (
    "采样半径（单位 R = 键半径）：\n"
    "· 环形：内环半径，即沿这条圆周等距取点（0.20R ~ 0.95R）\n"
    "· 方块：中心到四边的距离，方块始终居中（0.20R ~ 1.50R）\n"
    "松手后写回 assets/instruments.json，下次启动自动读取"
)

RADIUS_WRITE_MS = 400
"""半径滑条写回 JSON 的防抖（毫秒）。"""

ERROR_CLEAR_MS = 6000
SAVE_DEBOUNCE_MS = 500
"""灵敏度拖动时的落盘防抖（毫秒）。"""

# 首次启动的窗口尺寸/位置定义在 `config.py`（与 `UiConfig` 的默认值同源，避免两处不一致）

MIN_WINDOW_WIDTH = 520
MIN_WINDOW_HEIGHT = 560
"""窗口再小就放不下三段布局，缩放记忆时按这个兜底。"""

BASELINE_OUTLIER_SHIFT = 20
"""学完基线后，某键的色偏与中位数的差距超过这个值 → 提示"可能学的时候正被按下"。

实测：正常画面各键色偏离散 1~2；被按下的键会偏离 35 以上，很容易区分。
"""

WINDOW_SIZE_DEBOUNCE_MS = 400
"""拖动窗口时的落盘防抖：连续 resize 只在停手后写一次。"""

EDIT_PANEL_WIDTH = 144
"""编辑浮层宽度：三段式按钮（拖动/新增/删除）横排后需要的最小可用宽。"""

STATUS_WIDE_PX = 620
STATUS_MEDIUM_PX = 560
"""状态栏按宽度丢弃次要数值的两道阈值（永远保留后端徽标与 FPS）。"""

TOOL_BUTTON_HEIGHT = 26
"""工具栏按钮统一高度（比 QSS 的 min-height 24 再高 2px，点起来不局促）。"""


def _mmss(milliseconds: float) -> str:
    """毫秒 → `0:05`（录制区头部的紧凑时长；完整的秒数/fps 留在 tooltip）。"""
    total = max(0, int(round(milliseconds / 1000.0)))
    return f"{total // 60}:{total % 60:02d}"


class MainWindow(QMainWindow):
    """M1 主窗：实时显示"当前画面哪些琴键被按下"。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YQ_moTools")
        self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)

        self._instruments = load_instruments()
        self._sampling = load_sampling()
        """全局采样口径（内环半径 / 方块半边长 / 分段数）：所有乐器共用，启动时读一次。"""
        self._json_mtime = instruments_mtime()
        """上次读到的 instruments.json mtime，用来判断开发者是否刚改过配置。"""
        self._radius_timer: QTimer | None = None
        self._radius_writing = False
        # 默认选中 JSON 里的**第一个**乐器（不是写死某个 id）；profile 里存过则随后覆盖
        self._spec: InstrumentSpec = next(iter(self._instruments.values()))
        self._grid: KeyGrid | None = None
        self._thresholds: dict[str, KeyThreshold] | None = None
        self._baseline: np.ndarray | None = None
        self._channel_by_area: dict[str, str] = {}
        self._worker: CaptureWorker | None = None
        self._region: tuple[int, int, int, int] | None = None
        self._partial_points: list[tuple[float, float]] = []
        self._picker: RegionPicker | None = None
        self._error_timer: QTimer | None = None
        self._save_timer: QTimer | None = None
        self._low_fps_hinted = False
        self._src_limited_hinted = False
        self._requested_backend = "dxgi"
        self._suppress_save = False
        self._profile_delta: dict[str, int] = {}
        self._size_timer: QTimer | None = None
        self._ui_ready = False
        """布局与尺寸恢复完成前不要写盘（构造期的 resize 也会进 resizeEvent）。"""
        self._config: AppConfig = self._load_app_config()
        """应用设置（`%APPDATA%\\YQ_moTools\\settings.json`）：目前只存窗口置顶状态。"""

        # 录制态（M2）
        self._recording = False
        self._record_result: Recording | None = None
        self._last_backend = "-"
        self._last_status_info: dict[str, Any] | None = None

        # 试听态（M2b）
        self._playback = PlaybackController(self)
        self._playback_score: Score | None = None
        self._tick_spans: list[tuple[int, int]] = []
        self._play_mode = False
        """试听模式：控制条可见（含暂停状态）。"""
        self._paused = False
        """试听中处于暂停（只有暂停时快进/快退可用）。"""
        self._cursor = 0
        """当前高亮的和弦下标（与 `_tick_spans` 同序）。"""
        self._paused_at = 0
        """暂停那一刻的高亮位置；继续播放时若位置被移动过就要 seek。"""

        # 编辑态控件
        self._edit_panel: EditPanel | None = None
        self._btn_edit_drag: QPushButton | None = None
        self._btn_edit_add: QPushButton | None = None
        self._btn_edit_delete: QPushButton | None = None
        self._btn_add: QPushButton | None = None
        """兼容旧引用（= 新增段按钮）；保存退出与关闭逻辑沿用它判断浮层是否在。"""
        self._btn_delete: QPushButton | None = None
        self._lbl_edit_count: QLabel | None = None
        self._lbl_edit_hint: QLabel | None = None

        # 主题与状态栏宽度分档（都在 _build_ui 之前给默认值，避免首次绘制拿到 None）
        self._theme = theme.normalize(self._config.ui.theme)
        self._status_compact = 2
        """状态栏信息密度：2 = 全量 / 1 = fps+帧数 / 0 = 只有 fps（resizeEvent 里更新）。"""

        self._build_ui()
        self._restore_ui_options()
        self._restore_window_size()
        self._apply_theme(self._theme)
        self._apply_on_top_state(self._config.ui.always_on_top)
        self._playback.note_played.connect(self._highlight_note)
        self._playback.state_changed.connect(self._on_playback_state)
        self._load_instrument(self._spec.id)
        _log.info("捕获后端可用性: %s", backend_availability())
        self._load_profile()
        # 首次运行（没有 settings.json）时把默认配置**现在**就落盘。构造期全程都在
        # `show()` 之前 —— 若不这样做，唯一的首次写盘会发生在窗口可见之后：
        # `_ui_ready` 置位后的 moveEvent/resizeEvent → 400ms 防抖 → `_persist_window_size`
        # → mkdir %APPDATA%\YQ_moTools + 新建 settings.json（老用户值没变不会写）。
        # 新建文件/目录在实机上可能被杀软实时扫描 / 受控文件夹访问 / 漫游配置档拖慢，
        # 主线程在窗口可见后卡住 >5s，Windows 会把窗口判成"未响应"—— 表现恰恰是
        # 标题栏右上角关闭按钮被系统置灰（用户反馈的"首次运行且无 C 盘存档时更易出现"）。
        # 提前建好后，防抖落盘因"值没变"直接跳过，窗口可见后不再有首次写盘。
        if not config_path().exists():
            self._save_app_config()
        # M2q.2：不再定时轮询 instruments.json —— 只在启动、「开始预览」「学基线」和 F5 时按需重读
        self._shortcut_reload = QShortcut(QKeySequence("F5"), self)
        self._shortcut_reload.activated.connect(self._reload_instruments_by_hand)
        # Esc：编辑态等同「保存退出」（只在浮层存在时生效，平时不吞按键）
        self._shortcut_escape = QShortcut(QKeySequence("Escape"), self)
        self._shortcut_escape.activated.connect(self._on_escape)
        # 启动时把焦点给窗口本身：否则第一个按钮会带着 accent 焦点框出现，
        # 看起来像"主操作"（Tab 仍能正常走完一整圈）。
        QTimer.singleShot(0, self.setFocus)

    # -- 构建 UI ----------------------------------------------------------- #

    def _build_ui(self) -> None:
        """窄长条竖排布局（单显示器友好）：工具栏 → 键位状态 → 预览 → 录制内容。

        用户要求（M2a.2）：预览不再占左侧大图，改为放在「键位状态」下方，
        整个窗口收成一条竖条，方便贴着游戏窗口摆。

        视觉层（M3a，按 `design/ui-redesign/`）：三段各包一张 `SectionCard`，
        工具栏去掉 4 个文字标签（`后端`/`乐器`/`检测`/`半径`），腾出的宽度给半径滑条；
        按钮按"主操作 / 常用 / 工具 / 收尾"四档挂 objectName，**可用性（setEnabled）一行不改**。
        """
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        holder = QWidget(self)
        holder.setObjectName("ToolbarHolder")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(8, 6, 8, 6)
        rows.setSpacing(4)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()
        row3 = QHBoxLayout()
        for row in (row1, row2, row3):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)

        # -- 第 1 行：抓取源 -------------------------------------------------- #
        self._btn_region = self._tool_button("框选区域", "Secondary", width=76)
        self._btn_region.setToolTip("在全屏遮罩上拖出要识别的区域（可锁 16:9）")
        self._btn_region.clicked.connect(self._pick_region)

        # 16:9 从复选框改成开关片：`.isChecked()` 语义不变，两处调用点无需改动
        self._chk_aspect = QPushButton("16:9")
        self._chk_aspect.setObjectName("Toggle")
        self._chk_aspect.setCheckable(True)
        self._chk_aspect.setChecked(True)
        self._chk_aspect.setFixedSize(52, TOOL_BUTTON_HEIGHT)
        self._chk_aspect.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chk_aspect.setToolTip("勾选后框选时锁定 16:9 长宽比")

        self._lbl_region = QLabel("未选择")
        self._lbl_region.setObjectName("RegionChip")
        self._lbl_region.setToolTip("框选区域（完整坐标见提示）")
        theme.set_state(self._lbl_region, "empty", prop="state")

        self._combo_backend = QComboBox()
        self._combo_backend.setObjectName("Quiet")
        # 88 而不是 74：`j-HDR` 在 9pt 下量出来 60px，加右侧箭头要 ~78px，74 会把它裁掉。
        self._combo_backend.setFixedSize(88, TOOL_BUTTON_HEIGHT)
        self._combo_backend.setToolTip(
            "抓帧后端（选择会被记住，下次打开沿用）\n"
            "· j-HDR（默认）：FP16 取帧 + 按显示器 SDR 白电平归一；SDR 下与 dxgi 等价，"
            "开 HDR 时只有它颜色是对的；失败自动退 dxgi → gdi\n"
            "· dxgi：Desktop Duplication，不认 HDR（HDR 下画面会灰阶塌陷）\n"
            "· gdi：兼容备选，上面两个都打不开时兜底"
        )
        # **不提供「自动」** —— 原来的"自动"对屏幕区域其实固定走 gdi，名不副实且帧率低
        # （用户明确要求去掉）。真打不开时由 CaptureManager 按回退链自动回退。
        # 顺序即默认：第 0 项 = j-HDR（M3e 起默认），dxgi / gdi 依次备选。
        self._combo_backend.addItem("j-HDR", "hdr")
        self._combo_backend.addItem("dxgi", "dxgi")
        self._combo_backend.addItem("gdi", "gdi")
        # 恢复上次选择（settings.json 的 backend）；旧值/脏值一律回落到默认 j-HDR。
        _restored = self._combo_backend.findData(str(self._config.backend or "hdr"))
        self._combo_backend.setCurrentIndex(_restored if _restored >= 0 else 0)
        self._combo_backend.currentIndexChanged.connect(self._on_backend_changed)

        # 日/夜切换：图标由当前主题决定（日间画月亮，夜间画太阳）
        self._btn_theme = QPushButton()
        self._btn_theme.setObjectName("IconButton")
        self._btn_theme.setFixedSize(28, TOOL_BUTTON_HEIGHT)
        self._btn_theme.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_theme.setToolTip("切换 日间 / 夜间 主题")
        self._btn_theme.clicked.connect(self._toggle_theme)

        # 窗口置顶（图钉）：默认开，状态存 settings.json
        self._btn_ontop = QPushButton()
        self._btn_ontop.setObjectName("OnTop")
        self._btn_ontop.setCheckable(True)
        self._btn_ontop.setFixedSize(28, TOOL_BUTTON_HEIGHT)
        self._btn_ontop.setIconSize(QSize(16, 16))
        self._btn_ontop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_ontop.toggled.connect(self._toggle_on_top)

        # -- 第 2 行：乐器与标定 ---------------------------------------------- #
        self._combo_instrument = QComboBox()
        self._combo_instrument.setObjectName("Instrument")
        self._combo_instrument.setMinimumWidth(150)
        self._combo_instrument.setFixedHeight(TOOL_BUTTON_HEIGHT)
        for inst_id, spec in self._instruments.items():
            self._combo_instrument.addItem(f"{spec.name}（{spec.key_count} 键）", inst_id)
        self._combo_instrument.currentIndexChanged.connect(
            lambda _i: self._load_instrument(str(self._combo_instrument.currentData()))
        )

        self._dot_instrument = StatusDot(self)

        self._btn_detect = self._tool_button("自动检测", "Secondary", width=76)
        self._btn_detect.setToolTip("自动检测键位（颜色法，失败回退 Hough）")
        self._btn_detect.clicked.connect(self._auto_detect_keys)
        self._btn_manual = self._tool_button("手动点选", "Secondary", width=68)
        self._btn_manual.setToolTip("手动点选键位（拖拽 / 增删检测圈）")
        self._btn_manual.clicked.connect(self._start_edit)
        self._btn_reset = self._tool_button("重置", "Quiet", width=52)
        self._btn_reset.setToolTip("重置检测（清空键位 / 基线，重新标定）")
        self._btn_reset.clicked.connect(self._reset_detection)

        # -- 第 3 行：运行 ---------------------------------------------------- #
        self._btn_preview = self._tool_button("开始预览", "Primary", width=80)
        self._btn_preview.clicked.connect(self._toggle_preview)

        self._btn_baseline = self._tool_button("学习基线", "Secondary", width=76)
        self._btn_baseline.setEnabled(False)
        self._btn_baseline.setToolTip("松开所有琴键后点这里，采 1.5 秒作为未按下的基线")
        self._btn_baseline.clicked.connect(self._learn_baseline)

        self._btn_record = self._tool_button("录制谱面", "Secondary", width=76)
        self._btn_record.setEnabled(False)
        self._btn_record.setToolTip("需先「学习基线」并「开始预览」，才能开始录制")
        self._btn_record.clicked.connect(self._toggle_record)

        self._combo_sample = QComboBox()
        self._combo_sample.setObjectName("Quiet")
        self._combo_sample.setFixedSize(104, TOOL_BUTTON_HEIGHT)
        self._combo_sample.addItem("环形（内环）", "ring")
        self._combo_sample.addItem("方块（居中）", "square")
        self._combo_sample.setToolTip(
            "用哪一圈像素判按（点采样 + 加权判据）：\n"
            "· 环形：在内环圆周上等距取点\n"
            "· 方块：四边等距取点，始终居中\n"
            "「半径」滑条决定取样的半径，改完会写回 instruments.json"
        )
        self._combo_sample.currentIndexChanged.connect(self._on_sample_mode_changed)

        self._slider_radius = QSlider(Qt.Orientation.Horizontal)
        self._slider_radius.setObjectName("Radius")
        self._slider_radius.setRange(RADIUS_MIN, RADIUS_MAX_RING)
        self._slider_radius.setMinimumWidth(56)
        self._slider_radius.setToolTip(RADIUS_TIP)
        self._slider_radius.valueChanged.connect(self._on_radius_changed)
        self._slider_radius.sliderReleased.connect(self._commit_radius)
        self._lbl_radius = QLabel("0.55R")
        self._lbl_radius.setObjectName("ValueChip")
        self._lbl_radius.setMinimumWidth(44)
        self._lbl_radius.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        row1.addWidget(self._btn_region)
        row1.addWidget(self._chk_aspect)
        row1.addWidget(self._lbl_region, 1)
        row1.addWidget(self._combo_backend)
        row1.addWidget(self._btn_theme)
        row1.addWidget(self._btn_ontop)

        row2.addWidget(self._combo_instrument, 1)
        row2.addWidget(self._dot_instrument)
        row2.addWidget(self._btn_detect)
        row2.addWidget(self._btn_manual)
        row2.addWidget(self._btn_reset)

        row3.addWidget(self._btn_preview)
        row3.addWidget(self._btn_baseline)
        row3.addWidget(self._btn_record)
        row3.addWidget(self._combo_sample)
        row3.addWidget(self._slider_radius, 1)
        row3.addWidget(self._lbl_radius)

        rows.addLayout(row1)
        rows.addLayout(row2)
        rows.addLayout(row3)
        toolbar.addWidget(holder)

        # -- 三段区块卡 -------------------------------------------------------- #
        self._preview = PreviewWidget(self)
        self._preview.points_changed.connect(self._on_points_changed)
        self._preview.edit_hint.connect(self._flash_status)
        self._preview.selection_changed.connect(self._on_selection_changed)
        self._preview.background_clicked.connect(self._on_canvas_background_clicked)
        self._keys_panel = KeysPanel(self)

        keys_card, keys_body, self._keys_meta = self._make_section("键位状态", "7×3 · 21 键")
        keys_body.addWidget(self._keys_panel, 1)

        preview_card, preview_body, self._preview_meta = self._make_section("预览", "")
        preview_body.addWidget(self._preview, 1)

        record_card, record_body, self._lbl_record_info = self._make_section(
            "录制内容 · tick（毫秒间隔）", "未录制"
        )
        self._record_view = QPlainTextEdit()
        self._record_view.setObjectName("RecordView")
        self._record_view.setMinimumHeight(72)
        self._record_view.setPlaceholderText(
            "录制 / 读取 / 手输的谱面都在这里，保存与试听都以它为准…"
        )
        self._record_view.setToolTip("可直接编辑：保存与试听都以这里的内容为准")
        self._record_view.textChanged.connect(self._on_record_text_edited)
        record_body.addWidget(self._record_view, 1)
        record_body.addLayout(self._build_record_actions())

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(keys_card)
        splitter.addWidget(preview_card)
        splitter.addWidget(record_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([246, 330, 200])
        self.setCentralWidget(splitter)

        bar = QStatusBar(self)
        self._lbl_backend = QLabel("—")
        self._lbl_backend.setObjectName("BackendChip")
        self._lbl_backend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("StatusText")
        self._lbl_toast = QLabel("")
        self._lbl_toast.setObjectName("Toast")
        self._lbl_toast.setMaximumWidth(260)
        bar.addWidget(self._lbl_backend)
        bar.addWidget(self._lbl_status, 1)
        bar.addPermanentWidget(self._lbl_toast)
        bar.setSizeGripEnabled(False)  # 右下角的小三角会跟消息区抢位置
        self.setStatusBar(bar)
        self._reset_status_text()
        self._refresh_save_tooltip()

    def _tool_button(self, text: str, role: str, *, width: int) -> QPushButton:
        """工具栏按钮：统一高度 + 档位 objectName（`Secondary`/`Quiet`/`Primary`/`Danger`）。"""
        button = QPushButton(text)
        button.setObjectName(role)
        button.setFixedHeight(TOOL_BUTTON_HEIGHT)
        button.setMinimumWidth(width)
        return button

    def _make_section(self, title: str, meta: str = "") -> tuple[QFrame, QVBoxLayout, QLabel]:
        """一张区块卡：header（标题 + 右侧 meta）+ 内容容器。

        返回 `(卡片, 内容布局, meta 标签)`；调用方把真正的控件塞进内容布局。
        meta 标签留成属性，运行中要显示进度（如「录制中 · 12 个音 · 0:05」）。
        """
        card = QFrame(self)
        card.setObjectName("SectionCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(card)
        header.setObjectName("SectionHeader")
        header.setFixedHeight(28)
        row = QHBoxLayout(header)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(8)
        label = QLabel(title, header)
        label.setObjectName("SectionTitle")
        meta_label = QLabel(meta, header)
        meta_label.setObjectName("SectionMeta")
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(meta_label)

        body = QWidget(card)
        body.setObjectName("SectionBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        outer.addWidget(header)
        outer.addWidget(body, 1)
        return card, body_layout, meta_label

    def _build_record_actions(self) -> QHBoxLayout:
        """录制内容区底部操作行：格式 / 保存 / 打开 …… 试听。"""
        save_row = QHBoxLayout()
        save_row.setContentsMargins(8, 4, 8, 8)
        save_row.setSpacing(6)
        self._combo_format = QComboBox()
        self._combo_format.setObjectName("Quiet")
        self._combo_format.setFixedSize(124, TOOL_BUTTON_HEIGHT)
        self._combo_format.setToolTip(
            "保存成哪种谱面格式\n"
            "· tick：YQ_moTools 自己的毫秒间隔 JSON（A1000F = 隔 1000ms）\n"
            "· BetterGI midi：D/U 事件流（1 tick = 1ms）\n"
            "· BetterGI yuanqin：音符时值记号 A[8] / (ASD)[4] / @[2]"
        )
        self._combo_format.addItem("tick（默认）", DEFAULT_SCORE_FORMAT)
        self._combo_format.addItem("BGI · midi", BGI_MIDI_FORMAT)
        self._combo_format.addItem("BGI · yuanqin", BGI_YQ_FORMAT)
        self._combo_format.currentIndexChanged.connect(self._refresh_save_tooltip)

        self._btn_save_score = self._tool_button("保存", "Primary", width=52)
        self._btn_save_score.setEnabled(False)
        self._btn_save_score.setToolTip("按左侧选择的格式保存录制好的谱面")
        self._btn_save_score.clicked.connect(self._save_score)

        self._btn_load_score = self._tool_button("打开", "Secondary", width=52)
        self._btn_load_score.setToolTip("从磁盘选择谱面（YQ_moTools JSON 或 BetterGI 曲谱）")
        self._btn_load_score.clicked.connect(self._load_score_file)

        self._btn_play = self._tool_button("试听", "Secondary", width=64)
        self._btn_play.setEnabled(False)
        self._btn_play.setIconSize(QSize(14, 14))
        self._btn_play.setToolTip("预览停止且文本框里有谱面时可试听")
        self._btn_play.clicked.connect(self._toggle_playback)

        # 试听控制条（M2q.9）：进入试听模式后**原地替换**「试听」按钮
        self._play_bar = QWidget()
        self._play_bar.setObjectName("PlayBar")
        play_bar = QHBoxLayout(self._play_bar)
        play_bar.setContentsMargins(2, 2, 2, 2)
        play_bar.setSpacing(0)
        self._btn_rewind = self._make_media_button(
            "rewind", "快退一个和弦（按住连续后退，不发声）", repeat=True
        )
        self._btn_toggle_play = self._make_media_button("pause", "暂停（暂停后可快退/快进）")
        self._btn_forward = self._make_media_button(
            "forward", "快进一个和弦（按住连续前进，不发声）", repeat=True
        )
        self._btn_play_stop = QPushButton("停止")
        self._btn_play_stop.setObjectName("MediaStop")
        self._btn_play_stop.setToolTip("停止试听并回到「试听」入口")
        self._btn_play_stop.clicked.connect(self._stop_playback)
        for widget in (self._btn_rewind, self._btn_toggle_play, self._btn_forward):
            play_bar.addWidget(widget)
        play_bar.addWidget(VLine(self))
        play_bar.addWidget(self._btn_play_stop)
        self._btn_rewind.clicked.connect(lambda: self._step_cursor(-1))
        self._btn_forward.clicked.connect(lambda: self._step_cursor(1))
        self._btn_toggle_play.clicked.connect(self._toggle_pause)
        self._play_bar.hide()

        save_row.addWidget(self._combo_format)
        save_row.addWidget(self._btn_save_score)
        save_row.addWidget(self._btn_load_score)
        save_row.addStretch(1)
        save_row.addWidget(self._btn_play)
        save_row.addWidget(self._play_bar)
        return save_row

    # -- 主题 -------------------------------------------------------------- #

    def _apply_theme(self, name: str | None = None) -> None:
        """套用主题：颜色全部来自 `theme.py` 的令牌，这里不再出现任何 `#hex`。"""
        self._theme = theme.apply(self, name or theme.current())
        self._config.ui.theme = self._theme
        self._sync_title_bar()
        self._repaint_theme_dependents()

    def _sync_title_bar(self) -> None:
        """让 Windows 原生标题栏跟随 App 主题。

        不处理的话：日间内容配黑色系统标题栏，整窗像"半夜间"（用户会直接说"默认夜间"）。
        `DWMWA_USE_IMMERSIVE_DARK_MODE` 在 Win10 1903+ 是 20、更早是 19，两个都试一次；
        任何异常都静默（非 Windows / 精简系统 / 无 DWM 时不该影响启动）。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            if not hwnd:
                return
            dark = 1 if theme.is_dark() else 0
            value = ctypes.c_int(dark)
            dwm = ctypes.windll.dwmapi
            for attribute in (20, 19):
                if dwm.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                ) == 0:
                    break
        except Exception as exc:  # 标题栏只是观感，失败不影响任何功能
            _log.debug("设置标题栏主题失败: %s", exc)

    def _repaint_theme_dependents(self) -> None:
        """QSS 管不到的绘制层要手动通知（图标 / 键位卡状态 / 预览叠加色）。"""
        media_colors = theme.media_colors()
        pin_colors = (theme.token("accent-text"), theme.token("text-secondary"))
        """图钉激活色用 `accent-text` 而不是 `accent-fg`：OnTop 勾选态是 accent-soft
        淡染底（不是 accent 实心），白色图标在日间主题的浅蓝底上会直接消失。"""
        self._btn_theme.setIcon(theme_toggle_icon())
        self._btn_ontop.setIcon(pin_icon(self._btn_ontop.isChecked(), colors=pin_colors))
        self._btn_play.setIcon(media_icon("play", size=14, colors=media_colors))
        self._btn_toggle_play.setIcon(
            media_icon("pause" if self._play_mode and not self._paused else "play",
                       size=18, colors=media_colors)
        )
        self._btn_rewind.setIcon(media_icon("rewind", size=18, colors=media_colors))
        self._btn_forward.setIcon(media_icon("forward", size=18, colors=media_colors))
        self._keys_panel.refresh_style()
        self._preview.refresh_theme()
        self._update_instrument_dot()

    def _toggle_theme(self) -> None:
        """日 ⇄ 夜：切换后立刻落盘（复用现有防抖写盘链路）。"""
        target = theme.LIGHT if theme.is_dark() else theme.DARK
        self._apply_theme(target)
        self._save_app_config()
        self._toast("已切换到" + ("日间" if target == theme.LIGHT else "夜间") + "主题")

    # -- 视觉层级：一行只允许一个 accent 填充按钮 --------------------------- #

    def _refresh_action_emphasis(self) -> None:
        """让「下一步」永远只有一个：按运行态给三个按钮换档位。

        只换 objectName（= 换样式），**绝不碰 `setEnabled`** —— 可用性矩阵与识别
        流程强绑定，档位只表达"哪个是主操作"，不禁用任何东西。
        """
        if self._play_mode:
            # 试听中：主操作在控制条里（播放/暂停），工具栏整体降级
            for button in (self._btn_record, self._btn_preview, self._btn_baseline):
                self._set_role(button, "Secondary")
            return
        if self._recording:
            # 录制中：两个"停止"都是收尾动作 → 都是 danger tonal
            self._set_role(self._btn_record, "Danger")
            self._set_role(self._btn_preview, "Danger")
            self._set_role(self._btn_baseline, "Quiet")
            return
        previewing = self._worker is not None
        if previewing and self._baseline is not None:
            self._set_role(self._btn_record, "Primary")
            self._set_role(self._btn_preview, "Danger")
            self._set_role(self._btn_baseline, "Secondary")
        elif previewing:
            self._set_role(self._btn_record, "Secondary")
            self._set_role(self._btn_preview, "Danger")
            self._set_role(self._btn_baseline, "Primary")
        else:
            self._set_role(self._btn_record, "Secondary")
            self._set_role(self._btn_preview, "Primary")
            self._set_role(self._btn_baseline, "Secondary")

    def _set_role(self, button: QPushButton, role: str) -> None:
        """换 objectName 并重算样式（QSS 用 objectName 挂钩）。"""
        if button.objectName() == role:
            return
        button.setObjectName(role)
        theme.repolish(button)

    def _update_instrument_dot(self) -> None:
        """乐器标定状态点：已标定+已学基线 / 已标定未学基线 / 未标定。"""
        if self._grid is None:
            kind, tip = "none", "该乐器参数未标定，只能用当前基线"
        elif self._baseline is None:
            kind, tip = "warn", "已标定，还需「学习基线」"
        else:
            kind, tip = "ok", "已标定，基线已学"
        self._dot_instrument.set_kind(kind)
        self._dot_instrument.setToolTip(tip)

    # -- 状态栏 ------------------------------------------------------------ #

    def _reset_status_text(self) -> None:
        self._last_status_info = None
        self._lbl_backend.setText("—")
        theme.set_state(self._lbl_backend, "", prop="tone")
        self._lbl_status.setText(self._status_text(0.0, 0.0, 0.0, 0))

    def _refresh_status_compact(self) -> None:
        """窗口宽度变了就重算状态栏的信息密度（不每帧算，只在 resize 时算一次）。"""
        width = self.width()
        compact = 2 if width >= STATUS_WIDE_PX else (1 if width >= STATUS_MEDIUM_PX else 0)
        if compact == self._status_compact:
            return
        self._status_compact = compact
        if self._last_status_info is not None:
            self._on_status(self._last_status_info)

    def _status_text(self, fps: float, src_fps: float, ms: float, frames: int) -> str:
        """按宽度优先级拼状态文字：窄窗只留 fps，宽窗给全（永远保留 fps）。"""
        if fps <= 0:
            return f"等待新帧… · {frames} 帧" if self._status_compact >= 2 else "等待新帧…"
        if self._status_compact >= 2:
            return f"{fps:.1f} fps · 源 {src_fps:.1f} · 单帧 {ms:.1f}ms · {frames} 帧"
        if self._status_compact == 1:
            return f"{fps:.1f} fps · {frames} 帧"
        return f"{fps:.1f} fps"

    def _toast(self, message: str, tone: str = "info") -> None:
        """统一消息区：`info` / `warn` / `error` 三种语气，同一位置，超时清空。"""
        self._lbl_toast.setProperty("tone", tone)
        theme.repolish(self._lbl_toast)
        self._lbl_toast.setText(message)
        self._lbl_toast.setToolTip(message)
        if self._error_timer is None:
            self._error_timer = QTimer(self)
            self._error_timer.setSingleShot(True)
            self._error_timer.timeout.connect(lambda: self._lbl_toast.setText(""))
        self._error_timer.stop()
        if not message:
            return
        self._error_timer.start(ERROR_CLEAR_MS if tone != "error" else ERROR_CLEAR_MS + 2000)

    def _delta(self) -> int:
        """灵敏度 Δ 固定在乐器 JSON 的 `delta` 字段里，界面不再暴露滑条。"""
        return int(self._spec.detection.default_delta)

    def _sample_mode(self) -> str:
        return str(self._combo_sample.currentData() or "ring")

    def _is_ring_mode(self) -> bool:
        return self._sample_mode().startswith("ring")

    def _radius_value(self) -> int:
        """「半径」滑条当前值（百分之一 R）。"""
        return int(self._slider_radius.value())

    def _effective_inner_ratio(self) -> float:
        """环形模式的内环半径（× R，点采样就落在这条圆周上）；方块模式恒 0。"""
        if not self._is_ring_mode():
            return 0.0
        return self._sampling.ring_inner_ratio

    def _effective_square_side(self) -> float:
        """方块边长（× 键半径）= 2 × 中心到四边的距离；方块始终居中（无偏移）。"""
        return float(self._sampling.square_side_ratio)

    def _sync_preview_sampling(self) -> None:
        """把当前采样口径推给预览：检测圈一显示就带小红点，滑条一动点就跟着动。"""
        self._preview.set_sampling(
            inner_ratio=self._effective_inner_ratio(),
            square_side=self._effective_square_side(),
            ring_points=int(self._sampling.ring_points),
            square_points=int(self._sampling.square_points),
        )

    def _sync_sampler_options(self) -> None:
        """把采样口径推给预览 + worker（worker 会触发 sampler 重建）。"""
        self._sync_preview_sampling()
        if self._worker is None:
            return
        try:
            self._worker.set_sampler_options(
                inner_ratio=self._effective_inner_ratio(),
                square_side=self._effective_square_side(),
                offset_y=0.0,  # 方块恒居中（v1.20 去掉了纵向偏移）
                point_sampling=True,  # v1.20：一律用分段点采样
                ring_points=int(self._sampling.ring_points),
                square_points=int(self._sampling.square_points),
                patch_ratio=float(self._sampling.point_patch_ratio),
            )
        except Exception as exc:  # 采样器重建失败不该打死主窗
            _log.warning("更新采样口径失败: %s", exc)

    def _sync_radius_slider(self) -> None:
        """把滑条同步成"当前模式在 instruments.json 里的半径"，并按模式切量程。

        环形 = 内环半径（0.95R 上限），方块 = 中心到四边的距离（1.50R 上限）。
        """
        ring = self._is_ring_mode()
        half_or_inner = (
            self._sampling.ring_inner_ratio if ring else self._sampling.square_side_ratio / 2.0
        )
        upper = RADIUS_MAX_RING if ring else RADIUS_MAX_SQUARE
        value = min(max(int(round(half_or_inner * 100)), RADIUS_MIN), upper)
        self._slider_radius.blockSignals(True)
        self._slider_radius.setRange(RADIUS_MIN, upper)
        self._slider_radius.setValue(value)
        self._slider_radius.blockSignals(False)
        self._update_radius_label()
        self._sync_preview_sampling()  # 检测圈上的小红点跟着半径走

    def _update_radius_label(self) -> None:
        value = self._radius_value() / 100.0
        self._lbl_radius.setText(f"{value:.2f}R")
        if self._is_ring_mode():
            self._lbl_radius.setToolTip(f"内环半径 {value:.2f}R（点在圆周上等距分布）")
        else:
            self._lbl_radius.setToolTip(
                f"中心到四边 {value:.2f}R（边长 {value * 2:.2f}R，方块居中）"
            )

    def _apply_radius_to_sampling(self) -> None:
        """把滑条值转成 sampling（只改内存，落盘另有防抖）。"""
        value = self._radius_value() / 100.0
        if self._is_ring_mode():
            self._sampling = self._sampling.with_ring_inner_ratio(value)
        else:
            self._sampling = self._sampling.with_square_half_ratio(value)

    def _on_radius_changed(self, _value: int = 0) -> None:
        """拖动中：立刻生效（预览/学习基线都用新半径），写盘走 400ms 防抖。"""
        self._apply_radius_to_sampling()
        self._update_radius_label()
        self._sync_sampler_options()
        self._apply_detection_spec(self._spec)
        if self._radius_timer is None:
            self._radius_timer = QTimer(self)
            self._radius_timer.setSingleShot(True)
            self._radius_timer.timeout.connect(self._write_radius)
        self._radius_timer.start(RADIUS_WRITE_MS)

    def _commit_radius(self) -> None:
        """松手立刻落盘（不用等防抖）。"""
        if self._radius_timer is not None:
            self._radius_timer.stop()
        self._write_radius()

    def _write_radius(self) -> None:
        """把当前模式的半径写回 `assets/instruments.json` 的 sampling 段。"""
        if self._radius_writing:
            return
        self._radius_writing = True
        try:
            if self._is_ring_mode():
                ok = update_sampling(ring_width_ratio=round(self._sampling.ring_width_ratio, 4))
                what = f"内环 {self._sampling.ring_inner_ratio:.2f}R"
            else:
                ok = update_sampling(square_side_ratio=round(self._sampling.square_side_ratio, 4))
                what = f"中心到四边 {self._sampling.square_side_ratio / 2:.2f}R"
        finally:
            self._radius_writing = False
        if not ok:
            return
        self._json_mtime = instruments_mtime()  # 自己写的，别再触发一次"配置已重载"提示
        try:
            self._sampling = load_sampling()  # 以文件为准，避免和手改值打架
        except Exception:  # 读不回来就沿用内存值
            pass
        hint = f"半径已保存（{what}）"
        if self._baseline is not None:
            hint += "，请重新「学习基线」"
        self._flash_status(hint)

    def _on_sample_mode_changed(self, _index: int = 0) -> None:
        self._config.ui.sample_mode = self._sample_mode()
        self._save_app_config()
        self._sync_radius_slider()
        self._sync_sampler_options()
        self._apply_detection_spec(self._spec)
        if self._baseline is not None:
            self._flash_status("检测方式已切换 —— 基线是按旧方式采的，请重新「学习基线」")

    def _refresh_sidebar_meta(self) -> None:
        """键位区右上角的口径说明 + 乐器标定状态点（标定/基线一变就同步）。"""
        if self._grid is None:
            self._keys_meta.setText("未标定")
        else:
            rows = max(1, self._grid.key_count // KEYS_PER_ROW)
            self._keys_meta.setText(f"{KEYS_PER_ROW}×{rows} · {self._grid.key_count} 键")
        self._update_instrument_dot()

    def _load_instrument(self, inst_id: str) -> None:
        spec = self._instruments.get(inst_id)
        if spec is None:
            return
        if spec.id == self._spec.id and self._grid is None and self._baseline is None:
            # 首次调用：只同步下拉与灯板
            self._spec = spec
            self._keys_panel.set_spec(spec)
            self._apply_detection_spec(spec)
            self._refresh_sidebar_meta()
            index = self._combo_instrument.findData(inst_id)
            if index >= 0 and self._combo_instrument.currentIndex() != index:
                self._combo_instrument.blockSignals(True)
                self._combo_instrument.setCurrentIndex(index)
                self._combo_instrument.blockSignals(False)
            return
        self._close_edit_panel()
        self._stop_playback()  # 换乐器 = 采样与键位映射都变了，试听必须停
        self._stop_preview()
        self._spec = spec
        self._keys_panel.set_spec(spec)
        self._apply_detection_spec(spec)
        self._clear_detection_state()
        self._reset_status_text()
        index = self._combo_instrument.findData(inst_id)
        if index >= 0 and self._combo_instrument.currentIndex() != index:
            self._combo_instrument.blockSignals(True)
            self._combo_instrument.setCurrentIndex(index)
            self._combo_instrument.blockSignals(False)
        self._refresh_action_emphasis()

    def _apply_detection_spec(self, spec: InstrumentSpec) -> None:
        """Δ 是乐器自己的，采样几何是全局的；tooltip 把两边都写清楚，方便对照 JSON 调试。"""
        detection = spec.detection
        ring = self._sampling
        weights = press_weights(detection)
        self._combo_sample.setToolTip(
            f"用哪圈像素判按（本乐器 Δ={detection.default_delta}，判据 {detection.metric}，"
            f"死区 {detection.tolerance}）：\n"
            f"· 环形：内环 {ring.ring_inner_ratio:.2f}R 圆周上 {ring.ring_points} 个等角点\n"
            f"· 方块：四边各 {ring.square_points} 个等距点，边长 {ring.square_side_ratio:.2f}R\n"
            f"· 每点取 {ring.point_patch_ratio:.2f}R 圆斑均值；权重 "
            f"R{weights[0]:.2f}/G{weights[1]:.2f}/B{weights[2]:.2f}\n"
            f"· 确认帧数 {detection.confirm_frames}（连续 N 帧满足才判按下；1 = 看到就认，"
            f"调大更抗误判但晚 1~2 帧）\n"
            "几何值在 assets/instruments.json 的 sampling 段（所有乐器共用）；「半径」滑条可直接改，手改 JSON 后按 F5 重读"
        )

    def _restore_ui_options(self) -> None:
        """启动时套用上次的「检测方式」，半径直接取 instruments.json 里的值。"""
        ui = self._config.ui
        mode = str(ui.sample_mode or "ring")
        if mode not in ("ring", "square"):
            mode = "ring"  # 旧版本可能存过 ring_points/square_points
        index = self._combo_sample.findData(mode)
        if index >= 0:
            self._combo_sample.blockSignals(True)
            self._combo_sample.setCurrentIndex(index)
            self._combo_sample.blockSignals(False)
        self._sync_radius_slider()

    # -- 配置重载（按需，M2q.2 起不再定时轮询） ----------------------------- #

    def _reload_instruments_by_hand(self) -> None:
        """F5：重读一次，并明确告诉用户有没有变化。"""
        self._reload_instruments(source="F5 手动重读", manual=True)

    def _reload_instruments(self, *, source: str = "", manual: bool = False) -> bool:
        """按需重读 `instruments.json` 与 `sampling` 段。

        调用点（**只有这三处 + F5**）：
        - 启动：`__init__` 里一次性 `load_sampling/load_instruments`；
        - 「开始预览」：把开发者刚改的 Δ / 权重 / 采样几何带进 worker；
        - 「学习基线」：保证基线是按最新参数采的；
        - F5：强制重读（等价于以前的"存盘即生效"，但由人触发）。

        为什么去掉 1.5s 轮询：`stat()` 本身很便宜（µs 级），真正的代价是
        **行为不确定** —— 录制途中可能突然弹「配置已重载」、也可能正好读到写了一半的
        文件。改成按需之后，运行期零后台开销、零意外，调参时按一下 F5 就行。
        """
        if self._radius_timer is not None and self._radius_timer.isActive():
            self._commit_radius()  # 还没落盘的半径先写掉，免得被紧接着的重读覆盖

        mtime = instruments_mtime()
        if mtime == 0:
            if source:
                self._flash_status("读不到 instruments.json，沿用当前参数")
            return False
        if mtime == self._json_mtime:
            if manual:  # 手动按 F5 却没改文件 → 明确回一句，避免"按了没反应"
                self._flash_status(f"乐器配置没有变化（{source}），继续用当前参数")
            return False

        try:
            sampling = load_sampling()
            instruments = load_instruments()
        except Exception as exc:  # 改坏了不能让程序崩，保持旧参数继续跑
            _log.warning("乐器配置重读失败: %s", exc)
            self._flash_status(f"乐器配置读取失败，沿用旧参数：{exc}")
            return False

        self._json_mtime = mtime
        self._sampling = sampling
        self._instruments = instruments
        # 当前乐器按 id 重新解析（可能改了 delta / refs / 判据）
        spec = instruments.get(self._spec.id)
        if spec is not None:
            self._spec = spec
            self._keys_panel.set_spec(spec)
            if self._worker is not None:
                self._worker.set_spec(spec)  # confirm_frames / delta 要传进后台判按器
            if self._baseline is not None:  # set_spec 会清灯板基线，补回来
                self._keys_panel.set_baseline(
                    self._baseline, self._channel_by_area, self._thresholds
                )
        self._apply_detection_spec(self._spec)
        self._sync_sampler_options()
        self._sync_radius_slider()
        self._refresh_sidebar_meta()
        _log.info(
            "乐器配置重读（%s）：内环 %.2fR / 方块半边长 %.2fR / Δ=%s / 确认帧数=%s",
            source or "启动",
            sampling.ring_inner_ratio,
            sampling.square_half_ratio,
            self._delta(),
            self._spec.detection.confirm_frames,
        )
        hint = (
            f"配置已读取（{source or '启动'}）：内环 {sampling.ring_inner_ratio:.2f}R / "
            f"方块半边长 {sampling.square_half_ratio:.2f}R / "
            f"点采样 环{sampling.ring_points}·方{sampling.square_points} / Δ={self._delta()}"
        )
        if self._baseline is not None:
            hint += "（参数有变请重新「学习基线」）"
        self._flash_status(hint)
        return True

    def _monitor_index(self, cx: int, cy: int) -> int:
        """按屏幕坐标求所在显示器索引（0 基；取不到就用主屏）。"""
        screens = QGuiApplication.screens()
        if not screens:
            return 0
        screen = QGuiApplication.screenAt(QPoint(cx, cy)) or QGuiApplication.primaryScreen()
        try:
            return screens.index(screen)
        except ValueError:
            return 0

    def _update_region_label(self) -> None:
        """区域胶囊：窄窗只显示 `W×H · 屏 i/n`，完整 `L,T,W,H` 放 tooltip。"""
        if self._region is None:
            self._lbl_region.setText("未选择")
            self._lbl_region.setToolTip("框选区域")
            theme.set_state(self._lbl_region, "empty", prop="state")
            return
        left, top, width, height = self._region
        index = self._monitor_index(left + width // 2, top + height // 2)
        total = max(1, len(QGuiApplication.screens()))
        self._lbl_region.setText(f"{width}×{height} · 屏 {index + 1}/{total}")
        self._lbl_region.setToolTip(
            f"L,T,W,H = {left},{top},{width},{height}（屏幕 {index + 1}/{total}）"
        )
        theme.set_state(self._lbl_region, "", prop="state")

    def _set_error(self, message: str) -> None:
        self._toast(message, "error")

    def _flash_status(self, message: str) -> None:
        """一次性提示 —— 全部走状态栏右侧的统一消息区（别再分两处跳）。"""
        self._toast(message, "info")

    # -- 目标 / 抓帧 -------------------------------------------------------- #

    def _build_target(self, *, silent: bool = False) -> ScreenTarget | None:
        """捕获目标只有屏幕区域（窗口模式已下线）。"""
        if self._region is None:
            if not silent:
                QMessageBox.information(self, "选择区域", "请先点「框选区域」选择检测范围")
            return None
        left, top, width, height = self._region
        monitor = self._monitor_index(left + width // 2, top + height // 2)
        return ScreenTarget(monitor=monitor, rect=Rect(left, top, width, height))

    def _on_backend_changed(self) -> None:
        """记住用户选的截图方式（`settings.json` 的 `backend`），下次启动直接沿用。

        不在这里动正在跑的预览：切换只影响下一次 `start()` —— 中途换后端要重建
        duplication 会话（dxcam 与 hdrcapture 还互斥），偷偷重启会让用户莫名掉帧。
        """
        if self._suppress_save:
            return
        choice = str(self._combo_backend.currentData() or "hdr")
        if self._config.backend == choice:
            return
        self._config.backend = choice
        self._save_app_config()

    def _backend_preference(self) -> str:
        """后端偏好：直接用「后端」下拉里选中的那个（hdr(j-HDR) / dxgi / gdi）。

        没有"自动"这一档 —— 原来的自动分支对屏幕区域固定返回 gdi，既不自动也不快。
        真打不开（旧机器 / 无 DWM / 独占全屏 / hdrcapture 缺失）时 `CaptureManager.start()`
        会按偏好对应的回退链（j-HDR: hdr→dxgi→gdi；dxgi: dxgi→gdi）自动回退，并通过
        `backend_fallback` 信号把原因显示到状态栏。
        """
        choice = str(self._combo_backend.currentData() or "hdr")
        return choice if choice in ("hdr", "dxgi", "gdi") else "hdr"

    def _grab_snapshot(self, timeout_ms: int = 2500) -> np.ndarray | None:
        """抓一帧静帧用于键位检测（不依赖是否已开始预览）。"""
        if self._worker is not None:
            deadline = time.perf_counter() + timeout_ms / 1000.0
            while True:
                frame = self._worker.latest_frame()
                if frame is not None:
                    return frame
                image = self._preview.frame_image()
                if image is not None:
                    return from_qimage(image)
                if time.perf_counter() >= deadline:
                    return None
                QApplication.processEvents()
                time.sleep(0.02)

        target = self._build_target(silent=True)
        if target is None:
            return None
        manager = CaptureManager()
        self._flash_status("正在抓取一帧画面…")
        try:
            manager.start(target, prefer=self._backend_preference(), target_fps=60)
            deadline = time.perf_counter() + timeout_ms / 1000.0
            while time.perf_counter() < deadline:
                got = manager.latest(timeout_ms=200)
                if got is not None:
                    return got[0].copy()
                QApplication.processEvents()
                time.sleep(0.01)
            return None
        except Exception as exc:
            self._set_error(f"抓帧失败：{exc}")
            return None
        finally:
            try:
                manager.stop()
            except Exception:
                pass

    # -- 框选（唯一的捕获源） ---------------------------------------------- #

    def _pick_region(self) -> None:
        self._close_edit_panel()
        self._picker = RegionPicker(self)
        self._picker._aspect = 16.0 / 9.0 if self._chk_aspect.isChecked() else None
        self._picker.region_selected.connect(self._on_region_selected)
        self._picker.cancelled.connect(lambda: self._flash_status("已取消框选"))
        self._picker.start()

    def _on_region_selected(self, region: tuple[int, int, int, int]) -> None:
        new_region = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
        changed = self._region is not None and self._region != new_region
        self._region = new_region
        left, top, width, height = new_region
        self._update_region_label()
        if changed:
            self._stop_preview()
            self._clear_detection_state()
            self._flash_status("区域已变更，请重新检测键位")
        else:
            self._flash_status(f"已选择区域 {left},{top},{width},{height}")
        self._save_profile()

    # -- 键位 -------------------------------------------------------------- #

    def _set_grid(self, grid: KeyGrid | None) -> None:
        self._grid = grid
        self._preview.set_grid(grid)
        if self._worker is not None and grid is not None:
            self._worker.set_keygrid(grid)
        self._btn_baseline.setEnabled(grid is not None)
        self._keys_panel.set_baseline(self._baseline, self._channel_by_area, self._thresholds)
        self._refresh_record_enabled()
        self._refresh_sidebar_meta()
        self._refresh_action_emphasis()

    def _clear_detection_state(self) -> None:
        """清掉键位/阈值/基线（不动框选区域）。"""
        self._grid = None
        self._thresholds = None
        self._baseline = None
        self._channel_by_area = {}
        self._partial_points = []
        self._preview.set_grid(None)
        self._preview.set_press([])
        self._keys_panel.set_baseline(None, {}, None)
        self._keys_panel.clear_press()
        if self._worker is not None:
            self._worker.set_keygrid(None)
        self._btn_baseline.setEnabled(False)
        self._refresh_record_enabled()
        self._refresh_sidebar_meta()
        self._refresh_action_emphasis()

    def _reset_detection(self) -> None:
        """重置键位检测：清空圈/阈值/基线，并把 profile 里的 keygrid 一并删掉。"""
        self._close_edit_panel()
        self._clear_detection_state()
        self._save_profile()
        self._flash_status("已重置键位检测：点「自动检测键位」或「手动点选」重新标定")

    def _auto_detect_keys(self) -> None:
        """自动检测：颜色法优先，失败回退 Hough；不需要先开始预览。"""
        if self._spec.key_count % KEYS_PER_ROW != 0:
            QMessageBox.information(
                self,
                "不支持自动检测",
                f"该乐器键数 {self._spec.key_count} 不是 {KEYS_PER_ROW} 的倍数，"
                "无法按 7 列网格自动定位，请点「手动点选」自行标定。",
            )
            return
        frame = self._grab_snapshot()
        if frame is None:
            QMessageBox.information(
                self, "拿不到画面", "没能抓到画面：请先点「框选区域」选择检测范围。"
            )
            return
        circles: list[Any] = []
        corrected: list[Any] = []
        try:
            color_circles = detect_keys_color(frame, self._spec)
            if len(color_circles) == self._spec.key_count:
                circles = color_circles
                corrected = color_circles
            else:
                circles = detect_keys(frame, self._spec.hough, self._spec.key_count)
                corrected = correct_button_coordinates(circles) if circles else []
        except Exception as exc:
            QMessageBox.warning(self, "自动检测失败", f"{exc}\n\n可点「手动点选」自行标定。")
            return
        if len(corrected) != self._spec.key_count:
            self._partial_points = [
                (float(c.x), float(c.y))
                for c in sorted(circles or [], key=lambda c: (c.y, c.x))
            ][: self._spec.key_count]
            self._flash_status(
                f"检测到 {len(self._partial_points)} 个圈（需要 {self._spec.key_count}），"
                f"已进入编辑模式：请补齐剩余的圈"
            )
            self._start_edit()
            return
        try:
            mapping = circle_map_note(corrected, self._spec)
            height, width = frame.shape[:2]
            grid = keygrid_from_note_map(mapping, self._spec, width, height)
        except Exception as exc:
            QMessageBox.warning(self, "键位映射失败", f"{exc}\n\n可点「手动点选」自行标定。")
            return
        self._set_grid(grid)
        self._save_profile()
        if self._worker is not None:
            self._flash_status(f"自动检测完成：{grid.key_count} 个键位（已生效）")
        else:
            if not self._preview.has_frame():
                self._preview.set_frame(to_qimage(frame))
            self._flash_status(f"键位已就绪（{grid.key_count} 个），点「开始预览」即可实时查看")

    # -- 编辑模式（拖拽 / 增删 / 保存退出） -------------------------------- #

    def _start_edit(self) -> None:
        if self._edit_panel is not None:
            return
        if not self._preview.has_frame():
            frame = self._grab_snapshot()
            if frame is None:
                QMessageBox.information(
                    self, "拿不到画面", "请先点「框选区域」选择检测范围，再手动标定键位。"
                )
                return
            self._preview.set_frame(to_qimage(frame))

        points: list[tuple[float, float]] = []
        if self._grid is not None:
            width, height = self._preview.frame_size()
            points = [(p.u * width, p.v * height) for p in self._grid.points]
        elif self._partial_points:
            points = list(self._partial_points)
            self._partial_points = []

        self._preview.enter_edit(points, limit=self._spec.key_count)
        self._preview.set_hint("拖动圈可调整位置")
        self._btn_detect.setEnabled(False)
        self._btn_manual.setEnabled(False)
        self._btn_reset.setEnabled(False)
        self._build_edit_panel()
        self._flash_status(
            "编辑模式：拖动圈调整位置；「新增/删除」后点画面操作；「保存退出」或点空白处生效"
        )

    def _build_edit_panel(self) -> None:
        """手动点选浮层：三段模式 + 计数 + 提示 + 保存退出。

        视觉上是**真浮层**（投影 + 顶部指向按钮的小三角），交互上补两条：
        点预览空白处 / 按 Esc 都等于「保存退出」（圈数不够时只提示，不静默丢弃）。
        """
        panel = EditPanel(self)
        panel.setGraphicsEffect(panel_shadow(panel, dark=theme.is_dark()))
        layout = QVBoxLayout(panel)
        # 顶部 9px 是箭头带（`widgets.ARROW_HEIGHT`），其余三边 8px
        layout.setContentsMargins(8, 9 + 4, 8, 8)
        layout.setSpacing(6)

        segmented = QWidget(panel)
        segmented.setObjectName("Segmented")
        seg_row = QHBoxLayout(segmented)
        seg_row.setContentsMargins(2, 2, 2, 2)
        seg_row.setSpacing(0)
        self._btn_edit_drag = self._segment_button(segmented, "拖动", EDIT_MODE_DRAG)
        self._btn_edit_add = self._segment_button(segmented, "新增", EDIT_MODE_ADD)
        self._btn_edit_delete = self._segment_button(segmented, "删除", EDIT_MODE_DELETE)
        for button in (self._btn_edit_drag, self._btn_edit_add, self._btn_edit_delete):
            seg_row.addWidget(button, 1)
        # 兼容旧引用：`_btn_add` / `_btn_delete` 就是新增/删除两段
        self._btn_add = self._btn_edit_add
        self._btn_delete = self._btn_edit_delete

        self._lbl_edit_count = QLabel(f"当前 {len(self._preview.edit_points())}/{self._spec.key_count}")
        self._lbl_edit_count.setObjectName("EditCount")
        self._lbl_edit_count.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._lbl_edit_hint = QLabel("拖动检测圈调整位置")
        self._lbl_edit_hint.setObjectName("EditHint")
        self._lbl_edit_hint.setWordWrap(True)
        self._lbl_edit_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        save = QPushButton("保存退出")
        save.setObjectName("Primary")
        save.setFixedHeight(TOOL_BUTTON_HEIGHT)
        save.clicked.connect(self._save_edit)

        layout.addWidget(segmented)
        layout.addWidget(self._lbl_edit_count)
        layout.addWidget(self._lbl_edit_hint)
        layout.addWidget(save)

        panel.setFixedWidth(EDIT_PANEL_WIDTH)
        panel.adjustSize()
        self._edit_panel = panel
        self._set_edit_mode(EDIT_MODE_DRAG, announce=False)
        self._position_edit_panel()  # 贴按钮右下，并夹在窗口内
        panel.show()
        panel.raise_()

    def _segment_button(self, parent: QWidget, text: str, mode: str) -> QPushButton:
        """分段控件的一段：checkable，互斥由 `_set_edit_mode()` 手动维护。

        不用 `QButtonGroup`：现有逻辑已经在 `_set_edit_mode` 里做了互斥，
        再加 group 就是两套状态，容易出现"按钮显示新增、实际在拖动"。
        """
        button = QPushButton(text, parent)
        button.setObjectName("Segment")
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda: self._set_edit_mode(mode))
        return button

    def _position_edit_panel(self) -> None:
        """把编辑面板摆到「手动点选」右下方，并保证**完整落在窗口内**。

        窗口变窄时向右会被裁掉 → 右对齐按钮右缘并夹到左边界；
        窗口变矮时向下会压到状态栏 → 改放到按钮上方（仍放不下就贴顶）。
        箭头水平位置跟着面板一起算，保证永远指着触发按钮的中心。
        """
        panel = self._edit_panel
        if panel is None:
            return
        panel.adjustSize()
        button = self._btn_manual
        margin = 6
        top_left = button.mapTo(self, QPoint(0, button.height() + 6))
        right_edge = button.mapTo(self, QPoint(button.width(), 0)).x()

        x = right_edge - panel.width()
        x = max(margin, min(x, self.width() - panel.width() - margin))
        y = top_left.y()
        lowest = self.height() - panel.height() - margin
        if y > lowest:
            y = max(margin, min(top_left.y() - panel.height() - 12, lowest))
        else:
            y = max(margin, y)
        panel.move(x, y)
        button_center = button.mapTo(self, QPoint(button.width() // 2, 0)).x()
        panel.arrow_x = float(button_center - x)
        panel.update()

    def _set_edit_mode(self, mode: str, *, announce: bool = True) -> None:
        if mode not in (EDIT_MODE_DRAG, EDIT_MODE_ADD, EDIT_MODE_DELETE):
            mode = EDIT_MODE_DRAG
        self._preview.set_edit_mode(mode)
        # 三段互斥（手动维护，避免按钮状态与实际模式分离）
        for button, button_mode in (
            (self._btn_edit_drag, EDIT_MODE_DRAG),
            (self._btn_edit_add, EDIT_MODE_ADD),
            (self._btn_edit_delete, EDIT_MODE_DELETE),
        ):
            if button is not None:
                button.setChecked(button_mode == mode)
        hints = {
            EDIT_MODE_ADD: "点画面新增检测圈",
            EDIT_MODE_DELETE: "点中要删除的检测圈",
            EDIT_MODE_DRAG: "拖动检测圈调整位置",
        }
        if self._lbl_edit_hint is not None:
            self._lbl_edit_hint.setText(hints.get(mode, ""))
        if not announce:
            return
        full = {
            EDIT_MODE_ADD: f"新增模式：点画面任意位置新增一个检测圈（上限 {self._spec.key_count}）",
            EDIT_MODE_DELETE: "删除模式：点中要删除的检测圈",
            EDIT_MODE_DRAG: "拖动模式：按住检测圈拖动",
        }
        self._flash_status(full.get(mode, ""))

    def _on_selection_changed(self, index: int) -> None:
        if self._lbl_edit_count is not None:
            total = len(self._preview.edit_points())
            selected = f" · #{index + 1}" if index >= 0 else ""
            self._lbl_edit_count.setText(f"当前 {total}/{self._spec.key_count}{selected}")

    def _on_points_changed(self) -> None:
        if self._edit_panel is None:
            return
        total = len(self._preview.edit_points())
        if self._lbl_edit_count is not None:
            self._lbl_edit_count.setText(f"当前 {total}/{self._spec.key_count}")
        for button, button_mode in (
            (self._btn_edit_drag, EDIT_MODE_DRAG),
            (self._btn_edit_add, EDIT_MODE_ADD),
            (self._btn_edit_delete, EDIT_MODE_DELETE),
        ):
            if button is not None:
                button.setChecked(self._preview.edit_mode == button_mode)

    def _on_escape(self) -> None:
        """Esc：编辑态退出（等同保存退出）；其它状态不拦截任何按键。"""
        if self._edit_panel is None:
            return
        self._save_edit()

    def _on_canvas_background_clicked(self) -> None:
        """编辑态点了预览空白：等同「保存退出」（圈数不够则只提示，不静默丢弃）。"""
        if self._edit_panel is None:
            return
        self._save_edit()

    def _save_edit(self) -> None:
        points = self._preview.edit_points()
        if len(points) != self._spec.key_count:
            self._flash_status(
                f"当前 {len(points)} 个检测圈，需要 {self._spec.key_count} 个才能保存"
            )
            return
        grid = self._grid_from_points(points)
        if grid is None:
            self._set_error("无法根据当前点位生成 3×7 网格，请调整点位后重试")
            return
        self._close_edit_panel()
        self._set_grid(grid)
        self._save_profile()
        self._flash_status(f"键位已保存（{grid.key_count} 个）")

    def _grid_from_points(self, points: list[tuple[float, float]]) -> KeyGrid | None:
        """把编辑态的像素点转成 `KeyGrid`：先按 3×7 行主序精确还原，失败再交给网格校正。"""
        width, height = self._preview.frame_size()
        if width <= 0 or height <= 0:
            return None
        ordered = self._rows_major(points, width)
        radius = float(self._spec.button_radius) / 1920.0
        if ordered is not None:
            packed = tuple(
                KeyPoint(
                    u=min(1.0, max(0.0, x / width)),
                    v=min(1.0, max(0.0, y / height)),
                    r=radius,
                )
                for x, y in ordered
            )
            return KeyGrid(points=packed, key_count=self._spec.key_count)
        try:
            return keygrid_from_points(points, self._spec, width, height)
        except Exception as exc:
            _log.warning("点位转网格失败: %s", exc)
            return None

    def _rows_major(
        self, points: list[tuple[float, float]], frame_width: int
    ) -> list[tuple[float, float]] | None:
        """按 y 聚成 3 行、行内按 x 排序；不是严格的 3×7 就返回 None。"""
        if len(points) != self._spec.key_count:
            return None
        tolerance = max(12.0, 0.6 * float(self._spec.button_radius) * frame_width / 1920.0)
        rows: list[list[tuple[float, float]]] = []
        for point in sorted(points, key=lambda p: p[1]):
            if rows and abs(point[1] - float(np.mean([p[1] for p in rows[-1]]))) <= tolerance:
                rows[-1].append(point)
            else:
                rows.append([point])
        expected_rows = self._spec.key_count // KEYS_PER_ROW
        if len(rows) != expected_rows or any(len(row) != KEYS_PER_ROW for row in rows):
            return None
        return [point for row in rows for point in sorted(row, key=lambda p: p[0])]

    def _close_edit_panel(self) -> None:
        if self._edit_panel is None:
            return
        self._preview.exit_edit()
        self._preview.set_hint("")
        self._edit_panel.deleteLater()
        self._edit_panel = None
        self._btn_edit_drag = None
        self._btn_edit_add = None
        self._btn_edit_delete = None
        self._btn_add = None
        self._btn_delete = None
        self._lbl_edit_count = None
        self._lbl_edit_hint = None
        self._btn_detect.setEnabled(True)
        self._btn_manual.setEnabled(True)
        self._btn_reset.setEnabled(True)

    # -- 预览 / 基线 ------------------------------------------------------- #

    def _toggle_preview(self) -> None:
        if self._worker is not None:
            self._stop_preview()
            return
        target = self._build_target()
        if target is None:
            return
        if self._play_mode:  # 预览要占 CPU，试听先停（暂停也算）
            self._stop_playback()

        self._low_fps_hinted = False
        self._src_limited_hinted = False
        self._reload_instruments(source="开始预览")  # 开发者刚改的 Δ/权重/采样几何要带进来
        backend = self._backend_preference()
        self._requested_backend = backend
        worker = CaptureWorker(self)
        worker.configure(
            target,
            self._spec,
            self._grid,
            thresholds=self._thresholds,
            backend=backend,
            target_fps=60,
            baseline=self._baseline,
            # 时间闸门来自 settings.json 的 record 段（默认 0 = 单帧即确认）
            detector_cfg=DetectorConfig(
                min_hold_ms=float(self._config.record.min_hold_ms),
                min_gap_ms=float(self._config.record.min_gap_ms),
            ),
        )
        worker.frame_ready.connect(self._preview.set_frame)
        worker.samples_ready.connect(self._keys_panel.update_samples)
        worker.press_ready.connect(self._on_press)
        worker.status_ready.connect(self._on_status)
        worker.baseline_ready.connect(self._on_baseline)
        worker.backend_fallback.connect(self._on_backend_fallback)
        worker.record_progress.connect(self._on_record_progress)
        worker.error.connect(self._on_worker_error)
        worker.stopped.connect(self._on_worker_stopped)
        self._worker = worker
        self._sync_sampler_options()  # 新建的 worker 需要立刻拿到采样口径（点采样/内径/方块边长）
        worker.start()
        self._btn_preview.setText("停止预览")
        self._preview.set_stopped(False)
        self._btn_baseline.setEnabled(self._grid is not None)
        if self._grid is None:
            self._flash_status(f"预览已启动（后端 {backend_label(backend)}）；尚未检测键位")
        else:
            self._flash_status(f"预览已启动（后端 {backend_label(backend)}）；灯不亮请松开所有键后点「学习基线」")
        self._refresh_record_enabled()
        self._refresh_action_emphasis()

    def _stop_preview(self) -> None:
        # 预览停 = 录制同步结束（用户要求）：必须在还持有 worker 时收尾
        if self._recording:
            self._finalize_recording()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()
            if not worker.wait(2000):
                # DXGI 的 get_latest_frame 在画面静止时会阻塞，兜底强杀避免 Qt 析构崩溃
                self._set_error("捕获线程未及时退出，已强制结束")
                worker.terminate()
                worker.wait(1000)
            worker.deleteLater()
        self._btn_preview.setText("开始预览")
        self._preview.set_stopped(self._preview.has_frame())
        self._btn_baseline.setEnabled(False)
        self._preview.clear_sample_overlay()
        self._preview.set_hint("")
        self._preview.set_press([])
        self._keys_panel.clear_press()
        self._reset_status_text()
        self._refresh_record_enabled()
        self._refresh_playback_enabled()
        self._refresh_action_emphasis()

    def _on_worker_stopped(self, backend: str) -> None:
        if self._recording:
            self._finalize_recording()
        if self._worker is None:
            return
        self._flash_status(f"预览已停止（后端 {backend_label(backend)}）")
        self._btn_preview.setText("开始预览")
        self._btn_baseline.setEnabled(False)
        self._refresh_record_enabled()
        self._refresh_action_emphasis()

    def _on_worker_error(self, message: str) -> None:
        self._set_error(message)
        if self._worker is not None:
            self._flash_status("预览出错，可换「后端」后重试")

    def _on_press(self, flags: list[bool]) -> None:
        self._preview.set_press(flags)
        self._keys_panel.set_press(flags)

    def _on_backend_fallback(self, message: str) -> None:
        """用户选了 A 后端却回退到 B 时，直接说清原因（别让状态栏悄悄变成另一个名字）。"""
        self._toast(message, "warn")

    def _on_status(self, info: dict[str, Any]) -> None:
        self._last_status_info = info
        self._last_backend = str(info.get("backend", self._last_backend))
        actual = str(info.get("backend", "-"))
        frames = int(info.get("frames", 0))
        requested = self._requested_backend
        fallback = requested not in ("", "auto", actual)
        self._lbl_backend.setText(f"{backend_label(actual).upper()}{' 回退' if fallback else ''}")
        theme.set_state(self._lbl_backend, "warn" if fallback else "", prop="tone")

        if info.get("waiting"):
            self._lbl_status.setText(self._status_text(0.0, 0.0, 0.0, frames))
            return
        fps = float(info.get("fps", 0))
        src_fps = float(info.get("src_fps", 0))
        if not self._low_fps_hinted and frames > 30 and 0 < fps < 5:
            self._low_fps_hinted = True
            self._toast("帧率偏低：画面静止时属正常；若持续偏低可在「后端」里换 dxgi 试试", "warn")
        # 源帧率 ≈ 处理帧率 且都不到 45 → 是"画面本身只有这么多帧"，不是我们在丢帧
        if (
            not self._src_limited_hinted
            and frames > 60
            and 0 < src_fps < 45
            and fps <= src_fps * 1.15
        ):
            self._src_limited_hinted = True
            self._toast(
                f"源画面只有 {src_fps:.0f} 帧/秒（游戏/视频的帧率上限），程序没有丢帧；"
                "想更灵敏请在游戏里开 60 帧",
                "warn",
            )
        self._lbl_status.setText(
            self._status_text(fps, src_fps, float(info.get("ms", 0)), frames)
        )

    def _learn_baseline(self) -> None:
        if self._worker is None:
            QMessageBox.information(
                self, "需要预览", "请先「开始预览」；并在全部松开琴键时学习基线。"
            )
            return
        if self._grid is None:
            QMessageBox.information(
                self,
                "需要键位",
                f"请先「自动检测键位」或「手动点选」标定 {self._spec.key_count} 个键位，再学习基线。",
            )
            return
        self._reload_instruments(source="学习基线")
        self._btn_baseline.setEnabled(False)
        self._show_sample_overlay()
        self._preview.set_hint("红点 = 采样点（松开所有键）")
        self._worker.request_baseline(1.5)
        self._flash_status("正在学习基线：请现在松开所有琴键，保持 1.5 秒…")

    def _show_sample_overlay(self) -> None:
        """学习基线期间把采样范围标出来（蓝色半透明），结束时清除。"""
        if self._grid is None or not self._preview.has_frame():
            return
        self._preview.show_sample_overlay(
            self._grid,
            inner_ratio=self._effective_inner_ratio(),
            square_side=self._effective_square_side(),
            offset_y=0.0,
            point_mode="points",
            ring_points=int(self._sampling.ring_points),
            square_points=int(self._sampling.square_points),
        )

    def _on_baseline(self, baseline: np.ndarray) -> None:
        self._preview.clear_sample_overlay()
        self._preview.set_hint("")
        self._baseline = np.asarray(baseline, dtype=np.float32)
        self._recompute_thresholds()
        self._keys_panel.set_baseline(self._baseline, self._channel_by_area, self._thresholds)
        if self._worker is not None:
            self._worker.update_thresholds(self._thresholds, self._baseline)
            self._btn_baseline.setEnabled(True)
        self._save_profile()
        self._flash_status(f"基线完成（Δ={self._delta()}），现在按键看灯是否亮")
        self._warn_suspicious_baseline()
        self._refresh_record_enabled()
        self._refresh_sidebar_meta()
        self._refresh_action_emphasis()

    def _suspicious_baseline_keys(self) -> list[str]:
        """基线里"颜色明显离群"的键 —— 多半是学习时正被按着（学成了按下色）。

        半透明键帽按下时色偏会整体平移；若这些键在学基线时是按下状态，
        之后它们永远回不到"未按下"的基线，表现就是"怎么调灵敏度都检不到"。
        """
        if self._baseline is None:
            return []
        base_from, base_to = chroma_axis(self._spec.detection)
        values = self._baseline[:, base_to] - self._baseline[:, base_from]
        median = float(np.median(values))
        return [
            key
            for key, value in zip(self._spec.key_order, values, strict=False)
            if abs(float(value) - median) > BASELINE_OUTLIER_SHIFT
        ]

    def _warn_suspicious_baseline(self) -> None:
        outliers = self._suspicious_baseline_keys()
        if len(outliers) < 2:
            return
        shown = " ".join(outliers[:6]) + ("…" if len(outliers) > 6 else "")
        self._set_error(
            f"基线可疑：{len(outliers)} 个键（{shown}）颜色异常，"
            "学习时它们可能正被按下 —— 请全部松开后重新学习基线"
        )

    def _refresh_channel_by_area(self) -> None:
        self._channel_by_area = {}
        for area in self._spec.areas:
            threshold = (self._thresholds or {}).get(area.code)
            if threshold is None:
                continue
            if threshold.mode == "max_drop":
                self._channel_by_area[area.code] = MAX_CHANNEL
                continue
            for channel in ("R", "G", "B"):
                if (threshold.channel_range(channel) or (0, 0))[1] > 0:
                    self._channel_by_area[area.code] = channel
                    break

    def _recompute_thresholds(self) -> None:
        if self._baseline is None:
            self._thresholds = None
            self._refresh_channel_by_area()
            return
        self._thresholds = thresholds_from_baseline(self._baseline, self._spec, self._delta())
        self._refresh_channel_by_area()

    # -- 录制（M2） -------------------------------------------------------- #

    def _refresh_record_enabled(self) -> None:
        """录制按钮：必须「已学基线」+「预览已启动」才可点（录制中保持可点以便停止）。"""
        if self._recording:
            self._btn_record.setEnabled(True)
            return
        ready = self._worker is not None and self._baseline is not None and self._grid is not None
        self._btn_record.setEnabled(ready)
        if ready:
            self._btn_record.setToolTip("开始录制谱面（再点一次结束）")
        else:
            self._btn_record.setToolTip("需先「学习基线」并「开始预览」，才能开始录制")

    def _record_click_warmup(self) -> None:
        """录制启动前的可选输入注入（`instruments.json` 的 `record_click`，全局参数）。

        模式：``0`` 不启用 / ``1`` 激活区域所在窗口后模拟空格 / ``2`` 区域中心模拟左键。
        均借鉴 BetterGI 前台 SendInput 方案 + MAA 前台激活三段式（01 §15）；
        放在 ``start_recording()`` **之前**执行，注入本身不会进谱面；
        任何失败都只记日志/状态栏提示，绝不拦录制。
        """
        try:
            mode = load_record_click()
        except Exception:
            _log.exception("读取 record_click 配置失败，跳过输入注入")
            return
        if mode == 0:
            return
        if self._region is None:
            _log.warning("record_click 已启用（模式 %d）但没有框选区域，跳过注入", mode)
            return
        left, top, width, height = self._region
        cx, cy = left + width // 2, top + height // 2
        try:
            if mode == 2:
                self._record_click_mouse(cx, cy)
            elif mode == 1:
                self._record_click_focus_space(cx, cy)
            else:
                _log.warning("record_click 模式 %d 未实现，跳过", mode)
        except Exception:
            _log.exception("录制启动输入注入（模式 %d）异常", mode)

    def _record_click_mouse(self, cx: int, cy: int) -> None:
        """模式 2：框选区域中心模拟一次左键点击（点完恢复光标）。"""
        try:
            ok = inputsim.left_click(cx, cy)
        except Exception:
            _log.exception("录制启动模拟点击 (%d, %d) 异常", cx, cy)
            return
        if not ok:
            self._flash_status("录制启动模拟点击失败（详见日志），录制继续")
            return
        _log.info("录制启动模拟点击: (%d, %d)", cx, cy)

    def _record_click_focus_space(self, cx: int, cy: int) -> None:
        """模式 1：激活区域所在窗口后模拟一次空格（安全阀见 01 §15）。

        激活失败坚决不发空格——空格会落回本程序焦点控件（录制按钮），
        导致录制立即被误停。
        """
        try:
            hwnd = inputsim.window_at(cx, cy)
        except Exception:
            _log.exception("定位 (%d, %d) 处窗口异常", cx, cy)
            return
        if not hwnd:
            self._flash_status("录制启动：区域中心未找到窗口，跳过空格注入，录制继续")
            return
        if inputsim.is_own_window(hwnd) or hwnd == int(self.winId()):
            _log.warning("区域中心 (%d, %d) 命中本程序窗口，不发空格", cx, cy)
            return
        try:
            ok = inputsim.activate_window(hwnd)
        except Exception:
            _log.exception("激活窗口 0x%x 异常", hwnd)
            return
        if not ok:
            self._flash_status("录制启动：目标窗口激活失败，跳过空格注入，录制继续")
            return
        try:
            sent = inputsim.press_space()
        except Exception:
            _log.exception("录制启动模拟空格异常")
            return
        if not sent:
            self._flash_status("录制启动空格注入失败（详见日志），录制继续")
            return
        _log.info("录制启动空格注入: 窗口 0x%x @ (%d, %d)", hwnd, cx, cy)

    def _toggle_record(self) -> None:
        if self._recording:
            self._finalize_recording()
            return
        self._start_recording()

    def _start_recording(self) -> None:
        if self._worker is None:
            QMessageBox.information(self, "需要预览", "请先点「开始预览」，再开始录制。")
            return
        if self._baseline is None:
            QMessageBox.information(
                self, "需要基线", "请先松开所有键点「学习基线」，基线完成后才能录制。"
            )
            return
        self._record_click_warmup()

        if not self._worker.start_recording():
            self._set_error("录制未启动：键位/乐器尚未就绪，请重新标定后重试")
            return

        self._recording = True
        self._record_result = None
        self._stop_playback()
        self._playback_score = None
        self._set_record_text("")
        self._lbl_record_info.setText("录制中 · 0 个音")
        self._refresh_save_enabled()
        self._btn_record.setText("停止录制")
        self._lock_while_recording(True)
        self._flash_status("录制中：在游戏里弹，再点「停止录制」结束（或点「停止预览」也会收尾）")
        self._refresh_action_emphasis()

    def _finalize_recording(self) -> None:
        """结束录制 → 出谱 → 刷新文本框与保存按钮（可重复调用）。"""
        if not self._recording:
            return
        self._recording = False
        self._btn_record.setText("录制谱面")
        self._lock_while_recording(False)

        worker = self._worker
        result: Recording | None = None
        if worker is not None:
            try:
                result = worker.stop_recording()
            except Exception as exc:  # 装配异常不该打死主窗
                _log.exception("结束录制失败: %s", exc)
                self._set_error(f"结束录制失败：{exc}")

        if result is None or not result.notes:
            self._record_result = None
            self._playback_score = None
            self._lbl_record_info.setText("未录到音符")
            self._refresh_save_enabled()
            if result is not None:
                self._set_record_text("")
            self._flash_status("录制结束：没有识别到音符，可降低「灵敏度 Δ」后重试")
            self._refresh_record_enabled()
            self._refresh_action_emphasis()
            return

        self._record_result = result
        self._playback_score = result.score
        self._set_record_text(result.tick_text)
        self._lbl_record_info.setText(
            f"{result.note_count} 个音 · {_mmss(result.duration_ms)}"
        )
        self._lbl_record_info.setToolTip(
            f"{result.note_count} 个音 · {result.frames} 帧 · "
            f"{result.duration_ms / 1000.0:.1f}s · {result.fps:.0f}fps"
        )
        self._refresh_save_enabled()
        self._flash_status(
            f"录制完成：{result.note_count} 个音 / {result.duration_ms / 1000.0:.1f} 秒，"
            "点「保存」存成 JSON"
        )
        self._refresh_record_enabled()
        self._refresh_action_emphasis()

    def _lock_while_recording(self, recording: bool) -> None:
        """录制期间锁住会改变识别配置的控件与录入框（否则谱面会被中途改坏）。"""
        for widget in (
            self._btn_region,
            self._chk_aspect,
            self._combo_instrument,
            self._combo_backend,
            self._combo_sample,
            self._slider_radius,
            self._btn_detect,
            self._btn_manual,
            self._btn_reset,
        ):
            widget.setEnabled(not recording)
        # 录制中文本框只读：内容由 worker 实时刷，手改会被下一帧覆盖、也会让高亮/试听错位
        self._record_view.setReadOnly(recording)
        self._record_view.setProperty("readonly", "true" if recording else "false")
        theme.repolish(self._record_view)
        self._record_view.setToolTip(
            "录制中不可编辑，点「停止录制」后可以自由修改"
            if recording
            else "可直接编辑：保存与试听都以这里的内容为准"
        )
        if not recording:
            self._btn_baseline.setEnabled(self._grid is not None)

    def _on_record_progress(self, info: dict[str, Any]) -> None:
        """录制中实时刷新文本框（worker 线程发来的快照）。"""
        if not self._recording:
            return
        self._set_record_text(str(info.get("text", "")))
        self._lbl_record_info.setText(
            f"录制中 · {int(info.get('notes', 0))} 个音 · "
            f"{_mmss(float(info.get('duration_ms', 0)))}"
        )

    def _refresh_save_tooltip(self) -> None:
        """保存按钮的 tooltip 跟随格式下拉。"""
        fmt = str(self._combo_format.currentData() or DEFAULT_SCORE_FORMAT)
        tips = {
            DEFAULT_SCORE_FORMAT: "保存为 YQ_moTools 谱面 JSON（存文本框里的毫秒间隔谱面）",
            BGI_MIDI_FORMAT: "保存为 BetterGI 曲谱（type=midi，D/U 事件流，1 tick = 1ms）",
            BGI_YQ_FORMAT: "保存为 BetterGI 曲谱（type=yuanqin，音符时值记号）",
        }
        self._btn_save_score.setToolTip(tips.get(fmt, "按左侧选择的格式保存文本框里的谱面"))

    def _refresh_save_enabled(self) -> None:
        """保存按钮只看文本框：有谱面就能存（录制、读取、手输一视同仁）。"""
        has_text = bool(self._record_view.toPlainText().strip())
        self._btn_save_score.setEnabled(has_text and not self._recording)

    def _save_score(self) -> None:
        """把**文本框里的谱面**按下拉格式存盘（不要求它来自本次录制）。"""
        text = self._record_view.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "没有谱面", "文本框里还没有谱面内容。")
            return
        fmt = str(self._combo_format.currentData() or DEFAULT_SCORE_FORMAT)
        sheet_type = BGI_FORMATS.get(fmt)
        default_path = Path(self._score_dialog_dir()) / suggested_filename(fmt)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存谱面" if sheet_type is None else f"保存 BetterGI 曲谱（{sheet_type}）",
            str(default_path),
            "YQ_moTools 谱面 (*.json)" if sheet_type is None else "BetterGI 曲谱 (*.json)",
        )
        if not path:
            return
        loss = None
        try:
            if sheet_type is None:
                saved = self._save_tick_json(path, text)
            else:
                saved = bgi_sheet.save_sheet_typed(
                    path,
                    text,
                    self._spec,
                    sheet_type=sheet_type,
                    name=Path(path).stem,
                    description=f"YQ_moTools · {fmt}",
                )
                if sheet_type == "yuanqin":
                    # yuanqin 是"音符时值"的单声部格式：叠音与超长按都会被截短，
                    # 保存成功也要把损失说出来（否则用户以为延音带过去了）。
                    loss = bgi_sheet.yuanqin_sustain_loss(
                        bgi_sheet.score_from_tick_text(text), self._spec
                    )
        except Exception as exc:
            _log.exception("保存谱面失败: %s", exc)
            self._set_error(f"保存失败：{exc}")
            return
        self._remember_score_dir(saved.parent)
        self._lbl_record_info.setText(f"已保存 {fmt} → {saved.name}")
        if loss is not None and loss.count:
            self._toast(
                f"已保存，但 yuanqin 不能叠音：{loss.count} 处延音被截短"
                f"（最长 {loss.max_ms:.0f}ms）。要完整延音请改存「BGI · midi」",
                "warn",
            )
        else:
            self._flash_status(f"已保存（{fmt}）：{saved}")

    def _save_tick_json(self, path: Path | str, text: str) -> Path:
        """tick 格式落盘：若是本次录制，保留 capture 里的真实采集信息。"""
        result = self._record_result
        extra = {"profile": PROFILE_NAME, "delta": self._delta()}
        if result is not None and result.tick_text.strip() == text:
            return save_record(
                path,
                result,
                self._spec,
                backend=self._last_backend,
                score_format=DEFAULT_SCORE_FORMAT,
                extra=extra,
            )
        return save_record_text(
            path,
            text,
            self._spec,
            backend=self._last_backend,
            fps=float(getattr(result, "fps", 0.0) or 0.0),
            score_format=DEFAULT_SCORE_FORMAT,
            extra=extra,
        )

    # -- 谱面文件对话框目录（记住上次） ------------------------------------ #

    def _score_dialog_dir(self) -> str:
        """保存/读取对话框的起始目录：上次用过的优先，否则 `文档\\YQ_moTools`。"""
        remembered = str(self._config.paths.last_score_dir or "")
        if remembered and Path(remembered).is_dir():
            return remembered
        fallback = record_default_dir()
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _log.debug("创建默认谱面目录失败（退回主目录）: %s", exc)
            return str(Path.home())
        return str(fallback)

    def _remember_score_dir(self, directory: Path | str) -> None:
        """记下本次保存/读取所在目录并立刻落盘（重启后仍生效）。"""
        path = Path(directory)
        if not path.is_dir():
            return
        value = str(path)
        if value == self._config.paths.last_score_dir:
            return
        self._config.paths.last_score_dir = value
        self._save_app_config()

    # -- 读取 / 试听（M2b） ------------------------------------------------ #

    def _set_record_text(self, text: str) -> None:
        """程序写入谱面文本（录制 / 读取），并同步高亮区间与按钮状态。"""
        self._tick_spans = tick_spans(text)
        self._record_view.blockSignals(True)  # 避免触发用户编辑回调（会重建播放谱面）
        self._record_view.setPlainText(text)
        self._record_view.setExtraSelections([])
        cursor = self._record_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._record_view.setTextCursor(cursor)
        self._record_view.blockSignals(False)
        self._refresh_playback_enabled()
        self._refresh_save_enabled()

    def _on_record_text_edited(self) -> None:
        """文本框被用户改动：高亮区间与试听谱面都跟着文本重建（文本是唯一来源）。"""
        if self._play_mode:  # 文本变了，原来的高亮下标就没意义了
            self._stop_playback()
        text = self._record_view.toPlainText()
        self._tick_spans = tick_spans(text)
        self._record_view.setExtraSelections([])
        self._playback_score = bgi_sheet.score_from_tick_text(text) if text.strip() else None
        self._refresh_save_enabled()
        self._refresh_playback_enabled()

    def _refresh_playback_enabled(self) -> None:
        """试听按钮：预览未激活 + 文本框有内容 + 有可播谱面 才启用。"""
        has_text = bool(self._record_view.toPlainText().strip())
        ready = (
            not self._recording
            and self._worker is None
            and self._playback_score is not None
            and has_text
        )
        if self._play_mode:  # 试听模式中入口按钮已隐藏，状态由控制条自己管
            return
        self._btn_play.setEnabled(ready)
        self._btn_load_score.setEnabled(not self._recording)
        if ready:
            self._btn_play.setToolTip("从头试听谱面（预览需处于停止状态）")
        elif self._worker is not None:
            self._btn_play.setToolTip("先点「停止预览」关闭预览，才能试听")
        else:
            self._btn_play.setToolTip("需要先录制或读取一份谱面")

    def _load_score_file(self) -> None:
        """从磁盘读取谱面（自动识别 YQ_moTools JSON / BetterGI 曲谱），可直接试听。"""
        if self._recording:
            self._flash_status("录制中不能读取文件，请先停止录制")
            return
        start_dir = self._score_dialog_dir()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "读取谱面",
            start_dir,
            "谱面文件 (*.json);;YQ_moTools 谱面 (*.json);;BetterGI 曲谱 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        instrument = ""
        source = "yqrec"
        try:
            record = load_record(path)
            if bgi_sheet.is_bgi_sheet(record):
                source = "bgi"
                score = bgi_sheet.parse_sheet(record)
                text = tick_text_from_score(score)  # 显示用：onset→毫秒文本
                instrument = bgi_sheet.local_instrument_id(
                    str(record.get("instrument") or "")
                ) or ""
            else:
                score = score_from_record(record)
                text = record_tick_text(record)
                instrument = str((record.get("instrument") or {}).get("id", ""))
            if not text.strip():
                raise ValueError("文件里没有可显示的谱面内容")
        except Exception as exc:
            _log.warning("读取谱面失败: %s", exc)
            QMessageBox.warning(self, "读取失败", f"无法解析该文件：\n{exc}")
            return

        self._stop_playback()
        if instrument and instrument in self._instruments and instrument != self._spec.id:
            index = self._combo_instrument.findData(instrument)
            if index >= 0:
                self._combo_instrument.setCurrentIndex(index)  # 会切乐器并停预览

        self._record_result = None  # 不是本次录制（tick 落盘时会用文本重建，不丢内容）
        self._playback_score = score
        self._set_record_text(text)  # 内部会按文本启用「保存文件」
        self._remember_score_dir(Path(path).parent)
        name = Path(path).stem
        label = "BetterGI 谱面" if source == "bgi" else "谱面"
        self._lbl_record_info.setText(f"已读取{label} {name} · {score.note_count} 个音")
        self._lbl_record_info.setToolTip(f"{label}：{path}")
        self._flash_status(f"已读取{label}：{path}（{score.note_count} 个音）")

    def _make_media_button(self, kind: str, tip: str, *, repeat: bool = False) -> QPushButton:
        """试听控制条的图标按钮：28×24、图标 18px、无内边距。

        `repeat=True` 才开自动重复（只有快退/快进需要"按住连续走"）。
        播放/暂停**绝不能**开重复：按住稍久（>420ms）就会连续 toggle，
        看起来就是"点一下退出了试听模式、又切回来"。
        """
        button = QPushButton()
        button.setObjectName("Media")
        button.setIcon(media_icon(kind, size=18, colors=theme.media_colors()))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(28, 24)
        button.setToolTip(tip)
        button.setAutoRepeat(bool(repeat))
        if repeat:
            button.setAutoRepeatDelay(420)
            button.setAutoRepeatInterval(90)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 别抢文本框焦点，高亮要跟着走
        return button

    def _toggle_playback(self) -> None:
        """入口按钮：进入试听模式（立即开始播放）。"""
        if self._play_mode:
            self._stop_playback()
            return
        score = self._playback_score
        if score is None or not score.events:
            self._flash_status("没有可试听的谱面")
            return
        self._playback.load(score, self._spec)
        self._flash_status("正在加载采样并开始试听…")
        self._enter_play_mode(0)

    def _enter_play_mode(self, start: int = 0) -> None:
        """切到试听模式：入口按钮原位换成控制条，并从头（或指定和弦）开始播。"""
        self._play_mode = True
        self._paused = False
        self._paused_at = self._cursor = max(0, int(start))
        self._btn_play.hide()
        self._play_bar.show()
        self._highlight_at(self._cursor)
        if not self._playback.play_from(self._cursor):
            self._set_error("试听失败：谱面为空")
            self._exit_play_mode()
            return
        self._sync_play_buttons()
        self._refresh_action_emphasis()

    def _exit_play_mode(self) -> None:
        """退出试听模式：控制条换回入口按钮，清高亮。"""
        self._play_mode = False
        self._paused = False
        self._paused_at = self._cursor = 0
        self._play_bar.hide()
        self._btn_play.show()
        self._record_view.setExtraSelections([])
        self._refresh_playback_enabled()
        self._refresh_action_emphasis()

    def _sync_play_buttons(self) -> None:
        """按当前状态刷新控制条：播放中显示暂停图标，暂停时才放开快退/快进。"""
        playing = self._play_mode and not self._paused
        colors = theme.media_colors()
        self._btn_toggle_play.setIcon(
            media_icon("pause" if playing else "play", size=18, colors=colors)
        )
        self._btn_toggle_play.setToolTip(
            "暂停（暂停后可快退/快进）" if playing else "从当前高亮处继续播放"
        )
        self._btn_rewind.setEnabled(self._play_mode and self._paused)
        self._btn_forward.setEnabled(self._play_mode and self._paused)

    def _toggle_pause(self) -> None:
        """播放 ⇄ 暂停。继续时：位置没动就无缝 resume，动过就 seek 到新位置。"""
        if not self._play_mode:
            return
        if not self._paused:
            self._playback.pause()
            self._paused = True
            self._paused_at = self._cursor
            self._sync_play_buttons()
            self._highlight_at(self._cursor)  # 暂停后高亮换成 accent-soft（"定位"语义）
            total = len(self._tick_spans)
            self._flash_status(f"已暂停（{self._cursor + 1}/{total}）— 可快退/快进后继续")
            return

        if self._cursor != self._paused_at:
            self._playback.play_from(self._cursor)  # 位置被移动过 → 从新位置重开
        elif not self._playback.resume():
            self._playback.play_from(self._cursor)  # 播放器已被回收 → 兜底重开
        self._paused = False
        self._paused_at = self._cursor
        self._highlight_at(self._cursor)
        self._sync_play_buttons()

    def _step_cursor(self, delta: int) -> None:
        """暂停状态下移动高亮（只动高亮，不发声）。"""
        if not self._play_mode or not self._paused or not self._tick_spans:
            return
        target = max(0, min(self._cursor + int(delta), len(self._tick_spans) - 1))
        if target == self._cursor:
            return
        self._cursor = target
        self._highlight_at(target)
        total = len(self._tick_spans)
        self._flash_status(f"定位到第 {target + 1}/{total} 个和弦（点播放从这里继续）")

    def _stop_playback(self) -> None:
        """停止试听：停播放器 + 退出试听模式（控制条换回播放试听）。"""
        self._playback.stop()
        self._exit_play_mode()

    def _on_playback_state(self, state: str) -> None:
        """播放线程 → 主线程（Qt 队列信号）。"""
        if not self._play_mode:
            return
        if state == "playing":
            self._paused = False
            self._sync_play_buttons()
            return
        if state == "paused":
            self._paused = True
            self._sync_play_buttons()
            return
        # idle：自然播完（或被内部 stop）→ 退出试听模式
        self._exit_play_mode()
        self._flash_status("试听结束")

    def _highlight_note(self, index: int) -> None:
        """播放回调：把正在播的和弦高亮并滚动到可见。"""
        if not self._play_mode:
            return
        self._cursor = index
        self._highlight_at(index)

    def _highlight_at(self, index: int) -> None:
        """在文本框里高亮第 index 个和弦（播放与手动定位共用，不发声音）。

        两套语义分开：**正在播放 = accent 实心**；**暂停后定位 = accent-soft 底**
        （原实现只有一个蓝色，暂停/移动高亮看起来跟播放一样）。
        """
        if index < 0 or index >= len(self._tick_spans):
            return
        start, end = self._tick_spans[index]
        document = self._record_view.document()
        cursor = QTextCursor(document)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)

        locating = self._paused
        if locating:
            background = theme.color("accent-soft")
            foreground = theme.color("accent-text")
        else:
            background = theme.color("accent")
            foreground = theme.color("accent-fg")

        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(background)
        selection.format.setForeground(foreground)
        self._record_view.setExtraSelections([selection])
        # 再兜一层：无焦点窗口里系统选区色是灰白，会把自定义底盖掉 → 顺手把 Highlight 调成同色
        palette = self._record_view.palette()
        palette.setColor(QPalette.ColorRole.Highlight, background)
        palette.setColor(QPalette.ColorRole.HighlightedText, foreground)
        self._record_view.setPalette(palette)

        # 光标放到高亮末尾但**不选中**：选中态会用系统高亮色盖掉上面设的底色
        # （窗口没焦点时是灰白，看起来像"没高亮"）。滚动仍靠这个光标位置。
        caret = QTextCursor(document)
        caret.setPosition(end)
        self._record_view.setTextCursor(caret)
        self._record_view.ensureCursorVisible()

    # -- 应用设置（窗口置顶） ---------------------------------------------- #

    def _load_app_config(self) -> AppConfig:
        """读 `%APPDATA%\\YQ_moTools\\settings.json`；缺失/损坏都退回默认值。"""
        try:
            return load_config()
        except Exception as exc:
            _log.warning("读取应用设置失败，使用默认值: %s", exc)
            return default_config()

    def _save_app_config(self) -> None:
        try:
            save_config(self._config)
        except Exception as exc:
            _log.warning("保存应用设置失败: %s", exc)
            self._set_error(f"保存设置失败：{exc}")

    # -- 窗口尺寸记忆（用户要求：缩放过就沿用上次大小） -------------------- #

    def _clamp_window_size(self, width: int, height: int) -> tuple[int, int]:
        """把记忆下来的尺寸夹进"当前屏幕能装下、且不小于最小可用尺寸"的范围。

        换显示器/改分辨率后旧尺寸可能比屏幕还大，直接 `resize` 会有一半跑到屏幕外。
        """
        hint = self.minimumSizeHint()
        min_w = max(int(hint.width()), MIN_WINDOW_WIDTH)
        min_h = max(int(hint.height()), MIN_WINDOW_HEIGHT)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            max_w = max(min_w, available.width() - 40)
            max_h = max(min_h, available.height() - 40)  # 留标题栏/任务栏余量
        else:  # 无屏幕信息（离屏测试）时不设上限
            max_w, max_h = max(min_w, width), max(min_h, height)
        return min(max(int(width), min_w), max_w), min(max(int(height), min_h), max_h)

    def _restore_window_size(self) -> None:
        """启动时套用上次的窗口尺寸（非法值回退默认，最大化状态单独记）。"""
        ui = self._config.ui
        width = int(ui.window_width or DEFAULT_WINDOW_WIDTH)
        height = int(ui.window_height or DEFAULT_WINDOW_HEIGHT)
        width, height = self._clamp_window_size(width, height)
        self.resize(width, height)
        self._restore_window_position(int(ui.window_x), int(ui.window_y), width, height)
        if ui.window_maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        self._ui_ready = True

    def _schedule_window_size_save(self) -> None:
        """拖动窗口时防抖：连续 resize 只在停手后写一次盘。"""
        if not self._ui_ready:
            return
        if self._size_timer is None:
            self._size_timer = QTimer(self)
            self._size_timer.setSingleShot(True)
            self._size_timer.timeout.connect(self._persist_window_size)
        self._size_timer.start(WINDOW_SIZE_DEBOUNCE_MS)

    def _persist_window_size(self) -> None:
        """把当前窗口尺寸/位置/最大化状态写进 `settings.json`（值没变就不落盘）。"""
        ui = self._config.ui
        maximized = bool(self.isMaximized())
        changed = ui.window_maximized != maximized
        ui.window_maximized = maximized
        if not maximized and not self.isMinimized():
            width, height = self._clamp_window_size(self.width(), self.height())
            if (ui.window_width, ui.window_height) != (width, height):
                ui.window_width, ui.window_height = width, height
                changed = True
            if (ui.window_x, ui.window_y) != (self.x(), self.y()):
                ui.window_x, ui.window_y = int(self.x()), int(self.y())
                changed = True
        if changed:
            self._save_app_config()

    # -- 应用设置（窗口置顶 / 尺寸） ---------------------------------------- #

    def _apply_on_top_state(self, on: bool) -> None:
        """同步「置顶」的三处表现：窗口标志 / 按钮勾选 / 图标与提示（**不写盘**）。"""
        self._btn_ontop.blockSignals(True)
        self._btn_ontop.setChecked(on)
        self._btn_ontop.blockSignals(False)
        self._btn_ontop.setIcon(
            pin_icon(
                on,
                colors=(theme.token("accent-text"), theme.token("text-secondary")),
            )
        )
        self._btn_ontop.setToolTip(
            f"窗口置顶：{'开' if on else '关'}（点击切换，下次启动沿用这次的选择）"
        )
        self._set_window_on_top(on)
        self._sync_title_bar()  # 改窗口标志会重建原生句柄，标题栏属性要重新设

    def _set_window_on_top(self, on: bool) -> None:
        """改窗口标志；Qt 改标志会把窗口隐藏，所以要在可见时补一次 show 并还原位置。"""
        flags = self.windowFlags()
        if bool(flags & Qt.WindowType.WindowStaysOnTopHint) == on:
            return
        geometry = self.geometry()
        visible = self.isVisible()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setGeometry(geometry)
        if visible:
            self.show()

    def _toggle_on_top(self, checked: bool) -> None:
        on = bool(checked)
        self._config.ui.always_on_top = on
        self._apply_on_top_state(on)
        self._save_app_config()
        self._flash_status("窗口已置顶：始终显示在其他窗口之上" if on else "已取消置顶")

    # -- 标定持久化 -------------------------------------------------------- #

    def _save_profile(self) -> None:
        """把区域 + 键位 + 阈值写入 `%APPDATA%\\YQ_moTools\\profiles\\default.json`。"""
        if self._suppress_save or self._region is None:
            return
        left, top, width, height = self._region
        monitor = self._monitor_index(left + width // 2, top + height // 2)
        try:
            save_profile(
                Profile(
                    name=PROFILE_NAME,
                    target=ScreenTarget(monitor=monitor, rect=Rect(left, top, width, height)),
                    instrument=self._spec.id,
                    backend=str(self._combo_backend.currentData() or "hdr"),
                    ref_size=(width, height),
                    keygrid=self._grid,
                    thresholds=dict(self._thresholds or {}),
                    baseline=(
                        None
                        if self._baseline is None
                        else tuple(
                            (int(v[0]), int(v[1]), int(v[2]))
                            for v in np.asarray(self._baseline, dtype=np.float32)
                        )
                    ),
                )
            )
        except Exception as exc:
            _log.warning("保存标定失败: %s", exc)
            self._set_error(f"保存标定失败：{exc}")

    def _schedule_save(self) -> None:
        """防抖落盘（拖灵敏度时避免狂写盘）。"""
        if self._suppress_save:
            return
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._save_profile)
        self._save_timer.start(SAVE_DEBOUNCE_MS)

    def _load_profile(self) -> None:
        """启动时恢复上次标定；文件不存在/损坏都静默忽略。"""
        try:
            profile = load_profile(PROFILE_NAME)
        except Exception as exc:
            _log.info("没有可用的历史标定（%s）", exc)
            return
        self._suppress_save = True
        try:
            if profile.instrument in self._instruments and profile.instrument != self._spec.id:
                index = self._combo_instrument.findData(profile.instrument)
                if index >= 0:
                    self._combo_instrument.setCurrentIndex(index)
            if isinstance(profile.target, ScreenTarget):
                rect = profile.target.rect
                self._region = (rect.left, rect.top, rect.width, rect.height)
                self._update_region_label()
            # 后端**不从标定档恢复**：它是"上次用的截图方式"，存在 settings.json；
            # 标定档里的 backend 只记录标定当时用了什么，拿来覆盖会让用户每次启动被拽回旧值。
            if profile.thresholds:
                self._thresholds = dict(profile.thresholds)
                self._refresh_channel_by_area()
            if profile.baseline is not None:
                self._baseline = np.asarray(profile.baseline, dtype=np.float32)
                # 判据（metric/weights/死区）可能随 JSON 换代了 → 用当前 spec 重算阈值，
                # 不能直接把旧档里存下来的 mode/weights 拿来用。
                self._recompute_thresholds()
                self._keys_panel.set_baseline(
                    self._baseline, self._channel_by_area, self._thresholds
                )
            else:
                # 旧配置档没有逐键基线：判据换代后旧阈值不再可信，宁可让用户重学 1.5s
                self._thresholds = {}
                self._refresh_channel_by_area()
            if profile.keygrid is not None:
                self._set_grid(profile.keygrid)
            if profile.baseline is None and profile.keygrid is not None:
                self._keys_panel.set_baseline(None, self._channel_by_area, {})
        except Exception as exc:
            _log.warning("恢复标定失败: %s", exc)
        finally:
            self._suppress_save = False
        if self._region is not None and self._grid is not None:
            left, top, width, height = self._region
            tail = "，判按已就绪" if self._baseline is not None else "；请重新「学习基线」"
            self._flash_status(
                f"已恢复上次标定（区域 {left},{top},{width},{height}，"
                f"{self._grid.key_count} 个键位）{tail}"
            )

    # -- 生命周期 ---------------------------------------------------------- #

    def _restore_window_position(self, x: int, y: int, width: int, height: int) -> None:
        """套用上次的窗口位置；跑到屏幕外（换显示器/拔屏）就拉回主屏居中。"""
        if x < 0 and y < 0:
            return
        screens = QGuiApplication.screens()
        visible = any(
            s.availableGeometry().intersects(QRect(x, y, width, height)) for s in screens
        )
        if visible:
            self.move(x, y)
            return
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return
        avail = primary.availableGeometry()
        self.move(
            avail.x() + max(0, (avail.width() - width) // 2),
            avail.y() + max(0, (avail.height() - height) // 2),
        )

    def moveEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        """移动窗口也要记住位置（和尺寸共用防抖定时器）。"""
        super().moveEvent(event)
        self._schedule_window_size_save()

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        """首次显示时补一次标题栏同步（构造期 `winId()` 还不是最终句柄）。"""
        super().showEvent(event)
        self._sync_title_bar()

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        """窗口缩放：浮层重定位 + 状态栏密度 + 记下新尺寸（防抖落盘）。"""
        super().resizeEvent(event)
        self._position_edit_panel()
        self._refresh_status_compact()
        self._schedule_window_size_save()

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt 签名
        if self._size_timer is not None:
            self._size_timer.stop()
        self._persist_window_size()  # 关窗前立刻落盘，不等防抖
        self._close_edit_panel()
        self._stop_playback()
        self._playback.close()
        self._stop_preview()
        super().closeEvent(event)


def instrument_icon() -> QIcon | None:
    """应用/窗口图标：`assets/icons/mo_motools.ico`（打包后由 datas 提供）。"""
    path = assets_dir() / "icons" / "mo_motools.ico"
    return QIcon(str(path)) if path.exists() else None
