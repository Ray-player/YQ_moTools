# CONTRACTS · 接口契约 v1.45（M0 冻结 + P1/P2 加法增补）

> 状态：**v1.45**（M2 录制链路 + UI 设置 + 试听 + 路径记忆 + BetterGI 互转/yuanqin + tick 毫秒制 + 录制期只读 + 窗口尺寸/位置记忆 + chroma 判据 + 外环采样 + 判据分派修复 + 检测方式切换 + 全局采样几何与配置热重载 + 分段点采样 / weighted 判据 / 逐键基线 / 半径滑条写回 JSON + **乐器级 `sustain_ms` 延音录制 / 谱面 `[ms]` 后缀 / confirm_frames 默认 2 / 乐器级 `preview_only`（下拉可见不可选）+ **试听进度条 `PlayProgress` / 拖动跳转 / 控制器 `position_ms·duration_ms·ordinal_at`** + **按键演奏 `KeyPlayer` / 前后台键注入 / `ui.key_play_mode`（M6）**）。变更流程：**升版本号 → 广播受影响 owner → 同步改测试 → 本文件记变更**。
> 详细语义见 [03 · 实现规格](03-实现规格与接口契约.md)；本文只列"谁必须提供什么"。

---

## 1. 错误

```python
# src/yqrec/errors.py
class ErrorCode(StrEnum): CAPTURE_OPEN / CAPTURE_BLACK / CAPTURE_TIMEOUT /
    KEYGRID_MISMATCH / CALIB_LOW_SNR / PARSE / EXPORT / CONFIG / NOT_IMPLEMENTED
class YQRecError(Exception):  code, message, detail
class CaptureError(YQRecError): ...
class ParseError(YQRecError): ...
class KeyGridMismatch(YQRecError, ValueError): ...
class NotImplementedYet(YQRecError): ...   # M0 占位，调用即报
```

## 2. 数据类型（`src/yqrec/types.py`）

| 类型 | 关键字段 |
|---|---|
| `Rect` | `left/top/width/height`；`right/bottom/aspect`；宽高 > 0 |
| `ScreenTarget` | `monitor, rect`（绝对屏幕坐标） |
| `WindowTarget` | `hwnd, norm(u0,v0,u1,v1), title?` |
| `CaptureStats` | `fps, dropped, last_error` |
| `KeyThreshold` | `min_r/max_r/min_g/max_g/min_b/max_b`；`.accepts(rgb, channels)`（通道 AND）。**v1.2 追加**：`mode("channel"\|"max_drop", 默认 channel)/base_r/base_g/base_b/delta` + `.max_drop(rgb)`；`max_drop` 模式下判 `max(base-r,g,b) >= delta` |
| `KeyArea` | `code, name, channels, keys` |
| `KeyPoint` | `u, v, r`（r 以 1920 宽归一化）；`.to_pixels(w,h)` |
| `KeyGrid` | `points, key_count`；`len(points) == key_count` |
| `Circle` | `x, y, r` |
| `InstrumentSpec` | `id/name/key_count/sustain/areas/note_to_key/note_to_audio/hough/button_radius/thresholds/detection/sustain_ms/preview_only`；`note_order/key_order/area_of_key/threshold_of_key`。**v1.35**：新增 `sustain_ms:int`（0=不延音，>0=延音阈值毫秒，唯一真相）；`sustain:bool` 保留但由它归一化（旧的 `sustain:true` → `LEGACY_SUSTAIN_MS=200`） |
| `RgbSample` | `ts, rgb(K,3)` |
| `PressEvent` | `key, down, ts`；**v1.43**：`delta=0`（按下时的判据量 Δ，与 `KeyThreshold.delta` 同口径；抬起恒 0；不参与时间轴） |
| `NoteEvent` | `keys, onset_ms, sustain_ms=0` |
| `RestEvent` | `ms` |
| `Diagnostic` | `message, offset, length, level` |
| `Score` | `events, metadata, diagnostics` |
| `Profile` | `name, target, instrument, backend, ref_size, keygrid, thresholds, created` |
| `SessionMeta` | `version, target, instrument, backend, fps, dropped, started_at, duration_ms` |

## 3. 捕获（`src/yqrec/capture/`）

```python
class CaptureBackend(Protocol):
    name: str
    @classmethod is_available() -> bool
    def supports(target) -> bool
    def open(target, *, target_fps=60) -> None
    def latest(timeout_ms=100) -> tuple[np.ndarray, float] | None   # BGR uint8, perf_counter 秒
    def close() -> None
    def stats() -> CaptureStats

class CaptureManager:
    def candidates(target) -> list[type]
    def probe(target, frames=30) -> list[ProbeResult]
    def start(target, *, prefer=None, target_fps=60) -> CaptureBackend
    def latest(timeout_ms=100)
    def stop() -> None
    def stats() -> CaptureStats
    def on_degraded(cb: Callable[[str], None]) -> None
    def set_file_source(path: str) -> None       # 回归测试
```

后端与目标：`dxgi → ScreenTarget`（首选）、`gdi → ScreenTarget`（兜底）、`wgc → WindowTarget`（首选）、
`printwindow → WindowTarget`（**v1.1 已实现**）、`file → ScreenTarget`（读 mp4/PNG，仅回归）。

> **v1.1 追加**：`BaseBackend.latest()` 对子类抛出的 `CaptureError` **透传原错误码**
> （不再包装成 `CAPTURE_OPEN`），同时仍计入 `dropped` 与 `last_error`。
> `printwindow_backend` 额外提供纯函数 `roi_from_norm(width, height, norm) -> (x, y, w, h)` 与
> `make_bitmap_info(width, height)` 供无窗口单测。

## 4. 视觉（`src/yqrec/vision/`）

```python
def detect_keys(frame, hough: dict, key_count: int) -> list[Circle]        # 需 cv2（Hough 兜底）
def detect_keys_color(frame, spec) -> list[Circle]                          # v1.2：颜色分割+圆度+7×3 网格拟合
def correct_button_coordinates(circles) -> list[Circle]                     # 7 的倍数且≥14；输出下→上
def circle_map_note(circles, spec) -> dict[str, Circle]                     # 数量不符抛 KeyGridMismatch
def keygrid_from_circles(circles, spec, w, h) -> KeyGrid                    # 内部按音名排序
def keygrid_from_note_map(mapping, spec, w, h) -> KeyGrid
def keygrid_from_points(points_px, spec, w, h) -> KeyGrid                   # 手动点选兜底

class KeySampler(grid, frame_w, frame_h, *, stride=4, inner_ratio=0.0,
                 square_side=DEFAULT_SQUARE_SIDE, offset_y=0.0):
    def sample(frame) -> np.ndarray(K,3) uint8 RGB                          # 尺寸变化抛 ValueError
    def sample_tuple(frame) -> tuple[tuple[int,int,int], ...]
    # v1.16：inner_ratio>0 只统计 inner_ratio·R ~ 0.98R 的圆环（跳过键帽中心图标）
    # v1.18：offset_y 是**1920 宽基准**下的像素，内部按 frame_w/1920 缩放后整体下移采样窗
    # v1.19：square_side 是**方块边长 × R**（半边长 = side/2，默认 1.0；M2p 之前是 2.0）。
    #        inner_ratio>0（环形）时它无效 —— 环形的 ROI 仍是 ±R，靠掩膜裁。

class SamplingSpec(ring_width_ratio=0.43, square_side_ratio=1.0):        # types.py，全局口径
    ring_inner_ratio -> float        # = clamp(0.98 - ring_width_ratio, 0, 0.98)
    square_half_ratio -> float       # = square_side_ratio / 2

instruments: load_sampling(path=None) -> SamplingSpec                    # 按 (mtime,size) 缓存重读
             instruments_mtime() -> int                                  # UI 轮询用
             instrument_deltas(path=None) -> dict[str, int]              # {id: Δ} 调试用

def calibrate(baseline(F,K,3), pressed(F,K,3), spec) -> CalibrationResult
    # CalibrationResult: thresholds / channel_by_area / deltas / low_snr_keys

def thresholds_from_baseline(baseline(K,3), spec, delta) -> dict[str, KeyThreshold]
    # v1.2：产出 mode="max_drop" 的阈值（三通道最大跌幅 ≥ delta），不再依赖单通道亮度下降
```

> **检测优先级（v1.2）**：`detect_keys_color` 优先（真实游戏画面/视频截图），
> 失败再回退 `detect_keys`(Hough) + `correct_button_coordinates`；两者都失败只提示，不自动改手动。
> 真图回归：`tests/data/real/lyre_1696x957.jpg` → 必须检出 21 圆且按下集合 `{+2, 7}`。

> **采样口径（v1.19）**：几何来自**全局** `sampling` 段（所有乐器共用，改 JSON 后 1.5s 内热生效）：
> 界面「检测」下拉选 `ring` → `inner_ratio = SamplingSpec.ring_inner_ratio`（0.98 − 环宽）；
> 选 `square` → `inner_ratio = 0` 且 `square_side = SamplingSpec.square_side_ratio`（边长 × R，默认 1.0）。
> 「偏移」滑条只在 `square` 生效，值以 **1920 宽为基准**存盘（`ui.square_offset_y`，默认 0，范围 ±80）。
> 换模式/挪偏移都会改变采样像素 → 基线作废，UI 只提示"请重新学习基线"，不自动清空。
> **单位不一致是有意的**：`KeySampler.offset_y` 收 1920 基准值（自己缩放），
> `PreviewWidget.show_sample_overlay(offset_y=…)` 收**帧像素**（渲染层只管画），
> 换算在 `main_window._show_sample_overlay()` 里做一次。
```

## 5. 时间线（`src/yqrec/timeline/`）

```python
class DetectorConfig: min_hold_ms=25.0, min_gap_ms=30.0, hysteresis=0.15
class PressDetector(spec, thresholds=None, cfg=None):
    def update(rgb(K,3), ts) -> list[PressEvent]
    def flush(ts) -> list[PressEvent]
    def reset() -> None

class AssembleConfig: chord_window_ms=25.0, min_rest_ms=1.0, scene_guard=True
def assemble(presses, spec, cfg=None) -> list[ScoreEvent]
    # v1.35：和弦 sustain_ms = max(达到 spec.sustain_ms 阈值的 hold)；不足阈值记 0；
    #        值为**完整按住时长**（不是超出阈值的部分）；spec.sustain_ms==0 恒 0
```

## 6. 格式（`src/yqrec/formats/`）

```python
@dataclass FormatInfo: id, name, ext, can_read, can_write
class Formatter(Protocol):
    info: FormatInfo
    def parse(text) -> Score
    def format(score) -> str
def register(cls) ; def get(fmt_id) ; def all_formats()

tick.py:  TickFormat（@register，id="tick"）；便捷函数 tick.parse / tick.format
bgi_js.py: build_events(score, default_hold_ms=50) -> list[dict]
           export(score, out_dir, *, name=None, instrument=None, countdown_ms=3000) -> Path
fengwu.py / domiso.py: 只读（P2 实现，当前 raise NotImplementedYet）
```

## 7. 播放（`src/yqrec/player/`）

```python
class PlayerState(StrEnum): IDLE / PLAYING / PAUSED
class AudioEngine(spec, *, assets_dir, enabled=True, volume=0.8):
    preload() / play(note) / stop_all() / close()
class Player(score, *, audio=None, speed=1.0, note_of_key=None):
    play(start_ms=0, *, loop=False) / pause() / stop() / seek(ms) / set_speed(x) / close()
    on_note(event, index) / on_state(state) / on_progress(ms)

# v1.45（M6）：按键演奏 —— 与 Player **同接口**，只是把音符换成键盘按下/抬起
class KeyAction: at_ms / down / key / event_index
class KeyTimeline: actions(已排序、同键不交叠) / duration_ms / note_count / key_count
build_key_timeline(score, *, min_hold_ms=60) -> KeyTimeline      # 纯函数
class KeyPlayer(score, sink, *, speed=1.0, min_hold_ms=60):
    play(start_ms=0, *, loop=False) / pause() / stop() / close() / set_speed(x)
    on_note(event, index) / on_state(state) / on_progress(ms) / on_interrupt(reason)
    duration_ms / timeline / keys_used()
# sink 由 inputsim 提供，需实现：key_down(key)/key_up(key)/release_all()/ready()
#                                  [+ 可选 force_release(keys) 收尾补抬起]
```

**按键演奏的时间轴约定（v1.45）**：

| 规则 | 语义 |
|---|---|
| 按住时长 | `max(note.sustain_ms, min_hold_ms)`；`min_hold_ms` 默认 60ms（0ms 的按在 60fps 游戏里可能整帧跳过） |
| 同键交叠 | 相邻两段**真交叠**（`end > next.start`）时，前一段提前到 `next.start - 30ms` 抬起；"刚好相接"不砍 |
| 同刻次序 | 稳定排序，`down=False` 排前面（同一毫秒既有抬起又有按下时先松后按） |
| 中途起播 | `_held_at(start_ms, strict=True)` 补按"起播瞬间仍应按着"的键（延音段中间起播才会出声） |
| 暂停 | `sink.release_all()` + 时间轴冻结；恢复时补按 `_last_at_ms` 时刻的按住的键 |
| 收尾 | `release_all()` 之外再 `force_release(keys_used())` —— 真实游戏里漏一个 up 就是琴键卡死，多余 up 无害 |

## 8. 会话 / 配置

```python
class Recorder(spec, *, detector_cfg=None, assemble_cfg=None, thresholds=None):
    push(ts, rgb) -> list[PressEvent] ； finish() -> Score
    # v1.1：thresholds=None 为加法参数（配置档标定阈值；None 用 spec 内置），原调用不变

class Session(spec, *, manager=None, sampler_factory=None):
    state: SessionState                      # IDLE / RECORDING / EDIT
    def start_record(target, *, keygrid=None, thresholds=None,
                     backend="auto", target_fps=60, on_preview=None) -> None
    def stop_record() -> Score               # 可重复调用：第二次起返回同一 Score 对象
    def on_degraded(cb) -> None
    def calibrate() -> None                  # P4 占位（NotImplementedYet）
    # 只读快照：stats / press_count / black_frames / ever_black / frame_count / duration_ms

config: AppConfig / load_config / save_config / load_profile / save_profile / config_path()
instruments: load_instruments() / load_instrument(id) / assets_dir()
```

**应用设置（v1.6）**：`%APPDATA%\YQRec\settings.json` 的 `ui` 段新增 `always_on_top`（默认 `true`），
主窗启动时读取并应用窗口置顶，切换时立即写盘。

**窗口尺寸记忆（v1.14）**：`ui` 段新增 `window_width` / `window_height`（v1.23 起默认 **558×913**，
即用户实机调好的窄长条；早期默认 660×980）与 `window_maximized`（默认 `false`）。缩放窗口时 400ms 防抖落盘、关窗前立即落盘；启动时读取并
`_clamp_window_size()` 夹进「不小于布局最小尺寸、不大于当前屏幕可用区域」的范围。
> 最大化时只记标志、不覆盖普通尺寸；值没变不重复写盘。

**窗口位置记忆（v1.18）**：`ui` 段再增 `window_x` / `window_y`（v1.23 起默认 **1393/50**＝主屏右下贴边；
更早默认 `-1`＝没记过，由系统决定位置）。
`_restore_window_position()` 用 `availableGeometry().intersects(QRect(x,y,w,h))` 判"还在屏幕上"，
是则 `move()`，否则主屏居中（换显示器/拔屏不会让窗口落在看不见的地方）。
`moveEvent` 与 `resizeEvent` 共用一个 400ms 防抖 timer，`closeEvent` 立即落盘；最大化时不记位置。

**路径记忆（v1.8）**：`paths.last_score_dir`（默认空串）——保存/读取谱面对话框的起始目录。
每次成功保存/读取后写入，启动时读取；目录被删或为空则退回 `<文档>\YQRec`。
> 修 bug：`AppConfig.to_dict()` 原来用 `vars()` 序列化 `slots=True` 的子配置（无 `__dict__`），
> `save_config` 一直会抛 `TypeError`；v1.6 改用 `dataclasses.asdict`。

**图标（v1.6）**：`ui/icons.py::pin_icon(active, *, size=20) -> QIcon` —— 运行时用 QPainter 画的
矢量图钉（24×24 网格、45° 斜置、4× 超采样），激活白 / 未激活灰蓝；不依赖外部图片资源。

**试听（v1.7）**
```python
# src/yqrec/ui/playback.py（GUI 桥；Player 在后台线程，回调必须经 Qt Signal 回主线程）
class PlaybackController(QObject):
    note_played = Signal(int)      # 第几个**和弦**（与 tick 文本 token 同序，不是 events 下标）
    state_changed = Signal(str)    # "playing" / "idle"
    interrupted = Signal(str)      # v1.45：按键演奏被迫中断（焦点跑了 / 窗口关了）
    load(score, spec, *, mode="audio", sink=None) / play() -> bool / stop() / close()
    # 首次 play 才 preload 21 个采样（同乐器复用；换乐器重建）
    # v1.25：play_from(ordinal) / pause() / resume() / onset_of(ordinal) / paused
    # v1.42：position_ms（播放头，暂停冻结）/ duration_ms（末音 onset+sustain）/ ordinal_at(ms)
    # v1.45：mode("audio"|"keys") / set_sink(sink)；keys 模式的 duration_ms 取 KeyPlayer 时间轴总长

# src/yqrec/recording.py（v1.7 追加，供高亮定位）
scan_tick_tokens(text) -> list[TickToken]   # keys / gap / start / end
tick_spans(text) -> list[tuple[int, int]]   # 每个和弦在文本里的字符区间
```

> `Player.on_note` 给的是 `Score.events` 下标（含 RestEvent），控制器内部按
> `ordinal_of_event` 换成和弦序号，UI 才能直接用 token 下标做高亮。

**音频资产（v1.7 修正）**：`assets/instruments.json` 的 `note_to_audio` 原为
`audio/YuanShen/<inst>/<note>.mp3`，但实际文件在 `assets/audio/<inst>/`，已统一改成
`audio/<inst>/<note>.mp3`（105 条）。打包必须带上 `assets/audio`（147 mp3，约 5.5MB）
且**不能**再把 `pygame` 放进 `EXCLUDES`。

**Session 语义（v1.1 冻结）**

| 项 | 约定 |
|---|---|
| 状态机 | `IDLE --start_record--> RECORDING --stop_record--> EDIT`（单次录制，EDIT 为终态） |
| 非法转换 | 未 start 就 stop、RECORDING 中再次 start → `YQRecError(CONFIG)` |
| 缺 keygrid / 键数不符 | `YQRecError(CONFIG)`，失败后状态回 `IDLE` 可重试 |
| 首帧超时 | 3.0s 未收到帧 → `CaptureError(CAPTURE_TIMEOUT)`，线程与后端已清理 |
| 分析线程 | 唯一执行 `KeySampler.sample` + `PressDetector.update` 的线程，名 `yqrec-analysis` |
| 帧尺寸变化 | 自动按新尺寸重建 `KeySampler`（stride=4）；重建失败只记 warning |
| 黑帧 | 帧 `max() <= 2` 计黑帧；连续 30 帧 → `ever_black=True`；**不中断录制** |
| 线程异常 | 不抛给 `stop_record`；写入 `Score.metadata["thread_error"]` 并 `log.error` |

## 8.1 CLI（v1.1 追加）

```
yqrec record --source {screen,file,synth} [--rect L,T,W,H] [--input PATH]
             [--profile NAME] [--instrument fwzs] [--backend auto]
             [--duration SEC] [--fps N] [--out PATH] [--json]
```
- `--json` 输出单行：`status(ok|partial) / events / notes / duration_ms / fps / dropped / black_frames / ever_black / out`
- 缺参数或参数非法 → stderr 可读提示 + 退出码 2（不输出 traceback）
- `python -m yqrec` 与 `yqrec` 命令等价（`src/yqrec/__main__.py`）

## 8.2 录制与谱面落盘（v1.3 追加，v1.4 起「谱面只存 tick 文本」，纯逻辑无 Qt）

```python
# src/yqrec/recording.py
@dataclass RecordedNote: keys / onset_ms / onset_frame / gap_ms / sustain_ms
@dataclass TickNote:     keys / gap / sustain_ms=0   # v1.35：`[ms]` 后缀
@dataclass TickToken:    keys / gap / start / end / sustain_ms=0
@dataclass KeySpan:      key / down_ms / up_ms / delta=0   # v1.43：逐键按下区间（**只在内存**）
@dataclass Recording:    score / notes / frames / duration_ms / fps / spans=()
                         .tick_text -> str     # `A[800]1000F`：字母 + `[ms]` 延音 + **毫秒**间隔
                         .press_deltas / .avg_delta / .min_delta   # v1.43：识别质量统计
def tick_text(notes) -> str                        # 写入端（v1.12 起记毫秒；v1.35 加延音）
def parse_tick_text(text) -> list[TickNote]        # 逆运算（字母=和弦，`[ms]`=延音，数字=间隔）
def key_spans(presses, *, last_ts=None) -> tuple[KeySpan, ...]   # v1.43：按下/抬起配对

class RecordingBuffer(spec, chord_window_ms=25.0):
    push_frame(ts)                 # 登记采集帧（worker 线程，持锁）
    push_events(events)            # 追加该帧判出的事件（worker 线程，持锁）
    snapshot() -> dict             # {"text","notes","frames","duration_ms"}，录制中实时刷
    close()                        # 封口：之后 push_* 丢弃（stop 时先调，防收尾竞态）
    last_frame_ts -> float | None  # v1.35：最后一帧时间戳（收尾补抬用）
    finish(extra=()) -> Recording  # 主线程收尾；时间轴以本次录制首帧为 0 点
                                   # v1.35：`extra` 绕过封口追加收尾事件（stop 时 flush 的抬起）

# src/yqrec/scorefile.py
TICK_UNIT_MS = "ms" ; TICK_UNIT_FRAME = "frame"
SCORE_TYPE_TICK = "tick"                          # v1.43：顶层 "type" 的取值
DELTA_AVG_KEY = "Average_Delta" ; DELTA_MIN_KEY = "Min_Delta"   # v1.44（"Avage_Delta" 只读兼容）
DELTA_AVG_KEY_LEGACY = "Avage_Delta"            # v1.43 的笔误键：只读，不再写
build_record(recording, spec, *, name, backend, extra) -> dict
build_record_from_text(text, spec, *, fps, name, backend, delta_stats=None, extra) -> dict
save_record(path, recording, spec, ...) -> Path   # 自动补 .json / 建目录 / UTF-8 不转义
save_record_text(path, text, spec, *, delta_stats=None, ...) -> Path
load_record(path) -> dict
record_default_dir() -> Path                      # <用户>\Documents\YQRec
suggested_filename(prefix="谱面") -> str
# 还原（v1.4）
record_tick_text(record) -> str
record_score_type(record) -> "tick" | ""          # v1.43（旧文件没有这个键）
record_delta_stats(record) -> (avg, min)          # v1.43（旧文件/无数据 = (0, 0)）
record_notes(record) -> list[TickNote]
record_unit(record) -> "ms" | "frame"             # v1.12；缺省 frame（v2 及更早）
ms_per_frame(record) -> float                     # 仅旧帧制文件用
score_from_record(record) -> Score                # tick 文本 → 可播放 Score（onset + 休止）

# 谱面格式标识（v1.5）
DEFAULT_SCORE_FORMAT = "tick"                     # 后续加 "bgi" 等；写进 JSON 也拼进文件名
suggested_filename(score_format="tick", *, prefix="谱面") -> str
                                                  # 谱面_tick_20261020_172400.json
```

**录制路径（重要，勿并列）**：GUI 录制挂在 `CaptureWorker` 里（`start_recording/stop_recording/recording`
+ `record_progress` 信号），**不接 `Session`** —— `Session` 自带捕获后端与分析线程，与预览线程并列会
对同一区域开两份捕获、两份采样。`Session` 只服务 CLI/回归。

**谱面 JSON（`version: 5`, `format: "yqrec.recording"`, `type: "tick"`）**

```jsonc
{
  "version": 5,
  "type": "tick",                  // v5：曲谱类型（与 BetterGI 的 type 同位置同语义）
  "Average_Delta": 40,             // v5：本次识别所有按下的平均 Δ（整数灰阶，0 = 无识别数据）
  "Min_Delta": 24,                 // v5：最小 Δ（最接近阈值的那一次，看余量）
  "format": "yqrec.recording", "score_format": "tick",
  "name": "谱面_tick_20261020_1650",
  "created_at": "2026-10-20T16:50:12+08:00",
  "instrument": { "id": "fwzs", "name": "风物之诗", "key_count": 21 },
  "capture": { "backend": "gdi", "fps": 59.8, "frames": 742, "duration_ms": 12400 },
  "tick": { "unit": "ms", "text": "A[800]1000F", "sustain": true }
}
```

**v5（v1.43）**：`version` 后紧跟 `type` 与两个 Δ 统计（顺序固定：`version → type → Average_Delta
→ Min_Delta`）。Δ 是**判据量**（与 `KeyThreshold.delta` 同口径）：`weighted` 取加权投影值、
`max_drop`/`chroma` 取对应标量、旧 `channel` 模式取通道最大跌幅；取的是**确认帧**的值。
`Average_Delta` 在 **v1.44** 更正过拼写：v1.43 曾误写成 `Avage_Delta`，写盘现在只用正确拼写；读取端（`record_delta_stats`）仍认旧键，那个窗口期存的谱面不会读不出平均 Δ。
逐键按下区间（`KeySpan`）**不落盘**：它是试听回放用的内存数据。

**v4（v1.35）**：`tick.text` 支持 `[ms]` 延音后缀；`tick.sustain` 是"这份谱面含延音"的
标记（旧版 exe 打开 v4 文件不会报错，但会丢延音 —— 使用说明里写明延音谱面用新版打开）。

> **单一数据源**：`tick.text` 就是谱面本身，文件里不再有 `events[]`。
> **v3 起 `tick.unit = "ms"`**：数字直接是毫秒，不用任何 fps；间隔用"累计已输出毫秒"差分，
> 整首误差 ≤0.5ms、不随音数漂移。首和弦前的数字是前导静默（保留）。
> `capture` 块现在**只作记录**（v2 及更早的 `unit: "frame"` 仍按它换算，见 `record_unit()`）。
> `score_format` 标明谱面编码（当前恒 `tick`；加载时缺省按 `tick` 处理）。
> 还原：`score_from_record(load_record(path))` → 可直接喂 `Player`。
> v1 旧文件仍可读（`events[]` 被忽略，谱面一直就在 `tick.text` 里）。
> 与 `formats/tick`（毫秒休止）刻意区分：录制产物一律 JSON，不直接写 `.txt`。

## 8.3 BetterGI（AutoYuanQin）曲谱互转（v1.9 → v1.12）

```python
# src/yqrec/formats/bgi_sheet.py
BGI_TICKS = 480 ; BGI_BPM = 125.0         # 通用导出：125×480=60000 → 1 tick = 1ms
DEFAULT_HOLD_MS = 60.0 ; MIN_REPRESS_GAP_MS = 30.0   # = BGI 的 lowest_latency，走正常分支
BGI_INSTRUMENT_NAMES: dict[str, str]      # fwzs→风物之诗琴 等 5 项

# v1.12：tick（毫秒）→ 两种 BGI 格式（不再有帧率概念）
BGI_BPM=125.0 / BGI_TICKS=480                   # midi 时间基准 → 1 tick = 1ms
YQ_BPM = 300.0                                  # 一拍 200ms
YQ_TYPES = (1,2,3,4,6,8,12,16,24,32,48,64)      # 允许的 [n] 分母
YQ_MAX_HOLD_MS = 100.0                          # 音符最长 100ms

ms_per_tick(bpm, ticks) -> float
bgi_instrument_name(spec) -> str ; local_instrument_id(name) -> str | None
build_events(score, *, default_hold_ms, min_gap_ms) -> list[(time_ms, is_down, key)]
notes_text(score, spec, *, bpm, ticks, default_hold_ms) -> str     # `DA0|UA60|DDF440|...`
build_sheet(score, spec, *, name, author, bpm, ticks, ...) -> dict  # type=midi
save_sheet(path, score, spec, *, name, **kw) -> Path
build_midi_sheet(score, spec, *, name, ...) -> dict                 # 1ms/tick（零量化）
score_from_tick_text(text) -> Score                                # 毫秒文本→Score
yuanqin_notes_text(score, spec, *, bpm, max_hold_ms) -> str
build_yuanqin_sheet(score, spec, *, name, ...) -> dict              # type=yuanqin
build_sheet_typed(score, spec, *, sheet_type, name, **kw) -> dict
sheet_from_tick(text, spec, *, sheet_type, name, **kw) -> dict
save_sheet_typed(path, text, spec, *, sheet_type, name, **kw) -> Path
is_bgi_sheet(data) -> bool
parse_sheet(data) -> Score          # 按 type 分派：midi / yuanqin
parse_yuanqin(data) -> Score        # A[8] / (ASD)[4] / @[2] / %[72] / 4-* 附点 / 16-# 装饰音 / .3 .6 .$ 连音
load_sheet(path) -> Score
class BgiSheetError(ValueError)
```

**token 语义（关键）**：`[D|U]<游戏键名><先等待的 tick 数>` —— 脚本 `sleep` 在 `keyDown/keyUp` **之前**，
所以数字是"本事件之前的间隔"（详见 03 §8.1 的实证）。

**UI**：保存行最左是格式下拉，三项 —— `tick（YQRec）` / `BGI · midi` / `BGI · yuanqin`，
data 为 `"tick"` / `"bgi_midi"` / `"bgi_yuanqin"`；默认文件名段随之变化
（`谱面_tick_*.json` / `谱面_bgi_midi_*.json` / `谱面_bgi_yuanqin_*.json`）；
「读取文件」自动识别三种来源（YQRec JSON / BGI `midi` / BGI `yuanqin`）。

**互校工具（v1.10）**：`tools/compare_sheets.py <A> <B>` —— 输出每份谱面的画像、
`Score→BGI(midi)→Score` 往返误差、跨文件的和弦序列相似度与时间轴拟合。

## 9.1 v1.20 新增/变更（分段点采样 + 加权判据）

```python
# vision/pointsampler.py
def ring_base_points(cx, cy, r, inner_ratio, count) -> list[tuple[float, float]]
def square_base_points(cx, cy, r, square_side, count) -> list[tuple[float, float]]
def base_points(cx, cy, r, *, inner_ratio, square_side, ring_points, square_points) -> list[tuple[float, float]]

class KeyPointSampler:                      # 与 KeySampler 接口对齐
    KeyPointSampler(grid, frame_w, frame_h, *, inner_ratio=0.0,
                    square_side=1.0, offset_y=0.0,
                    ring_points=16, square_points=8, patch_ratio=0.06)
    sample(frame_BGR(H,W,3)) -> np.ndarray  # (K,3) uint8 RGB
    points_of(index) -> list[tuple[float, float]]
    is_point_sampler -> True；point_counts -> (ring_points, square_points)

# types.KeyThreshold
    mode: Literal["channel", "max_drop", "chroma", "euclid", "weighted"]
    weights: tuple[float, float, float] = (1/3, 1/3, 1/3)   # weighted 用
    tolerance: int = 0                                      # weighted 死区
    weighted_score(rgb) -> float                            # Σw·max(0, drop-tol)/Σw

# types.SamplingSpec（新增字段 + 派生）
    ring_points: int = 16；square_points: int = 8；point_patch_ratio: float = 0.06
    with_ring_inner_ratio(inner) -> SamplingSpec            # 滑条：环形
    with_square_half_ratio(half) -> SamplingSpec            # 滑条：方块

# timeline.detector.PressDetector
    PressDetector(spec, thresholds=None, cfg=None, baseline=None)   # baseline=(K,3) 逐键基线

# instruments
    update_sampling(**{ring_width_ratio|square_side_ratio|...}) -> bool   # 文本级写回 JSON
    parse_sampling() → 新增 ring_points/square_points/point_patch_ratio
    parse_detection() → 新增 weights/tolerance，metric 支持 "weighted"

# types.Profile
    baseline: tuple[tuple[int,int,int], ...] | None         # 逐键基线，to_dict/load_profile 双向
```

契约版本 **v1.34**。v1.32~v1.34 只动后端默认值/打包收集与启动时序，配置文件结构不变；
旧配置档（无 `baseline`）自动退回 `thresholds.base_*`；旧 `instruments.json`（无
`ring_points`/`weights`/`tolerance`）按默认值补齐，不影响读入。
v1.26 只动**视觉层**（`ui/theme.py`、`ui/qss.py`、`ui/widgets.py` 与各 UI 文件的绘制/挂档代码），
识别、录制、谱面与配置契约不变；`settings.json` 里的 `ui.theme` 旧值（无该字段）默认按 `light` 读入。

## 9.2 v1.35 新增/变更（延音录制 M4）

```python
# types.py
DEFAULT_CONFIRM_FRAMES = 2          # DetectionSpec.confirm_frames 默认 1 → 2
DEFAULT_SUSTAIN_MS = 0；LEGACY_SUSTAIN_MS = 200；SUSTAIN_TEXT_MIN_MS = 50
class InstrumentSpec: ...; sustain_ms: int = 0      # 构造时归一化 sustain = sustain_ms > 0
class DetectionSpec:  confirm_frames: int = 2

# instruments.py
MAX_SUSTAIN_MS = 20000
_parse_sustain_ms(raw, inst_id) -> int              # 非法/越界 → 0 + warning，绝不抛
load_instruments() → spec.sustain_ms 由 JSON 乐器级 `sustain_ms` 读入

# timeline/assembler.py
_chord_sustain(chord, threshold_ms) -> float        # 达阈值的 hold 取 max，否则 0

# recording.py
SUSTAIN_TEXT_MIN_MS 起：tick_text / tick_text_from_score 渲染 `<CHORD>[<ms>]`
scan_tick_tokens / _read_sustain：解析 `[ms]`（非法吃整段、未闭合不吃）
TickNote.sustain_ms / TickToken.sustain_ms；RecordingBuffer.last_frame_ts / finish(extra=…)

# scorefile.py
RECORD_VERSION = 4；tick.sustain 标记；score_from_record 透传 sustain_ms

# formats/bgi_sheet.py
score_from_tick_text 透传 sustain_ms（此前延音在"文本→BGI"这一段丢失）
yuanqin_notes_text：延音乐器（spec.sustain_ms>0）音符时值上限 100ms → 4000ms
                    （谐律键琴的时值就是延音），仍不越过下一个音的起点

# ui/capture_worker.py
_judge_lock（只保护 detector.update/flush）；stop_recording 先 close 再 flush，
把补抬事件交给 finish(extra=…)，避免与分析线程抢判按器状态
```

## 9. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1 | 2026-10-20 | M0 首次冻结（随骨架提交） |
| **v1.1** | 2026-10-20 | **P1 加法增补（全部向后兼容）**：① `Recorder` 增可选 `thresholds=None`；② `Session` 从占位转为可用实现并冻结状态机/超时/黑帧/线程异常语义，构造增可选 `manager` / `sampler_factory`（测试注入）；③ `BaseBackend.latest()` 透传子类 `CaptureError` 原错误码；④ `printwindow` 后端落地 + 导出 `roi_from_norm`；⑤ 新增 `record` CLI 与 `python -m yqrec` 入口 |
| **v1.2** | 2026-10-20 | **判定语义扩展（真实画面驱动）**：① `KeyThreshold` 增 `mode/base_r/base_g/base_b/delta` 与 `max_drop()`，默认 `channel` 保持旧行为；② `thresholds_from_baseline` 产出 `max_drop` 阈值（三通道最大跌幅 ≥ Δ），修复"按下由白变棕但 R 通道只跌 29、任何 Δ 都不触发"的问题；③ `PressDetector` 在 `max_drop` 下补按下/保持分支（`Δ` / `Δ*(1-hysteresis)`）；④ 新增 `detect_keys_color`（颜色分割+圆度+7×3 网格拟合），检测链路改为 颜色法 → Hough 兜底；⑤ 真图 golden 用例 `tests/data/real/lyre_1696x957.jpg` |
| **v1.3** | 2026-10-20 | **M2 录制链路（加法，不动既有契约）**：① 新增 `recording.py`（`RecordingBuffer` / `Recording` / `RecordedNote` / `tick_text`，帧间隔语义）与 `scorefile.py`（谱面 JSON 读写，schema 见 §8.2）；② `CaptureWorker` 增 `start_recording / stop_recording / recording` 与 `record_progress` 信号，录制挂 worker 不接 `Session`；③ `CaptureWorker._judge` 事件时间戳改用**后端帧 ts**（原来用循环时刻，与录制帧序列有几毫秒漂移）；④ 明确"预览停止 = 录制结束"由 UI 侧同步收尾 |
| **v1.4** | 2026-10-20 | **谱面 JSON 去冗余（用户反馈）**：① 删除 `events[]` 与 `stats`，谱面唯一数据源 = `tick.text`（帧间隔写作 `A60F`），`version: 1 → 2`；② 新增 `recording.parse_tick_text` / `TickNote`（写入端逆运算）与 `scorefile.record_notes / ms_per_frame / score_from_record`（文本 → 可播放 `Score`），帧→毫秒只用 `capture` 的 fps/帧数/时长；③ v1 旧文件仍可读（`events[]` 忽略） |
| **v1.5** | 2026-10-20 | **谱面格式标识 + 窄长条布局（用户反馈）**：① `scorefile.DEFAULT_SCORE_FORMAT="tick"`、JSON 增 `score_format` 字段、`suggested_filename(score_format, *, prefix)` 默认名变为 `谱面_tick_20261020_172400.json`（为后续 BetterGI 等格式预留）；② UI 主区由「左预览 + 右键位」改成**纵向三段**（键位状态 → 预览 → 录制内容/保存），工具栏拆 3 行，默认窗口 660×980；`PreviewWidget` 最小尺寸降到 320×180；区域标签在窄窗只显示 `W×H · 屏 i/n`，完整 `L,T,W,H` 进 tooltip |
| **v1.6** | 2026-10-20 | **窗口置顶（用户反馈，单显示器）**：① `UiConfig.always_on_top=True`（`settings.json` 持久化，启动读取、切换即写）；② 新增 `ui/icons.py::pin_icon`（运行时绘制矢量图钉，无外部资源）；③ 工具栏第一行末尾加 checkable「置顶」按钮（选中=蓝底白钉）；④ `_set_window_on_top` 改标志后还原几何并补 `show()`；⑤ 修 `AppConfig.to_dict()` 的 `vars()`→`asdict()`（slots dataclass 导致 `save_config` 必然抛错） |
| **v1.7** | 2026-10-20 | **读取谱面 + 试听（M2b，用户拍板只做这两项）**：① 新增 `ui/playback.py::PlaybackController`（Player+AudioEngine 的 GUI 桥，回调经 Qt Signal 回主线程，`note_played` 给**和弦序号**、首次 play 才 preload）；② `recording.scan_tick_tokens / tick_spans`（文本 → 和弦 + 字符区间，供高亮跟随）；③ 主窗加「读取文件」（JSON → 文本框 + 试听）与「播放试听」（预览停止且有谱面时可用）；④ 修 `assets/instruments.json` 的 `note_to_audio` 路径（`audio/YuanShen/…` → `audio/…`，105 条）；⑤ 打包带 `assets/audio`（147 mp3 / +19MB）并移出 `pygame` 排除 → dist 200MB |
| **v1.8** | 2026-10-20 | **对话框目录记忆（用户反馈）**：① 新增 `PathConfig.last_score_dir`（`AppConfig.paths`，`settings.json` 持久化）；② 主窗 `_score_dialog_dir()`（上次目录优先，失效退回 `<文档>\YQRec`）与 `_remember_score_dir()`（保存/读取成功后写盘）；③ `load_config` 的段合并列表加 `paths` |
| **v1.9** | 2026-10-20 | **BetterGI 曲谱互转（M2c，用户需求）**：① 新增 `formats/bgi_sheet.py`（`type: "midi"` 事件流读写，见 §8.3 与 03 §8）；② 保存行最左加格式下拉（tick / BetterGI），默认文件名带对应格式段；③ 「读取文件」自动识别 BetterGI 曲谱并可试听；④ 新增 `recording.tick_text_from_score()`（Score→帧间隔文本，供显示非录制来源谱面）；⑤ 导出时间基准取 `bpm=125, ticks=480`（1 tick = 1ms，零量化损失）；⑥ 新增真实曲谱夹具 `tests/data/bgi/canon_7BRsrJmI.json` |
| **v1.10** | 2026-10-20 | **BGI `yuanqin` 支持 + 互校工具（用户要求互印证）**：① `bgi_sheet.parse_yuanqin()`（`A[8]`/`(ASD)[4]`/`@[2]`/`%[72]`/`4-*` 附点/`16-#` 装饰音/`.3 .6 .$` 连音），`parse_sheet` 按 type 分派；② 时值用 JS `Math.round` 语义（`symbol_time = round(60000/bpm)`）；③ 新增 `tools/compare_sheets.py`（画像 + 往返误差 + 跨文件比对）；④ 新增夹具 `tests/data/bgi/pugongying_yuanqin.json`、`tests/data/tick/pugongying_tick.txt`；⑤ 实测三份真实曲谱往返误差 ≤1ms、音高一致率 100%（实测数据留存本表）；⑥ 结论：两份《蒲公英的约定》是**不同编配**（相似度 0.124），不能逐音互校 |
| **v1.11** | 2026-10-20 | **tick → BGI 双格式（M2e，用户给规格）**：① 新增 60fps 规格常量 `FRAME_FPS` / `MS_PER_FRAME`；② `score_from_tick_text()`（帧号→毫秒）；③ `type: "midi"` 导出改取 `bpm=60, ticks=60`（**1 tick = 1 帧**，往返 0 误差）；④ 新增 `type: "yuanqin"` **导出**（`bpm=300`，一拍 12 帧；`A[8]`/`(ASD)[4]`/`@[2]`/附点，绝对时刻对齐、误差不累积）；⑤ `build_sheet_typed` / `sheet_from_tick` / `save_sheet_typed` 统一入口；⑥ UI 下拉由 2 项扩到 3 项（`tick` / `bgi_midi` / `bgi_yuanqin`），文件名段同步；⑦ fps 默认 60，界面保存时传录制实测帧率（防掉帧导致变速）。规则详见 03 §8.2 |
| **v1.12** | 2026-10-20 | **tick 改毫秒制 + 任意文本可保存（M2f/M2g，用户需求）**：① `tick_text` 的间隔单位由**帧**改为**毫秒**（`A1000F` = 隔 1 秒），录制端 `RecordedNote.gap_ms`、解析端 `TickNote.gap`，误差从 ±半个帧长（60fps 时 8.3ms、31.5fps 时 15.9ms）降到 ≤0.5ms，且不再需要 fps；② JSON 升 `version: 3`、`tick.unit = "ms"`，`record_unit()` 区分新旧（旧文件仍按 `capture.fps` 换算）；③ 新增 `build_record_from_text` / `save_record_text`（任意文本直接落盘）；④ BGI midi 导出改取 `bpm=125/ticks=480`（**1 tick = 1ms**，往返 0 误差），yuanqin 仍 `bpm=300` 但按毫秒推进；⑤ 首和弦前的数字保留为前导静默；⑥ UI：录入文本框改为**可编辑**，保存按钮只看文本框内容（录制 / 读取 / 手输都可存），编辑后试听谱面与高亮区间跟着文本重建。规则见 03 §8.2 |
| **v1.13** | 2026-10-20 | **录制期间录入框只读（M2h，用户反馈）**：`_lock_while_recording()` 增加 `_record_view.setReadOnly(recording)` 与动态 tooltip；与其它识别配置控件一样在 `_start_recording` / `_finalize_recording` 成对切换，避免手改内容被 worker 的实时快照覆盖、也让高亮区间与试听谱面始终与文本一致 |
| **v1.14** | 2026-10-20 | **窗口尺寸记忆（M2k，用户需求）**：① `UiConfig` 增加 `window_width=660` / `window_height=980` / `window_maximized=false`；② `_restore_window_size()` 启动套用（`_clamp_window_size()` 夹进最小尺寸与屏幕可用区域）；③ `resizeEvent` 里 400ms 防抖落盘、`closeEvent` 立即落盘，最大化只记标志 |
| **v1.15** | 2026-10-20 | **色偏判据 + 风物之诗重新标定（M2l，用户反馈识别不准）**：① `KeyThreshold.mode` 增加 `"chroma"`（`(to−from)_cur − (to−from)_base >= delta`，方向 `shift_from/shift_to` 由 `detection` 的 refs 自动推出）；② `detection.metric` 允许 `"chroma"`，`thresholds_from_baseline()` 新增 `_chroma_axis()`；③ `fwzs` 改 `metric=chroma`、`default_delta=22`、`delta_range=[8,26]`，refs 用两张截图联合标定；④ `detect_keys_color()` 两处修复：棕键加饱和度上界 `S<170`（橙黄花海不再被当成棕盘）、列基准改为"吸附点数最多"的候选（背景谱号不再让 7 列整体左移） |
| **v1.16** | 2026-10-20 | **外环采样（M2m，用户思路：键帽中心有音符、颜色几乎不变）**：① `KeySampler(..., inner_ratio=0.0)` 新增可选参数，>0 时只统计 `ratio·R .. 0.98·R` 的圆环（掩膜按 stride 网格预计算，尺寸与 ROI 严格一致；掩膜为空退回整块）；② `DetectionSpec.sample_inner_ratio`（默认 0 = 旧行为），`instruments.py` 解析并校验范围；③ `session.make_sampler_factory()` 按乐器配置造工厂，`CaptureWorker` 同步传参；④ `fwzs` 设 `0.55` 并按环形口径重标 refs/Δ（Δ 30、滑块 10–40）：信号 55~80 / 干扰 1~2（原方形 32~50 / 2.6~12.5，信噪比 4~12 倍 → 27~55 倍） |
| **v1.17** | 2026-10-20 | **修 chroma 判据在状态机里失效（M2n，实机"怎么调灵敏度都不亮"）**：① `PressDetector._signals()` 补 `chroma` 分支（此前只认 max_drop/euclid，chroma 阈值没有 min/max → `_limits()` 返回 None → 该键被整场跳过）；② `keys_panel.update_samples()` 的 Δ 显示同口径分派；③ `vision/thresholds.chroma_axis()` 公开（供 UI 诊断）；④ 学完基线新增**可疑基线提示**：某键色偏与中位数差 > 20（`BASELINE_OUTLIER_SHIFT`）时提示"学习时这些键可能正被按下"，避免用户拿按下态画面学基线后永远检不到 |
| **v1.18** | 2026-10-20 | **检测方式切换 + 纵向偏移 + 采样可视化 + 窗口位置（M2o，用户 4 项）**：① Δ 收进 `detection.default_delta`，**界面灵敏度滑条删除**，`_delta()` 直接读 spec，`delta_range` 仅作文档参考；② 原位改为「检测」下拉（`ring` 环形 / `square` 方块）+「偏移」滑条（±80，仅方块可用的 `_sync_offset_enabled`）；③ `UiConfig` 增 `sample_mode="ring"` / `square_offset_y=0`（1920 宽基准像素，`KeySampler._dy()` 按帧宽缩放）；④ `CaptureWorker.set_sampler_options(inner_ratio=, offset_y=)`（参数变化即丢弃缓存 sampler）；⑤ `PreviewWidget.show_sample_overlay/clear_sample_overlay/has_sample_overlay`（蓝 `(70,150,255,70)`，环形用 `QPainterPath` + `OddEvenFill`），挂在 `_learn_baseline` / `_on_baseline` 两端；⑥ `UiConfig` 增 `window_x/window_y=-1`，`_restore_window_position()` 用 `availableGeometry().intersects` 判可见（换显示器不会跑丢），`moveEvent` 与 `resizeEvent` 共用 400ms 防抖；⑦ 新增 `tests/unit_ui/test_sample_overlay.py`（7 例，渲染级断言环形不盖中心 / 方块盖四角 / 偏移平移 / 清除复原） |
| **v1.19** | 2026-10-20 | **采样几何全局化 + Δ 提到乐器层级 + 配置热重载（M2p，用户 3 项）**：① `instruments.json` 升 **v3**，新增顶层 `sampling` 段（`ring_width_ratio=0.43` / `square_side_ratio=1.0`），**所有乐器统一调用**，删除乐器级的 `detection.sample_inner_ratio`；② 小方块边长由 2R **减半为 1R**（用户反馈检测区域太大），面积降到 1/4；实测谐律键琴信号 49/64 → 54/74、干扰 7 → 16（Δ=40 仍安全）；③ `delta` 从 `detection.default_delta` **提到乐器上层**（`{"id":"fwzs","delta":30}`），v2 老写法仍兼容，删除 `delta_range`；④ `KeySampler(..., square_side=)`（`_half_extent = r·side/2`，环形模式忽略）、`PreviewWidget.show_sample_overlay(..., square_side=)`；⑤ `CaptureWorker.set_sampler_options(inner_ratio, square_side, offset_y)`；⑥ 新增 `SamplingSpec` + `load_sampling()` / `instruments_mtime()` / `instrument_deltas()`，`_JSON_CACHE` 按 `(mtime_ns, size)` 签名重读；主窗 `SAMPLING_POLL_MS=1500` 轮询，改 JSON 存盘即生效（读坏只 warning 不换参） |
| **v1.20** | 2026-10-21 | **分段点采样 + 加权判据（M2q）**：① `vision/pointsampler.py::KeyPointSampler`（环形=内环圆周等角点 / 方块=四边等距点，每点取 `point_patch_ratio·R` 圆斑均值），接口与 `KeySampler` 对齐；② `KeyThreshold.mode="weighted"`：`Σw·max(0, drop−tolerance)/Σw ≥ Δ`，权重由 refs 归一化或显式 `detection.weights`；③ `PressDetector(baseline=(K,3))` 逐键基线（区域均值会把被遮挡的键判成常按），`Profile.baseline` 持久化；④ `sampling` 新增 `ring_points/square_points/point_patch_ratio`；「偏移」滑条改「半径」（环形=内环半径 / 方块=中心到四边距离），松手文本级写回 `instruments.json`；⑤ `instruments.update_sampling()` 公开 |
| **v1.21** | 2026-10-21 | **配置读取策略（M2q.2）**：主窗删除 `SAMPLING_POLL_MS` 轮询与 `_sampling_timer`；新增 `MainWindow._reload_instruments(*, source="", manual=False) -> bool`（按 `(mtime,size)` 比对，`manual=True` 时无变化也回一句）；触发点 = 启动 / 「开始预览」/「学习基线」/ **F5**（`QShortcut`）；重读前若半径滑条仍在防抖中先 `_commit_radius()`。`settings.json`（窗口尺寸/位置/置顶/检测方式）与 `profiles/*.json`（区域/键位/基线/阈值）仍只在启动读一次，运行期无任何后台读盘。**未**把 UI 状态并入 `instruments.json`（会被打包覆盖、可能只读、多用户互斥）。 |
| **v1.23** | 2026-10-21 | **配置默认值与版本迁移（M2q.7）**：① `config.py` 集中导出 `DEFAULT_WINDOW_WIDTH/HEIGHT/X/Y` = **558×913 @ (1393,50)**（用户实机调好的窗口），`UiConfig` 与 `main_window` 都引用它（`main_window` 里原有的重复常量已删）；② `CONFIG_VERSION 1 → 2`，`load_config()` 新增 `_migrate()`：version<2 的档把 `record.min_hold_ms` 重置为 0 —— 旧文件里残留的 25 会让 M2q.4「单帧即确认」失效（轻按仍被吃掉），迁移只改这一项、不碰用户其他设置，且**读时不写盘**；③ 新增 `DEFAULT_MIN_HOLD_MS` 模块常量（`slots=True` 的 dataclass 不能通过类属性取默认值，拿到的是 member descriptor）。 |
| **v1.22** | 2026-10-21 | **采样点常显 + 默认几何（M2q.3）**：① 新增 `PreviewWidget.set_sampling(inner_ratio=, square_side=, ring_points=, square_points=, show_points=True)`，红点由 `_paint_grid` 随检测圈常显（用真实 `KeyPoint.r`，圈仍按 `MARKER_SCALE` 放大）；② `show_sample_overlay(point_mode="points")` 只画参考圆，不再画点；③ 默认几何 `DEFAULT_RING_WIDTH 0.43→0.18`（内环 **0.80R**）、`DEFAULT_SQUARE_SIDE 1.0→1.1`（半边长 **0.55R**）、`DEFAULT_RING_POINTS 16→8`、`DEFAULT_SQUARE_POINTS 8→4`；④ 主窗 `_sync_preview_sampling()` 在滑条/下拉/重读/启动时推给预览，点随半径实时移动。 |
| **v1.23** | 2026-10-21 | **轻按识别（M2q.4）**：① `DetectorConfig.min_hold_ms` 默认 25→**0**（条件首次成立即确认；旧语义等于「至少两帧可见」，单帧按下必丢）；② `_KeyState.seen` + `PressDetector.short_presses`（只被 1 帧看到就确认的次数，停止录制时写日志）；③ `CaptureWorker.configure(..., detector_cfg=)`、`_detector_cfg` 来自 `settings.json` 的 `record.min_hold_ms/min_gap_ms`（此前是死配置）；④ `PREVIEW_EVERY 2→3`；⑤ 新增 `capture.manager.backend_availability()`，主窗启动时打印；⑥ `build/yqrec.spec` 的 `hiddenimports` 加入 `collect_submodules('dxcam') + comtypes*`（漏了会静默回退 GDI）。 |
| **v1.24** | 2026-10-21 | **确认帧数取代时间闸（M2q.8）**：① `types.DetectionSpec` 新增 `confirm_frames: int = 1`（连续 N 帧满足才确认按下，1~10，解析越界回落 1）；② `PressDetector` 用 `spec.detection.confirm_frames` 判确认，事件时间戳仍取首帧；`DetectorConfig.min_hold_ms` 降级为可选绝对时长下限（默认 0，不启用）；③ `PressDetector.short_presses` 语义改为「刚好用满 confirm_frames 帧就确认」；④ `CaptureWorker.set_spec(spec)` 新增：F5 重读后重建判按器，让 `confirm_frames`/`delta`/`weights` 真正生效（此前 F5 只换界面 spec，后台仍跑旧参数）；⑤ `assets/instruments.json` 6 个乐器显式写 `detection.confirm_frames: 1`；⑥ 修掉 `min_gap` 不满足时 `pending_since`/`last_up_ts` 双双不变导致的**永久卡死**（快速连击第二个音整段丢失）—— 改为把计时起点推到当前帧重数、等够间隔补认。 |
| **v1.24** | 2026-10-21 | **源帧率可见（M2q.5）**：① `DxgiBackend._open` 改 `target_fps=0`（不走 dxcam 定时器 pacing，跟随桌面 present）；② `CaptureWorker` 新增 `_src_ts`（最近 31 帧后端时间戳），`status_ready` 载荷新增 `src_ms` / `src_fps`（帧间隔中位数）；③ 状态栏格式变为 `后端 | FPS: 处理 | 源: xx.xfps(ms) | 单帧 | 帧数`，`fps ≈ src_fps < 45` 时提示「源画面只有 N 帧」；④ `setup_logging` 把 `dxcam*` logger 接到同一份日志。 |
| **v1.25** | 2026-10-21 | **试听控制条（M2q.9）**：① `PlaybackController` 新增 `play_from(ordinal)` / `pause()` / `resume()` / `state` / `paused` / `onset_of()` / `notes()`；`stop(*, emit_state=True)`（暂停中停止也要发 idle，否则 UI 卡在暂停态）；② `Player._run` 跳过 `onset_ms < start_ms` 的音（否则从中间开始会把之前的音一次性爆出）；③ `icons.media_icon(kind)` 运行时绘制 play/pause/rewind/forward（含 Disabled 配色，无外部资源）；④ 主窗「播放试听」原位换成控制条：`_play_mode` / `_paused` / `_cursor` / `_paused_at`，快退快进仅暂停时可用（`autoRepeat` 长按连续、只移高亮不发声），停止后换回入口按钮；⑤ 高亮改为「光标不选中 + 调色板 Highlight 兜底」（原来系统选区灰白会盖掉蓝底）；
⑥ `PlaybackController._suppress_state`：`play_from()` 换播放器期间屏蔽旧 `Player.stop()`
内部的 `idle`，否则 UI 会误判「播完了」而退出试听模式（实机反馈「点播放键退出又切回来」）；
⑦ 播放/暂停键不开 `autoRepeat`（按住会连续 toggle），只有快退/快进开。 |
| **v1.25** | 2026-10-21 | **多显示器 + 回退可见（M2q.6）**：① 新增 `capture.dxgi_backend.find_output(dxcam, cx, cy) -> (device_idx, output_idx, origin, size)` 与 `local_region(rect, origin, size) -> (l,t,r,b)`（全局→输出局部并夹边界）；`DxgiBackend._open` 改用它，区域落在副屏也能开（此前会 `Invalid Region` 并静默回退 gdi）；② `CaptureManager.start_errors`（本次 start 各后端失败原因）；③ `CaptureWorker.backend_fallback = Signal(str)`，主窗接成红字提示 + 状态栏后端名后标（回退）。 |
| **v1.25** | 2026-10-21 | **多显示器 + 回退可见（M2q.6）**：① 新增 `capture.dxgi_backend.find_output(dxcam, cx, cy) -> (device_idx, output_idx, origin, size)` 与 `local_region(rect, origin, size) -> (l,t,r,b)`（全局→输出局部并夹边界）；`DxgiBackend._open` 改用它，区域落在副屏也能开（此前会 `Invalid Region` 并静默回退 gdi）；② `CaptureManager.start_errors`（本次 start 各后端失败原因）；③ `CaptureWorker.backend_fallback = Signal(str)`，主窗接成红字提示 + 状态栏后端名后标（回退）。 |
| **v1.26** | 2026-10-21 | **视觉层重构（M3a，按 `design/ui-redesign/` 设计包落地，零逻辑改动）**：① 新增 `ui/theme.py`（`THEMES` 暗/亮两张令牌表 + `OVERLAY` 叠加层 + `apply/repolish/set_state/tokens/color/overlay/media_colors` + `chevron_url()`）与 `ui/qss.py`（**唯一** QSS 模板，`{{token}}` 占位，两主题共用几何，`render()` 缺令牌即抛 `KeyError`）；② 新增 `ui/widgets.py`（`StatusDot` 标定状态点 / `VLine` / `EditPanel` 自绘背景+箭头 / `panel_shadow`）；③ 主窗 `_apply_theme(name)` 取代 27 行硬编码样式表，`UiConfig.theme`（默认 `light`）走既有 `settings.json` 链路，工具栏加日/夜切换按钮；④ 三段各包 `QFrame#SectionCard`（`SectionHeader` + `SectionTitle` + `SectionMeta`），`QSplitter.setSizes([246,330,200])`；⑤ 按钮改**四档 objectName 契约**：`Primary` / `Secondary` / `Quiet` / `Danger`（tonal，仅"停止 X"），新增 `_refresh_action_emphasis()` 按 `_play_mode/_recording/_worker/_baseline` 迁移主操作 —— **一行 `setEnabled` 都没改**；⑥ 状态栏拆 `BackendChip`（回退时 `tone="warn"` + 「回退」）/ `StatusText`（`_status_compact` 2/1/0 按窗宽丢弃次要数值）/ `Toast`（info/warn/error 单条消息区），`_flash_status`/`_set_error` 统一走 `_toast()`，**不再调用 `statusBar().showMessage()`**；⑦ 键位卡改属性驱动（`state=pressed/idle/disabled`、`KeyDelta[tone="near"]` 在 Δ≥60% 阈值时琥珀），删除三套行内样式表与 `QFont`；⑧ 试听控制条收进 `QWidget#PlayBar` 胶囊（`Media` 28×24 / 图标 18px / `MediaStop`），`repeat` 与 `NoFocus` 约束不变；⑨ `PreviewWidget` 叠加层颜色改 `theme.overlay()`（新增 `crosshair`），header 改半透明胶囊、新增 `set_stopped()` 角标与 `background_clicked` 信号（编辑态点空白 = 保存退出），采样点常态降为 `dot`(45%,0.07R)、学基线/悬停/按下才提亮；⑩ `RegionPicker` 加四角 L 标记 + 十字辅助线 + 尺寸标签底；⑪ 图标配色改由主题注入（`pin_icon(colors=)` / `media_icon(colors=,size=18)` / `theme_toggle_icon()`），删除 `_ACTIVE_COLOR/_ICON_ON` 等硬编码常量；⑫ **`chevron_url()` 返回本地绝对路径而不是 `file://` URI** —— 实测 Windows 上 QSS 的 `image: url("file:///…")` 不加载（下拉箭头直接消失）。**明确未动**：`PressDetector`/`KeyPointSampler`/`capture/**`/`formats/**`/`recording.py`、tick 文本与谱面 JSON 结构、`setEnabled` 时机、窗口默认尺寸 558×913。 |
| **v1.27** | 2026-10-21 | **默认主题纠偏（M3a.1，用户实机反馈"打开是夜间"）**：① `CONFIG_VERSION 2 → 3`，`_migrate()` 新增 v2→v3 分支把 `ui.theme` 重置为 `light` —— M3a 之前**没有主题切换入口**，旧档里的 `"dark"` 只能是旧默认值被 `asdict(ui)` 序列化，不是用户选择，不重置会让升级后首次启动直接进夜间；用户在 M3a 之后主动切到夜间时档里已是 version 3，不再被该迁移覆盖（新增 3 条用例锁死这两侧边界）；② 主窗新增 `_sync_title_bar()`：用 `DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE=20/19)` 让 Windows 原生标题栏跟随 App 主题（否则日间内容配黑色标题栏，整窗像"半夜间"），`showEvent` / 改窗口标志后各补一次，非 Windows 与调用失败都静默。配置字段与语义无变化，只补一条"旧值不可信"的迁移。 |
| **v1.28** | 2026-10-21 | **产物改名 + 应用图标（M3b，用户要求）**：① 打包产物 `dist/YQRec/YQRec.exe` → **`dist/YQ_moTools/YQ_moTools.exe`**（`build/yqrec.spec` 用 `APP_NAME = "YQ_moTools"` 同时命名 EXE 与 COLLECT，未改 Python 包名 `yqrec` —— import 路径与 CLI 入口不受影响）；② 应用图标换成 `assets/icons/mo_motools.ico`（新增 `tools/make_icon.py`：方形 PNG → 四角圆角透明的多尺寸 ICO 16/24/32/48/64/128/256，圆角比例 0.22 按 `fqwz.ico` 目视对齐；**不用 `ImageDraw.rounded_rectangle`** —— 它用贝塞尔近似、角部比标准圆弧更鼓，改用圆角矩形 SDF + 4× 超采样，每个尺寸单独铺遮罩后再手拼 ICO 容器，避免大图缩放把圆角带偏）；③ 用户可见品牌名同步：窗口标题 / `QApplication` 的 name+displayName+organizationName / BetterGI 与 YQUtick 导出里的 `author`；④ `build/version_info.txt` 的 CompanyName/FileDescription/InternalName/OriginalFilename/ProductName 一并改为 YQ_moTools（文件属性里的"详细信息"）；⑤ **数据目录不改名**：新增 `config.DATA_DIR_NAME = "YQRec"`，`%APPDATA%\YQRec`（标定/设置/日志）与 `<文档>\YQRec`（谱面）继续沿用 —— 改名会丢用户已标定的 21 键坐标与逐键基线，收益只是"看着干净"，不划算。 |
| **v1.29** | 2026-10-21 | **数据目录改名（M3b.1，用户要求）**：① `config.DATA_DIR_NAME = "YQRec"` → **`"YQ_moTools"`**，`%APPDATA%\YQ_moTools`（settings.json / profiles / logs）与 `<文档>\YQ_moTools`（谱面）全部跟产品名一致；② 旧目录**不做迁移、直接删除**（用户明确"后续重新标定一次就可以"）—— `%APPDATA%\YQRec` 整个删掉，`<文档>\YQRec` 里仅有的 1 个谱面已移到新目录后再删空目录（谱面属作品，未销毁）；③ `app._log_dir()` / `scorefile.record_default_dir()` 统一引用 `DATA_DIR_NAME`，CLI description 与相关 docstring 一并改；④ 实机验证：首次启动在新目录生成 `settings.json`（version 3 / theme light / 窗口回落默认 558×913 / `sample_mode=ring`），日志写 `YQ_moTools\logs`，`APPDATA` 下不再出现 `YQRec`。 |
| **v1.30** | 2026-10-21 | **抓帧后端默认值（M3c，用户要求）**：① 后端下拉删除 `auto` 档，只保留 **`dxgi`（默认）/ `gdi`** —— 原「自动」在 `_backend_preference()` 里对屏幕区域固定返回 `gdi`，名不副实且帧率低；② `_backend_preference()` 简化为"返回下拉选中项"（签名去掉无用参数），`AppConfig.backend` 默认 `"auto"` → `"dxgi"`，`_save_profile()` 缺省值同步；旧 profile 里的 `"auto"` 因 `findData()` 落空而自然回落默认 dxgi；③ `_toggle_preview()` 里重复两遍的 `_requested_backend = "auto"` 一并清掉；④ 回退链不动：`CaptureManager.start()` 在首选后端打不开时仍按可用性自动回退并触发 `backend_fallback`；⑤ 置顶默认值无需改动（`UiConfig.always_on_top` 本就是 `True`，实测标志生效），新增两条测试锁定「dxgi 默认 / 无 auto / 置顶默认开」。 |
| **v1.31** | 2026-10-21 | **默认灵敏度 + 交付说明（M3d，用户要求）**：① `assets/instruments.json` 里 **6 个乐器的 `delta` 统一改为 20**（原来是 fwzs=30、其余 5 个=40），让识别更灵敏；判据公式与采样几何不动，改 delta 后**不需要重学基线**（阈值由 baseline + delta 现算，F5 重读即生效）。② 新增 `assets/使用说明.txt`（52 行，UTF-8 **with BOM**，兼容老记事本），面向零基础用户：首次 6 步、录制/保存/试听、何时要重新标定、识别不灵的排查、文件位置。③ 新增 `tools/build_release.py`：一条命令完成 `PyInstaller onedir → dist/YQ_moTools/ → 复制使用说明到 exe 同级`（说明**不能**放 `_internal/`，用户看不到）；脚本先 `taskkill` 旧进程避免 exe 被占用（EBUSY）只换了 JSON。④ 顺带清掉界面里残留的旧品牌文案：格式下拉 `tick（YQRec）` → **`tick（默认）`**（原名加上新品牌会撑爆 124px 宽度）、文件对话框过滤器、保存 tooltip、导出 description、若干 docstring。⑤ 测试同步：`fwzs.default_delta` 30→20、`instrument_deltas()["fwzs"]`、`xljq` 阈值 `delta` 40→20，并补一条「旧档 `backend=auto` 因下拉无此项而回落 dxgi」的断言。 |
| **v1.32** | 2026-10-22 | **j-HDR 后端（用户要求，不影响原链路）**：① 新增 `capture/hdr_backend.py::HdrBackend`（`name="hdr"`，界面显示 `j-HDR`），用 `j-hdrcapture` 的 `Capture(timeout_ms=16)` + `grab_region(l,t,w,h)` 取帧，输出 `HxWx3 uint8 BGR`；`pick_monitor()` 要求区域完整落在**单块屏**内（跨屏拒绝，否则库会去合成整个虚拟桌面）；`InitialFrameTimeout` → 返回 None（计丢帧、不逼降级），`AccessLost`/`DimensionsChanged` → `RuntimeError`（触发回退/重建）。② `manager.FALLBACK_CHAINS` + `order_candidates()`：用户选 j-HDR 时 `hdr → dxgi → gdi`；选 dxgi 时仍是 `dxgi → gdi`（不会偷偷用 hdr）；`prefer` 为空/auto 时保持 `default_backends()` 原顺序（dxgi 仍第一）。③ `manager.BACKEND_LABELS` / `backend_label()`：内部名 `hdr` → 界面 `j-HDR`，状态栏徽标、启动/停止提示、回退提示统一走它。④ 主窗「后端」下拉加 `j-HDR` 项（`setFixedSize` 74→**88**，`j-HDR` 实测 60px + 箭头会被裁），`_backend_preference()` 白名单加 `hdr`；profile 的 `backend` 直接存 `hdr`，`findData` 可回读。⑤ `pyproject.toml` 加 `j-hdrcapture>=0.1.0`（abi3 wheel，零第三方依赖）；`build/yqrec.spec` `hiddenimports` 加 `hdrcapture`（延迟导入，不加会静默缺包）。⑥ **顺带优化 dxcam 打包**：`collect_submodules("dxcam", filter=...)` 排除 `core.winrt_duplicator` 并同步写入 `EXCLUDES`（只在 `backend="winrt"` 的函数内懒加载、依赖未安装的 winrt；实测剪除后包内已无该文件）；保留 collect 而非硬编码清单，避免 dxcam 升版漏模块又静默回退 GDI。⑦ `assets/使用说明.txt` 补 HDR 排查指引（选 j-HDR；**切 HDR / 切后端后必须重学基线**）。**明确未动**：`dxgi_backend`/`gdi_backend`/`wgc_backend`、判据与采样算法、谱面格式、默认后端。 |
| **v1.33** | 2026-10-22 | **默认截图方式改 j-HDR + 记住选择（M3e，用户要求）**：① `AppConfig.backend` 默认 `"dxgi"` → **`"hdr"`**；`load_config()` 缺省值同步；`CaptureManager.default_backends()` 顺序改为 `[hdr, dxgi, wgc, gdi, …]`，CLI `--backend auto`（不带 prefer）也随之 hdr 优先；`_backend_preference()` / `_save_profile()` 兜底值改 `hdr`。② 下拉顺序改为 **`j-HDR`(第 0 项) / `dxgi` / `gdi`**，tooltip 同步（并注明"选择会被记住"）。③ **选择持久化**：新增 `MainWindow._on_backend_changed()` → 写 `AppConfig.backend` + `_save_app_config()`；构造下拉后按 `findData(cfg.backend)` 还原（脏值/空值回落 `hdr`）。④ **标定档不再覆盖后端**：`_load_profile()` 里 `findData(profile.backend)` 整段删除 —— 标定档只记录标定当时用了什么，覆盖会让用户每次启动被拽回旧值。⑤ `CONFIG_VERSION 3 → 4`：`_migrate()` 新增 v3→v4 把 `backend` 强制置 `hdr`（v4 之前 UI 从不回写该字段，档里的 `dxgi` 只是序列化出来的默认值，不是用户选择；不迁移则"改默认"对已有档完全无效）。**未动**：判据/采样算法、谱面格式、`hdr_backend`/`dxgi_backend` 实现、回退链语义。 |
| **v1.34** | 2026-10-22 | **首次运行建档 + 打包日志修复（M3f，用户报告关闭按钮置灰）**：① `MainWindow.__init__` 末尾（`_load_profile()` 之后）新增 `if not config_path().exists(): self._save_app_config()` —— 把首次运行的 `mkdir %APPDATA%\YQ_moTools + 新建 settings.json` 提前到 `show()` 之前，消除窗口可见后唯一一次首次运行主线程写盘（窗口几何 400ms 防抖触发），根除杀软实时扫描下卡主线程 >5s 触发 Windows ghost 窗口（标题栏 X 被系统置灰的唯一机制）；防抖落盘此后因「值没变」跳过。② `app._log_dir()` 相对导入 `from .config import …` 改**绝对导入**：frozen 下 `app.py` 以 `__main__`（无父包）运行，相对导入 ImportError 被 `run_gui()` 的 `except Exception` 静默吞掉 → **打包版文件日志从未装上过**（真实机数据目录无 `logs/` 即铁证）；except 分支补 traceback 留档 `%TEMP%\yqrec_log_fail.txt`。③ `cli.main()` 对 `gui` 子命令跳过无目录的 `setup_logging()`（否则 `_configured` 置位、`run_gui` 幂等跳过、文件 handler 永远装不上）。**未动**：判据/采样算法、谱面格式、配置文件 schema、后端实现。规矩沉淀见 03 §1。 |
| **v1.44** | 2026-10-23 | **键名更正 `Avage_Delta` → `Average_Delta`（用户确认）**：v1.43 落地时按用户原话保留了笔误拼写，用户看到后要求按建议改正。① 写盘键名改为 **`Average_Delta`**（`DELTA_AVG_KEY`），位置不变（紧跟 `type`）；② 新增 **`DELTA_AVG_KEY_LEGACY = "Avage_Delta"` 只读兼容** —— `record_delta_stats()` 在新键缺省时回退旧键，v1.43 那半天存过的谱面仍能读出平均 Δ；③ 键顺序（`version → type → Average_Delta → Min_Delta`）、`Min_Delta` 语义、Δ 口径、JSON 版本号（仍是 v5）一概不动。**未动**：识别算法、tick 文本、试听回放、BGI 互转。 |
| **v1.43** | 2026-10-23 | **tick 谱面加类型与识别质量统计 + 试听回放键位（M2q.11，用户四项要求）**：① 顶层 `version` 后紧跟 **`"type": "tick"`**（与 BetterGI 曲谱的 `type` 同位置同语义，一眼看出文件类型）；② 新增 **`"Avage_Delta"`**（本次识别所有按下的平均 Δ）与紧随其后的 **`"Min_Delta"`**（最小 Δ），值是四舍五入的整数灰阶，`RECORD_VERSION 4→5`；键名**按用户指定拼写**（`Avage` 是 `Average` 的笔误，不改，已有脚本按它对账），常量 `DELTA_AVG_KEY / DELTA_MIN_KEY` 全仓唯一。③ Δ 来源：`PressEvent` 新增 `delta` 字段（**按下时的判据量**，`PressDetector._signals` 现在把标量值一起返回；`weighted`/`max_drop`/`chroma`/`euclid` 各取本模式标量，旧 `channel` 模式用通道最大跌幅），抬起事件恒 0、不参与时间轴。④ 新增 `recording.KeySpan(key/down_ms/up_ms/delta)` + `Recording.spans`（**只在内存，不落盘**）+ `press_deltas/avg_delta/min_delta`；`RecordingBuffer.finish()` 顺手配对出区间（未闭合的按下按最后一帧闭合）。⑤ 试听**刚录的谱面**时，40ms 心跳按 `spans` 点亮键位灯（含按下/弹起时刻与按下时的 Δ；无画面 → RGB 显示 `—/—/—`），读取的文件没有这份数据也不影响；文本一改 / 读文件 / 换乐器即失效（判据是 `_playback_score is recording.score`）。⑥ 顺带修「暂停回弹」：新增 `_parked_ms`，暂停/拖动/快进快退后进度条停在真实时刻，不再跳回和弦起点。**未动**：tick 文本语法、`NoteEvent`、识别判据公式、BGI 互转、采样几何。 |
| **v1.42** | 2026-10-23 | **试听进度条 + 拖动跳转（M2q.10，用户要求「播放组件左侧显示实时进度，可拖动跳跃」）**：① 新增 `ui/widgets.py::PlaybackProgress`（自绘 4px 轨道 + accent 填充 + 悬停放大的把手；18px 高、`NoFocus`、左右各留一个把手半径让 0%/100% 都点得到；`total==0` 不响应鼠标），**只发信号不含业务**：`scrub_started()` / `scrubbed(ratio)` / `seek_released(ratio)` + `set_progress(head_ms, total_ms)` / `reset()` / `fraction()` / `time_of(ratio)` / `refresh_style()`。② `PlaybackController` 新增进度数据源 **`position_ms`**（锚点 `perf_counter()` + 起播 ms，只在控制器留一份；暂停用 `_frozen_ms` 冻结、`resume()` 从冻结处接着算）、**`duration_ms`**（末音 `onset + sustain`，≥1.0）、**`ordinal_at(ms)`**（bisect，+0.5ms 容差）—— UI 每 40ms 读一次自己画，不另记锚点。③ 主窗：`_progress_timer`（`PROGRESS_TICK_MS=40`）刷新；`_play_spacer` 平时 `Expanding`、试听时改 `Minimum` 把留白让给进度条；**按下先 `pause()` 再拖**（否则定时器把把手拽回去）；拖动只改 `_cursor` + 高亮（accent-soft 定位样式）不发声；松手在播就 `play_from(新和弦)` 续播、本来暂停就留在暂停态（`_paused_at` 不动，点播放时自动 seek）；暂停态进度条跟 `onset_of(_cursor)` 走而不是冻结播放头。④ 退出试听模式停表 + `reset()` + 还原 spacer。**未动**：识别算法、谱面格式、`Player`/`AudioEngine`、快退快进语义、窗口默认尺寸。 |
| **v1.45** | 2026-10-26 | **按键演奏（M6，用户要求「参考 BGI 前后台按键输入模式，入口按钮在试听左侧，控制条同试听但直接模拟按键」）**：① 新增 `player/keys.py`：`KeyAction`/`KeyTimeline`/`build_key_timeline()`（纯函数）/`KeyPlayer`（与 `Player` 同接口，后台线程按绝对时间表注入，支持暂停冻结、中途起播补按、收尾强制补抬起）；② `ui/playback.py::PlaybackController` 加 `mode`/`sink`/`set_sink()`/`interrupted` 信号 —— 音频与按键**共用一套进度/暂停/拖动逻辑**；③ `inputsim` 新增 `virtual_key()`/`same_root_window()`/`ForegroundKeys`（SendInput+扫描码，每次注入前复核前台是目标窗口）/`BackgroundKeys`（`PostMessage(WM_KEYDOWN/UP)` + 目标不在前台时补 `WM_ACTIVATE`，lParam 与 BGI `MakeKeyLParam` 同构）/`make_key_sink()`；④ `UiConfig.key_play_mode`（`foreground`/`background`，默认前台）；⑤ 主窗：`_btn_play_keys`（「演奏」，在「试听」左侧）+ 复用同一条 `_play_bar`（`_btn_key_mode` 前台/后台开关只在演奏时出现）+ `_make_key_sink()`/`_key_target_hwnd()`（优先框选区域所在窗口）/`_rearm_key_sink()`（暂停/拖动后继续前先抢回前台，抢不到就停在暂停态而不是退出）。**未动**：识别算法、谱面格式、`Player`/`AudioEngine`、试听语义、窗口默认尺寸。 |
| **v1.44** | 2026-10-23 | **启动做一次"注入通道"自检（`record_click != 0` 时）**：新增 `inputsim.self_test()` —— 把光标挪 40px 再挪回、用 `GetCursorPos` 复核，返回 `(是否生效, 诊断详情)`；详情含会话号/桌面/提权/虚拟屏/沙箱标记。这是区分两类"没反应"的唯一可靠手段：**光标不动 = 指令没进系统输入队列**（安全软件驱动拦截/没有输入桌面）；**光标动了 = 指令进了队列、目标不理会**（打错窗口/目标不认合成输入）。`MainWindow.show_injection_selftest()`：通过只写日志、失败出 warn（气泡带环境）。实机实测：本机（360 的 `360FsFlt/360AntiAttack/360AntiHijack/360Box` 驱动在跑）连**计划任务起的、无沙箱的、提权的** python 都移不动光标 → 拦截在系统层，与 WorkBuddy 沙箱无关。 |
| **v1.43** | 2026-10-23 | **光标移动走 SendInput 绝对坐标 + 移动后复核（用户报"模式 2 报错、键鼠没执行"）**：① 新增 `inputsim.move_cursor()`：先 `SendInput(MOUSEEVENTF_MOVE\|ABSOLUTE\|VIRTUALDESK)`（走输入队列，和真实鼠标同一条路），失败才退 `SetCursorPos` —— 实机日志里 `SetCursorPos(-1513, 762) 失败: winerror=0`（返回 0 且错误码 0、光标纹丝不动），单靠它不可靠。② `left_click()` 移动后**复核 `GetCursorPos`**：偏超 2px 就**放弃点击**并记 warning（否则 down/up 会落在当前光标处，可能误点别的按钮）。③ 新增 `environment_report()` 并写进启动日志 `路径[输入环境]`：会话号/活动会话/桌面名/是否提权/虚拟屏/光标位/是否处于沙箱（`SANDBOX_CENTER_IPC_ADDRESS`、`WORKBUDDY_FS_PROTECTION_ROLE`）—— 一句话定位"注入被谁拦了"。**未动**：`record_click` 语义、识别算法、谱面格式。 |
| **v1.42** | 2026-10-23 | **注入可诊断化（用户报"日志有点击记录但游戏没反应"）**：① 新增 `inputsim.process_name()/window_process()/window_title()/describe()/foreground_window()` —— 日志里现在直接写"打到谁身上"：`注入目标: 0x1059a msedge.exe「…bilibili…」 @ (727,584)`、`录制启动空格注入: 目标 … → 前台 …`。用户当时那句 `窗口 0x1059a` 实际是 **Edge**（最大化盖住主屏），`SendInput` 完全成功、只是没打给游戏。② 新增 `inputsim.is_elevated()/injection_hint()`：目标提权而本程序没提权时提示"UIPI 会静默丢弃注入"（消息区 warn + 日志）。③ `press_space()` 改发**扫描码**（`KEYEVENTF_SCANCODE + MapVirtualKeyW`）—— DirectInput/RawInput 的游戏常常只认扫描码，VK-only 可能被无视；`use_scan_code=False` 可退回旧行为。④ **修 64 位 bug**：`activate_window()` 强提兜底分支用了 `user32.GetWindowLong`（64 位无此导出，只有 `GetWindowLongPtrW`）→ 走到兜底必 AttributeError；改 `_get_window_long()`。⑤ 模式 2（点击）先尝试激活该点窗口再点（后台窗口第一下点击常被吞）。**未动**：`record_click` 语义、识别算法、谱面格式。 |
| **v1.41** | 2026-10-23 | **存档路径可配置 + 消息区气泡（M4e，用户三项要求）**：① `assets/instruments.json` 新增全局键 **`archive_path`**（字符串）：`"Local"` = `<exe目录>\data`、`"Temp"` = `%APPDATA%\YQ_moTools`（**写不进去自动降级 Local**）、其他 = 绝对路径（`%VAR%`/`~` 展开，相对路径非法回落默认）；`config.data_dir()` 顺序改为 `YQREC_DATA_DIR` → `archive_path` → 默认 `%APPDATA%`；新增 `config.archive_setting()/archive_path_dir()/exe_dir()/local_data_dir()`，`instruments.load_archive_path()`。② **去掉弹窗**：存档写不进去只在窗口最下方消息区给一句短提示，完整原因（配置值/目标/错误/实际位置）进悬浮气泡与日志（`_ensure_writable_data_dir()` 改为返回 `(短句, 详情)`）。③ 消息区（Toast）**过长内容悬停弹气泡显示全文**：`_toast(message, tone, detail=)` + `_toast_tooltip()`（按字体实测宽度判断是否截断）+ `_wrap_tooltip()`（`textwrap` 折行，Qt 纯文本 tooltip 不自动折行）；超时清空时连气泡一起清。**未动**：识别算法、谱面格式、`record_click` 语义。 |
| **v1.40** | 2026-10-23 | **启动把 cwd 挪出程序目录（用户问"debug.log 是怎么来的"）**：`debug.log` **不是本程序的产物** —— 它是搜狗输入法写在自己宿主进程**当前工作目录**里的调试日志（内容 `sgime_skin.vN.filemap.sogouime` / `KernelParameterSetter … scheme=Sogou`）。新增 `app._detach_cwd()`：启动早期（Qt 之前）把 cwd 迁到 `%TEMP%\yqrec-cwd`（只认绝对路径的 TEMP/TMP/TMPDIR，都不可用就不动），让 IME 的日志落在临时目录而不是交付目录。本程序所有路径均自算绝对路径（数据目录 / `YQREC_ASSETS` / 谱面默认目录 / 主题缓存），挪 cwd 无副作用。**未动**：日志内容与格式、`record_click`、识别算法、谱面格式。 |
| **v1.39** | 2026-10-23 | **存档目录"真写一次"自检 + 可写兜底（用户报"新包仍读不到存档修改、日志没有今天的记录"）**：① `config.probe_writable(path)`：在目标目录实际建/删一个 `.write_probe_*` 文件，返回错误说明或 None —— 只查 ACL 会得出"一切正常"的假结论（实测 ACL 是 `Administrator:(F)`、PowerShell 能写，某些进程 `open(...,'a')` 仍 `PermissionError`）。② `app._ensure_writable_data_dir()`：启动先探 `data_dir()`，不可写就依次试 `<exe>/data`、`%LOCALAPPDATA%\YQ_moTools`，命中就设 `YQREC_DATA_DIR`（**全进程视图一致**：设置/标定/日志一起搬过去）；一个都不可写则返回失败说明。③ `MainWindow.show_startup_diagnostics(note=)`：有任何写盘异常时**弹模态对话框**（路径 + 原因 + 兜底位置 + 三条处置建议），不再只靠消息区小字。④ `_report_paths()` 多写一行 `路径[乐器配置] = ...（record_click=N）`。**未动**：识别算法、谱面格式、`record_click` 语义、后端实现。 |
| **v1.38** | 2026-10-23 | **数据目录绝对化 + 乐器选择持久化（用户报"设置/日志没写进 C 盘存档"）**：① `config.data_dir()` 取代 `os.environ.get("APPDATA") or Path.home()`：**任何相对路径都视为无效**，依次退 `YQREC_DATA_DIR` → `APPDATA` → 注册表 `User Shell Folders\AppData` → `<主目录>\AppData\Roaming`，全失败才抛 `RuntimeError`；`config_dir()` 转调它。根治"`APPDATA` 为空/相对时日志、设置、标定全落到 exe 所在目录"（实机现象：运行目录凭空多出 `logs/`）。② `app._log_dir()` 改走 `data_dir()/logs`；新增 `_report_paths()`：启动把数据目录/设置/标定档/日志四条路径写进日志，文件 handler 装不上时在界面上红字提示（不再静默）。③ `ui.theme._cache_dir()` 不再用 `tempfile.gettempdir()`（TEMP 不可用时它会回退 cwd）：只认 `TEMP`/`TMP`/`TMPDIR` 里的绝对路径，否则退 `data_dir()/cache`。④ `UiConfig.instrument` 新增：乐器选择写 `settings.json`，启动按它恢复；`_load_profile()` 只在"当前乐器 == 标定档乐器"时套用标定，乐器不同则整份跳过并提示（旧档 `instrument` 为空时沿用旧行为，按标定档切乐器，不丢老用户的标定）。⑤ `_record_click_warmup()` 没有框选区域时改走 `warn` 提示（原来只写日志）。**未动**：识别算法、谱面格式、`record_click` 语义、后端实现。 |
| **v1.37** | 2026-10-22 | **乐器级 `preview_only`（"看得见但选不了"）**：① `InstrumentSpec.preview_only: bool = False`，由 `instruments.json` 乐器级 `preview_only` 读入；真值时乐器仍列在界面下拉里但 `ItemIsEnabled/ItemIsSelectable` 被清掉（灰显 + 「· 仅预览」后缀 + 气泡说明）；② 三处程序化切换兜住：`_load_instrument()` 拒绝并回退原乐器、标定档恢复整份跳过（基线与阈值绑在乐器上，套用必错）、读取谱面只显示不切乐器；默认乐器改为"第一个可选"；③ F5 重读配置时 `_sync_instrument_model()` 重算文字/flags；④ QSS 补 `QComboBox QAbstractItemView::item:disabled`（视图上设了 `color` 后 Qt 默认 disabled 配色不生效，不补就"灰"不出来）；⑤ 当前 `ykq`（悠可琴）/ `yy`（余音）为 `preview_only: true` —— 双区配色与和弦区判定待实机标定，标定完删掉该键即恢复可选（零代码改动） |
| **v1.36** | 2026-10-22 | **yuanqin 延音保真度（用户追问后的修正）**：① 延音取档从"向下取整"改为**就近取档**（`_yq_nearest_ms`）——长音附近相邻档差 200~400ms，向下取整会把 750ms 砍成 600ms，就近取 800ms 误差减半；`upper` 仍以"下一个音起点 / 同键留 30ms / 单音上限"为硬边界，所以向上靠档不会让后续 onset 漂移；② 新增 `bgi_sheet.yuanqin_sustain_loss(score, spec) -> SustainLoss(count, max_ms)` 与 `SUSTAIN_LOSS_TOLERANCE_MS=30`，统计"叠音截断 + 量化损失"；③ UI 保存 `BGI · yuanqin` 成功后按损失量出 warn 提示"改存 BGI · midi"（`main_window._save_score`）；④ 明确：**yuanqin 只能表达单声部延音**（token 顺序播放，无法叠音），midi 才能完整保留多声部延音；⑤ 新增回归：按住一键同时按其他键的整链路（tick/midi 叠音不丢） |
| **v1.35** | 2026-10-22 | **谐律键琴延音录制（M4，用户要求）**：① 延音开关改为**乐器级 `sustain_ms`**（int；0=不延音，>0=按下时长达到该毫秒数才记延音），旧 `sustain: true` 兼容为 200ms；谐律键琴取 300ms；② `detection.confirm_frames` **默认 1 → 2**（至少两帧确认，延音需要"按住态持续"的证据），6 个乐器 JSON 同步；③ 谱面文本新增 `[ms]` 后缀（`A[800]1000F`），`tick_text`/`scan_tick_tokens` 对称实现，谱面 JSON `version 3 → 4` + `tick.sustain` 标记；④ `stop_recording` 用最后一帧时间戳 `flush()` 补抬，停止时会话中按住的键按已按下毫秒数记延音（`RecordingBuffer.finish(extra=…)`）；⑤ 修掉"文本 → BetterGI"丢延音（`score_from_tick_text`），yuanqin 对延音乐器放宽时值上限到 4000ms；⑥ 判按器 update/flush 加锁，避免收尾与分析线程竞态 |
