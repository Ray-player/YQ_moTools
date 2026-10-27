# 介绍视频制作指南

> 用**纯代码**生成 YQ_moTools 的产品介绍片：画面是 QPainter 逐帧画的，音乐是 numpy 合成的，界面截图是程序自己跑一遍识别流程抓的。不依赖任何外部素材、不需要剪辑软件。
>
> 本文给两类读者：**人类**（只想改文案/换音乐/出片）看 §1–§5；**AI**（要改结构/加场景）先读 §6 的红线再动手。

---

## 1. 它是什么

| 项 | 值 |
|---|---|
| 产物 | `dist/YQ_moTools_介绍片.mp4`（1920×1080 / 60 fps / 88 s / H.264 + AAC / ~23 MB） |
| 画面 | `render.py` 用 QPainter 逐帧绘制，8 幕交叉叠化 |
| 音乐 | `audio.py` 三层合成（和弦垫 + 稀疏长音 + 高频微光），**无音效、无外部素材** |
| 素材 | `capture_ui.py` 抓的真实界面截图 + 真机语料的琴键区裁剪 |
| 渲染耗时 | 5280 帧约 **115 秒**（4 核以上机器，纯 CPU） |

设计目标是"**简洁易懂、条理明晰、丝滑**"：每幕只讲一件事，出现的元素都带缓动，场景之间没有硬切。

---

## 2. 三分钟上手

所有命令都在 `tools/intro_video/` 下执行（Windows 的 Git Bash 写法；PowerShell 把 `../../.venv/Scripts/python.exe` 换成 `..\..\.venv\Scripts\python.exe`）。

> **两处例外**：§4.5 的识别圈标定、§7 的成片检查要在**仓库根**下跑（要用到 `src/` 和 `tests/`），代码块首行有标注。

```bash
cd tools/intro_video

# ① 抽帧检查排版（几秒出图，不用等全片）
../../.venv/Scripts/python.exe make.py --preview 3.5,20,31,45,60,71,80

# ② 出片
../../.venv/Scripts/python.exe make.py

# ③ 换外部配乐（自动裁到 88 s + 头尾淡入淡出）
../../.venv/Scripts/python.exe make.py --music "D:/music/xxx.mp3"

# ④ 只重做音轨（想单独试听音乐时）
../../.venv/Scripts/python.exe audio.py
```

- ① 的产物在 `out/intro_video/preview/tXXX_X.png`，直接看，不要等编码。
- ② 会先确保 `out/intro_video/audio.wav` 存在（没有就自动合成），然后边渲染边喂给 ffmpeg，不落中间帧。
- 只能看源码改，**没有配置文件**：一切都在 `timeline.py` / `render.py` / `audio.py` 里。

> 首次跑如果慢，是 PySide6 启动 + 字体注册的固定开销，不是卡住。

---

## 3. 五个文件的分工

```
timeline.py ──┬──→ render.py ──→ 每帧 QImage ──┐
（唯一时间轴） │                                 │
              └──→ audio.py  ──→ audio.wav ────┴──→ make.py ──→ ffmpeg ──→ mp4
```

| 文件 | 职责 | 什么时候改它 |
|---|---|---|
| `timeline.py` | **唯一时间轴**：`S_*` 幕边界、全部文案常量、`MUSIC_MELODY`（旋律）、`CHORDS`（和弦）、`MELODY`（画面里的试听旋律） | 改文案、改时长、换配乐风格 |
| `render.py` | 逐帧绘制。`scene_intro` … `scene_outro` 共 8 个场景 + 调色板 + 绘制原语 | 改版式、加场景、改配色 |
| `audio.py` | 配乐合成（`bell` / `pad` / `shimmer` / `reverb`）与三条分轨 | 改音色、改混音比例 |
| `capture_ui.py` | 启动真实 `MainWindow`，用真机语料跑一遍"检测→采样→判据"，抓 2× 截图 | 换界面素材、换乐器/主题 |
| `make.py` | 编排：`--preview` 抽帧 / 编码全片 / `--music` 换曲 | 改帧率、码率、输出路径 |

**铁律：时间只写在 `timeline.py`。** `render.py` 和 `audio.py` 都从它取数，所以改完不会音画不同步。唯一例外是每幕**内部**的动画时间轴（见 §6.4）。

---

## 4. 我要改 X

### 4.1 改文案

文案全在 `timeline.py` 的常量里（`BRAND` / `PROBLEM_TITLE` / `STEPS` / `DETECT_BADGES` / `OUTRO_FEATS` …）。

改之前先确认这句话画在哪、字号多大：

```bash
grep -n "PROBLEM_TITLE" render.py     # 找到常量被消费的那一行
```

大多数文案是**单行居中**，长度变化不会坏版；但两种情况要复核：

- 已经是长句（如 `PROBLEM_SUB`、`DETECT_NOTE`）——变长可能超出 `box_w`；
- 出现在卡片/胶囊里的（如 `STEPS` 的标题、`OUTRO_FEATS` 的说明）——变长会把卡片撑满或换行。

改完务必 `--preview` 抽那一幕的帧看一眼。

### 4.2 改时长 / 幕边界

```python
S_INTRO  = (0.0, 7.0)     # ← 元组的两个数就是这一幕的起止秒
...
DURATION = 88.0           # ← 必须等于最后一幕的终点
```

改边界时**必须同步这四处**，否则会出现静音、黑屏或音乐早停：

1. `S_*` 元组要连续覆盖 `0 → DURATION`（后一幕起点 = 前一幕终点，中间不能有缝）；
2. `CHORDS` 最后一段的 `止` 要等于 `DURATION`；
3. `MUSIC_MELODY` 里每个音的 `时刻 + 时长` 不能超过 `DURATION`；
4. `MELODY_START` 必须落在 `S_PLAY` 区间内（画面旋律条和键位灯都靠它）。

幕**内部**的动画不用动 —— 它们全部按"场景内相对时间"写的。

### 4.3 换配乐

**改风格（保留自合成）**：

- `CHORDS` — 每段一条和弦垫。`(起, 止, 根音, 音程元组)`，音程是相对根音的半音数，例如 A 小三和弦 = `(0, 3, 7, 12)`。
- `MUSIC_MELODY` — 稀疏点缀。`(起播时刻, 音名, 时长, 音量)`。
  **保持稀疏**：现在的密度是 23 个音、每个 3.4–5.6 s、间隔 ≥3 s。加密就会变成"旋律抢戏"，这正是之前返工的原因。
- 音色在 `audio.py`：`bell()` 是音乐盒，`pad()` 是和弦垫。

**换外部曲目**：

```bash
../../.venv/Scripts/python.exe make.py --music "D:/music/xxx.mp3"
```

会自动 `atrim` 到 88 s、头 1.5 s / 尾 4 s 淡入淡出、音量压到 0.85。注意外部曲目**不会跟着画面换段落**。

### 4.4 换界面截图素材

`capture_ui.py` 顶部三个开关：

| 常量 | 含义 |
|---|---|
| `REAL` | 喂给界面的真机语料图（默认 `tests/data/real/fwzs_1695x956.jpg`） |
| `TICK_SAMPLE` | 录制文本框里显示的 tick 片段 |
| `spec_id` | 乐器 id，见下表；要和语料匹配 |

可选 `spec_id`（来自 `assets/instruments.json`，只有前四个可选）：

| id | 乐器 |
|---|---|
| `fwzs` | 风物之诗琴（默认，语料就是它） |
| `xljq` | 谐律键琴 |
| `jhzq` | 镜花之琴 |
| `ljdsq` | 老旧的诗琴 |
| `ykq` / `yy` | 悠可琴 / 余音（`preview_only`，界面里灰显，别用） |

改完重跑：

```bash
../../.venv/Scripts/python.exe capture_ui.py
```

产物在 `out/intro_video/shots/`：`ui_dark.png`、`ui_light.png`、`_crop_a.png`（琴键区裁剪，§4.5 的标定依赖它的裁剪框）。

> 它跑的是真实流程：`detect_keys_color → circle_map_note → KeyPointSampler → weighted_score`，所以界面上的 RGB、Δ 值、点亮的绿键都是程序算出来的，不是编的。

### 4.5 识别圈位置标定（换素材必做）

`render.py` 里 `GAME_COLS` / `GAME_ROWS` / `GAME_R` 决定 S3「识别」那一幕里 21 个采样圈套在哪。

**不要目测**（目测过一次，整片圈集体朝左上偏）。用程序的检测结果反推：

```bash
# 在【仓库根】执行（下面 4 行在 tools/intro_video 里请先 cd ../..）
.venv/Scripts/python.exe - <<'EOF'
import sys, pathlib
ROOT = pathlib.Path.cwd()
sys.path.insert(0, str(ROOT / "src"))
import cv2
from yqrec.instruments import load_instruments
from yqrec.vision.keygrid import detect_keys_color, circle_map_note

spec = load_instruments()["fwzs"]
frame = cv2.imread(str(ROOT / "tests/data/real/fwzs_1695x956.jpg"))
h, w = frame.shape[:2]
m = circle_map_note(detect_keys_color(frame, spec), spec)

cx0, cy0, CW, CH = 180, 430, 1240, 500        # 必须和 capture_ui.crop_game() 的裁剪框一致
cols, rows = [[] for _ in range(7)], [[] for _ in range(3)]
for i, note in enumerate(spec.note_order):
    c = m[note]
    cols[i % 7].append((c.x - cx0) / CW)
    rows[i // 7].append((c.y - cy0) / CH)
print("COLS =", tuple(round(sum(c) / len(c), 4) for c in cols))
print("ROWS =", tuple(round(sum(r) / len(r), 4) for r in rows))
print("R =", round(spec.button_radius * w / 1920 / CW, 5))
EOF
```

当前输出（供对照）：

```
COLS = (0.1809, 0.2979, 0.4182, 0.5371, 0.6532, 0.7763, 0.8955)
ROWS = (0.3992, 0.6324, 0.877)
R = 0.03845
```

把三行输出填回 `render.py` 的 `GAME_COLS` / `GAME_ROWS` / `GAME_R`，然后抽一帧确认。

### 4.6 改配色

`render.py` 顶部调色板，全部颜色只从这里取（画面里禁止写裸 `#hex`）：

```python
BG   = "#0B0F14"   # 底
T1/T2/T3           # 文字三级（主/次/弱）
ACC / ACC_L / ACC_T # 主色（深/亮/淡底文字）
GREEN / AMBER / DANGER  # 状态色
```

背景光斑、网格、暗角在 `draw_bg()`；左下品牌水印与右上章节点在 `draw_chrome()`。

---

## 5. 设计约束（审美红线）

这几条是踩过坑之后定下来的，改版时别破：

1. **不要任何固定周期的元素。** 视觉上是闪烁/循环位移，听觉上是节拍器。上一版配乐就是因为"分解和弦每 0.5 s 循环 + 低音每 1 s 脉冲"被判为"重复音太显眼"。
2. **配乐要淡。** 目标是人说话时不需要调小音量。当前指标：RMS ≈ −17.7 dB、峰值 0.72、最大样本跳变 0.028。
3. **文字最多三级层次**（`T1` 标题 / `T2` 正文 / `T3` 说明），再多就糊。
4. **每幕只讲一件事**，一屏不超过 4 个信息块。
5. **出现动画统一用** `ease_out`（位移 18–40 px + 透明度），时长 0.55–1.0 s；重要元素可以 `ease_out_back`，但别超过 1.1 的过冲。
6. **场景之间 0.55 s 交叉叠化**（`CROSS`），不要硬切、不要转场特效。

---

## 6. AI 契约：六个必须记住的坑

改代码前读完这一节，每条都真实踩过。

### 6.1 字体必须显式注册

offscreen 平台不会加载系统字体，**不注册就是一整片豆腐块**：

```python
render.init_fonts()   # make.py 里已调；自己新写入口也要调
```

它从 `%WINDIR%/Fonts` 注册 `msyh.ttc` / `msyhbd.ttc` / `consola.ttf`。

### 6.2 `col()` 的透明度是"乘"

```python
col(QColor(255, 255, 255, 9), 0.5)   # → alpha 9 * 0.5，正确
```

早期写成 `setAlphaF(alpha)` 直接覆盖，导致所有 `rgba(255,255,255,9)` 的卡片变成纯白实心块。

### 6.3 `text()` 的对齐语义

```python
text(p, cx, y, s, 24, align=Qt.AlignmentFlag.AlignHCenter, box_w=600)
#    ↑ 这里传的是"中心点"，函数内部会自己减掉 width/2
```

**调用方不要自己再减**。有场景写成 `text(p, node_x - 260, ..., box_w=520, align=HCenter)`，结果整体左移 260 px 被画面切掉。

### 6.4 场景内时间 ≠ 绝对时间

每个 `scene_*(p, t)` 收到的 `t` 是**从这一幕起点算起的秒数**。

- 幕内动画直接用 `t`；
- 引用 `timeline` 里的绝对时刻（如 `MELODY_START`）必须先换算：

```python
MELODY_LOCAL = TL.MELODY_START - TL.S_PLAY[0]   # 绝对 → 场景内
```

漏了这步的表现是"某个动画整段不出现"（判定条件永远为假）。

### 6.5 交付 QImage 给 ffmpeg 必须是 RGB888

```python
QImage(w, h, QImage.Format.Format_RGB888)   # 配 -pix_fmt rgb24
```

用 `Format_RGB32`（实际是 BGRA）会让整片红蓝互换。另外**不要**给 `constBits()` 加 `.copy()` 以外的加工，stride 必须是 `w*3`。

### 6.6 抓界面时初始化顺序

`capture_ui.make_window()` 里的顺序不能换：

```
先设 _baseline → 再 _recompute_thresholds() → 最后 _set_grid(grid)
```

`_recompute_thresholds()` 依赖 `_baseline`，反了会把 `_thresholds` 置 `None`，灯板上所有 Δ 显示成 `Δ—`。

---

## 7. 交付前自检

```bash
# ① 每幕抽一帧，看"文字没被裁 / 元素没溢出 / 没有重叠"
#    在 tools/intro_video/ 下
../../.venv/Scripts/python.exe make.py --preview 3.5,11,20,31,45,60,71,84
```

```bash
# ② 出片后检查黑帧与静音（都应该没有任何输出行）
#    在【仓库根】下
FF=$(.venv/Scripts/python.exe -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())")
$FF -hide_banner -i "dist/YQ_moTools_介绍片.mp4" -vf blackdetect=d=0.4:pix_th=0.06 -an -f null -
$FF -hide_banner -i "dist/YQ_moTools_介绍片.mp4" -af silencedetect=n=-45dB:d=1.2 -vn -f null -

# ③ 音量：mean −18 ~ −15 dB，max ≤ −1 dB
$FF -hide_banner -i "dist/YQ_moTools_介绍片.mp4" -af volumedetect -f null - 2>&1 | grep volume

# ④ 时长与流信息
$FF -hide_banner -i "dist/YQ_moTools_介绍片.mp4" 2>&1 | grep -E "Duration|Stream"
```

---

## 8. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| 全片文字是方框/豆腐块 | 没注册字体 | 确认调了 `render.init_fonts()` |
| 卡片变成纯白实心块 | `col()` 覆盖了原色 alpha | 用乘算版本（§6.2） |
| 某段文字整体偏左/被画外切掉 | 居中时自己又减了半宽 | 传中心点（§6.3） |
| 整片红蓝互换 / 颜色发绿 | `RGB32` 配了 `rgb24` | 改 `Format_RGB888`（§6.5） |
| 识别圈集体偏左上 | `GAME_COLS/ROWS` 是目测值 | 用 §4.5 的命令重标 |
| 某个动画整段不出现 | 用绝对时间比较了场景内 `t` | 减 `S_*[0]`（§6.4） |
| 灯板全是 `Δ—` | 阈值重算在设基线之前 | 调整顺序（§6.6） |
| 片尾黑屏/音乐早停 | `DURATION` 大于最后一幕终点，或 `CHORDS` 没覆盖到结尾 | 见 §4.2 |
| 场景切换有硬边 | `CROSS` 被改成 0 或两层权重没归一化 | 检查 `weights_at()` |
| `capture_ui.py` 报 `KeyGridMismatch` | 语料图与 `spec_id` 不匹配 | 换回对应乐器的语料 |
| 出片音画不同步 | 在 `render.py` 里写死了秒数 | 回 `timeline.py` 取数 |

---

## 9. 参数速查

### `timeline.py`

| 常量 | 含义 |
|---|---|
| `DURATION` | 总时长（秒），必须等于最后一幕终点 |
| `S_INTRO` … `S_OUTRO` | 8 幕的 `(起, 止)` |
| `CROSS` | 场景交叉叠化时长（0.55 s） |
| `BRAND` / `TAGLINE` / `INTRO_CAPS` | 开场标题、副标、三个能力胶囊 |
| `PROBLEM_*` | 第二幕（痛点）文案 |
| `STEPS` / `FLOW_WORDS` | 第三幕三个节点、底部数据流标签 |
| `DETECT_*` | 第四幕标题、副标、三个 badge、判据说明 |
| `UI_*` | 第五幕标题、三条标注、底部说明 |
| `PLAY_*` | 第六幕标题、两条注入通道、安全提示 |
| `EXPORT_*` | 第七幕标题、三张格式卡（名称/后缀/说明/代码片段，代码用 `\n` 分行） |
| `OUTRO_*` | 第八幕标题、四个数据格、尾部提示、版本号 |
| `MELODY` / `MELODY_START` / `MELODY_STEP` | 画面里"试听演示"的旋律与节奏（管视觉，不管声音） |
| `MUSIC_MELODY` | 配乐旋律 `(起, 音名, 时长, 音量)` |
| `CHORDS` | 和弦进行 `(起, 止, 根音, 音程)` |
| `NOTE_FREQ` | 音名 → 频率表（加新音名时补这里） |

### `render.py`

| 常量 | 含义 |
|---|---|
| `W`, `H` | 画布尺寸（1920×1080） |
| `GAME_COLS` / `GAME_ROWS` / `GAME_R` | 第四幕采样圈的列/行/半径（§4.5 产出） |
| `UI_BLOCKS` | 第五幕截图里三块区域的比例坐标 `(左上, 右下)` |
| `SCENES` | `(时间范围, 场景函数)` 的注册表，顺序即叠化顺序 |

### `make.py`

| 参数 | 含义 |
|---|---|
| `--preview 3.5,20,31` | 只渲染这几个时间点的 PNG 到 `out/intro_video/preview/` |
| `--music PATH` | 用外部音频替换自合成配乐 |
| `FPS`（常量 60） | 帧率；改 30 会让动效明显变顿，不建议 |

---

## 10. 当前分镜（改版先看这张表）

| 时间 | 幕 | 内容 | 关键视觉 |
|---|---|---|---|
| 0–7 s | 开场 | 品牌 + 一句话定位 + 三个能力 | Logo 呼吸、标题升起、光带 |
| 7–14 s | 痛点 | "弹完，就散了" + 三张否定卡 | 红色下划线、✕ 图标交错淡入 |
| 14–25 s | 流程 | 三步：框选 → 播放大佬视频 → 自动出谱 | 节点 ripple、连接线生长、流动光点 |
| 25–39 s | 识别 | 只盯 21 个键 | 扫描线 + 21 个采样圈逐个点亮 + 灯板 Δ 值 |
| 39–55 s | 界面 | 真实截图，聚光依次框住三块区域 | 截图升起、mask 挖洞聚光、序号圆点 |
| 55–67 s | 演奏 | 试听 + 双通道注入 | 进度条推进、键位灯、旋律柱 |
| 67–75 s | 导出 | 一份谱三种用法 | 三卡滑入、代码逐字打出并语法着色 |
| 75–88 s | 收尾 | 四个数据 + 开箱即用 | Logo、数据格、尾部胶囊、淡出 |

---

## 11. 依赖

`pyproject.toml` 的 `dependencies` 已经覆盖大部分，但**视频工具额外需要两个包**（不在项目依赖里，因为程序本体不碰视频）：

```bash
.venv/Scripts/python.exe -m pip install imageio-ffmpeg pillow
```

| 包 | 用途 | 在 pyproject 里？ |
|---|---|---|
| `PySide6` | 渲染（QPainter / QImage）、抓界面（MainWindow） | ✅ |
| `numpy` | 音频合成、采样 | ✅ |
| `opencv-python-headless` | 读语料、颜色检测（仅 `capture_ui.py`） | ✅ |
| `Pillow` | 裁剪琴键区（仅 `capture_ui.py`） | ❌ 需手动装 |
| `imageio-ffmpeg` | 提供 ffmpeg 可执行文件（编码 mp4） | ❌ 需手动装 |

约束：**不联网、不引用外部音效/音乐素材**（`--music` 是留给用户自己换曲的出口），界面截图必须来自程序自己跑出来的结果。
