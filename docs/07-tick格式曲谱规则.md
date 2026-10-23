# Tick 格式曲谱规则（Agent 速查）

> 面向：需要**读/写/改/校验** YQ_moTools 谱面的其他 Agent。
> 版本：数据契约 **v1.37**（谱面 JSON `version: 4`）。
> 真相在代码里，本文与实现一一对应：`src/yqrec/recording.py`、`src/yqrec/scorefile.py`、
> `src/yqrec/formats/tick.py`、`src/yqrec/formats/bgi_sheet.py`。

---

## 0. 一句话

tick 文本 = **和弦字母串** + **相对毫秒间隔** + 可选 **`[延音毫秒]`**：

```
A1000F          # 0ms 按 A；1000ms 时按 F
A[800]1000DF    # 0ms 按 A 并按住到 800ms；1000ms 时按 D+F
```

**数字永远是「上一个和弦的按下 → 这个和弦的按下」的间隔。**
`[800]` 是 A 的**按住时长**，它**不推进时间轴**、也不与后面的 `1000` 相加 ——
上例 F 在 **1000ms**，不是 1800ms（A 松手到 F 按下只隔 200ms）。

**没有绝对时刻、没有小节线、没有音名（只用键字母）、没有帧。**

---

## 1. ⚠️ 先分清两套写法（最容易踩的坑）

仓库里有两个名字都叫 "tick" 的东西，**语法不同、不能混用**：

| 用途 | 写法 | 解析入口 | 备注 |
|---|---|---|---|
| **GUI 文本框 / 谱面 JSON 的 `tick.text`**（主流，99% 场景） | `A[800]1000F` | `recording.parse_tick_text` / `scan_tick_tokens` | 数字 = **毫秒间隔**；延音用 **`[ms]`** |
| 正式「数字 Tick 谱」`.txt`（CLI `yqrec export` 的输入） | `DBZ.731 731 MXB.731 731` | `formats/tick.py::TickFormat.parse` | 数字 = **休止毫秒**；延音用 **`.ms`** 后缀；支持 `/* @name */` 元数据 |

**`A.800` 在 GUI 方言里不是延音！** 实测 `parse_tick_text("A.800F")` → `A(0ms) + F(晚 800ms)`：
`.` 被当杂字符忽略，`800` 变成下一个和弦的**间隔**。手改谱面时写错这个符号，延音会静默变成休止。

> 本文 §2 起若无特别说明，均指 **GUI/JSON 方言**。

---

## 2. 语法（GUI/JSON 方言，权威）

```
谱面      ::= ( 间隔? 和弦 延音? )*                       # 顺序即时间顺序
间隔      ::= DIGIT+                                     # 毫秒，与**前一个和弦**的间隔
和弦      ::= LETTER+                                    # 连续字母 = 同时按下
延音      ::= '[' DIGIT+ ']'                             # 紧跟所属和弦，最多一个
LETTER    ::= A-Z | a-z                                  # 解析时统一转大写
```

规则表：

| 记号 | 语义 | 细节 |
|---|---|---|
| `ABC` | **一个和弦**（同时按下） | 字母之间**不能有分隔符**；重复字母去重（`AA` → `A`） |
| `1000` | 与**前一个和弦 onset** 的间隔（毫秒） | 不是绝对时刻；首和弦前的数字 = 开头静默 |
| `A[800]` | 该和弦**延音 800ms**（keyUp = onset + 800） | 不改变自身 onset，也不推迟下一个和弦 |
| 空白 / 换行 | 忽略 | 只影响可读性，不产生时间 |
| 其它字符（`.` `,` `\|` `+` `-` `/` 等） | 忽略 | **静默丢弃**，不报错 |

---

## 3. 时间轴模型

**onset 只由间隔数字累加得出**，与 `[...]` 完全无关：

```
text :  500    A[800]   304    DF[992]
onset:   500   500       804    804
keyUp:   —     1300      —      1796
```

- `onset_0 = 首个间隔`（没有数字则为 0）
- `onset_{i+1} = onset_i + gap_{i+1}`
- `keyUp_i = onset_i + sustain_i`（没写 `[ms]` 就是"按下即抬起"，keyUp 等于 onset 附近）
- **叠音合法**：A 还按着（到 1300）可以弹 DF（804）—— 这与 `BGI · yuanqin` 相反（见 §8）

### 3.1 具体到 `A[800]1000F`（最常被问的一次）

| 事件 | 时刻 | 来源 |
|---|---|---|
| A 按下 | **0ms** | 首个和弦，无前导数字 |
| A 抬起 | **800ms** | `[800]`（完整按住时长） |
| F 按下 | **1000ms** | 数字 `1000` = 与 **A 按下**的间隔（不是与 A 抬起） |
| F 抬起 | 1000ms | 无 `[ms]`，视为按下即抬起 |

- ❌ **不是 1800ms**：`[800]` 只描述 A 按住多久，不向后推时间轴
- ✅ A 松手（800）→ F 按下（1000）之间是 **200ms**；A 按下 → F 按下是 **1000ms**
- 若 `[ms]` 大于后面的间隔（如 `A[1200]300F`），就是**叠音**：A 还按着，F 已经在 300 响了

### 3.2 识别 / 试听 / 导出 三处的时间基准

| 环节 | 用什么排时间 | 与文本是否一致 |
|---|---|---|
| 录制（识别） | `assemble` 里 `rest_i = onset_{i+1} − onset_i`，写到文本就是那个数字 | ✅ 数字即 onset 差 |
| 试听（`Player._run`） | 只按 `ev.onset_ms` 定时（`target = t0 + onset/1000/speed`） | ✅ 同一套 onset |
| `BGI · midi` | 1 tick = 1ms，D/U 绝对时刻由 onset ± sustain 累加 | ✅ 逐毫秒对齐 |
| `BGI · yuanqin` | 时值序列，长音受"下一个音起点"截断 | ⚠️ 时间基准同，但表达不了叠音 |

> 唯一"不一致"的地方不在时间轴、而在**音色**：试听只播放单次 mp3 采样，
> **不读 `sustain_ms`**（按住 800ms 和按住 100ms 听起来一样）。延音真正生效是在
> 导出给 BGI/游戏之后（`midi` 的 keyUp 时刻）。

---

## 4. 延音语义

| 问题 | 答案 |
|---|---|
| `[ms]` 是什么 | 该和弦的**完整按住时长**（keyUp 时刻 = onset + ms），不是"超出阈值的部分" |
| 不写 `[ms]` | 视为无延音（0） |
| 写多小会被丢 | **写出**时 `< 50ms`（`SUSTAIN_TEXT_MIN_MS`）不写；**读入**时不裁剪，`A[10]` 照样得到 10 |
| 和弦里每个键时长不同 | 本格式**没有 per-key 延音**：一个和弦只有一个值，录制时取"达阈值的最长 hold" |
| 谁决定"要不要记延音" | 乐器级 `assets/instruments.json` 的 `sustain_ms`：`0` = 不延音；`>0` = 按下时长 ≥ 该值才记（谐律键琴 `xljq` = 300）。**手写文本不受此阈值限制** |
| 一个和弦能写两个 `[..]` 吗 | 不能。`A[800][900]F` 实测 → A 延音 800，`900` 变成 F 的间隔（第二个方括号被当杂字符 + 数字） |

---

## 5. 实测解析表（可直接当单测用例）

| 输入 | 解析结果 `(keys, gap, sustain)` | 说明 |
|---|---|---|
| `A1000F` | `(A,0,0) (F,1000,0)` | 基本形 |
| `A[800]1000DF` | `(A,0,800) (DF,1000,0)` | 延音 + 和弦 |
| `1000AB200C` | `(AB,1000,0) (C,200,0)` | 首数字 = 前导静默 |
| `a100f` | `(A,0,0) (F,100,0)` | 大小写不敏感 |
| `AA100F` | `(A,0,0) (F,100,0)` | 和弦内重复键去重 |
| `A[800]` | `(A,0,800)` | 只有延音、没有后续 |
| `A[0]B` | `(A,0,0) (B,0,0)` | `[0]` 当无延音 |
| `A.800F` | `(A,0,0) (F,800,0)` | **`.` 被忽略 → 800 变间隔**（反例） |
| `A[abc]F` | `(A,0,0) (F,0,0)` | 括号内非数字 → 延音 0，但**整段吃掉**（不会多出 ABC 和弦） |
| `A[800][900]F` | `(A,0,800) (F,900,0)` | 只认第一个 `[...]`（反例） |
| `A[800` | `(A,0,0)` | 未闭合 `]` → 不当延音，也不吞后面的内容 |
| `AB\|C` | `(AB,0,0) (C,0,0)` | `\|`、空格等杂字符忽略 |

---

## 6. 谱面 JSON（`version: 4`）

tick 文本是**唯一谱面数据**，其它字段都是元信息（没有冗余的 `events[]`）：

```json
{
  "version": 4,
  "format": "yqrec.recording",
  "score_format": "tick",
  "name": "谱面",
  "created_at": "2026-10-23T10:38:00+08:00",
  "instrument": { "id": "xljq", "name": "谐律键琴", "key_count": 21 },
  "capture": { "backend": "hdr", "fps": 60.0, "frames": 300, "duration_ms": 4983.3 },
  "tick": {
    "unit": "ms",
    "text": "A[800]1000DF",
    "sustain": true
  }
}
```

- `tick.unit`：**`"ms"` = 数字即毫秒**（v3 起）；`"frame"` 是 v2 旧文件的帧数，读取时乘 `capture.duration_ms/(frames-1)` 换算（见 `scorefile.ms_per_frame`）。
- `tick.sustain`：这份谱面**是否含延音**的标记（仅提示，不影响解析；老 exe 打开会忽略它并丢延音）。
- `capture.fps/frames` 自 v3 起**只作记录**，不参与任何换算。
- 兼容：v1 的冗余 `events[]` 与 `tick` 为纯字符串的写法都能读。

---

## 7. 代码 API 速查

| 需要做什么 | 用哪个 | 位置 |
|---|---|---|
| 文本 → 和弦列表 | `parse_tick_text(text) -> list[TickNote(keys, gap, sustain_ms)]` | `recording.py` |
| 文本 → 带字符区间（高亮/诊断） | `scan_tick_tokens(text) -> list[TickToken(keys, gap, start, end, sustain_ms)]` | `recording.py` |
| 和弦列表 / `Recording` → 文本 | `tick_text(notes)`（要 `RecordedNote`，见下方陷阱） | `recording.py` |
| `Score`（onset 制）→ 文本 | `tick_text_from_score(score)` | `recording.py` |
| 文本 → 可播放 `Score` | `bgi_sheet.score_from_tick_text(text)`（= 逐 gap 累加 onset） | `formats/bgi_sheet.py` |
| JSON 文件 ↔ `Score` | `scorefile.load_record` / `score_from_record` / `record_tick_text` / `record_unit` | `scorefile.py` |
| 正式 `.txt` 数字 Tick 谱 | `formats.tick.parse/text`（`.ms` 方言，**不要**跟 GUI 文本混用） | `formats/tick.py` |

> **API 陷阱：两个"音符"数据类字段名不同**
> `parse_tick_text()` 返回 `TickNote(keys, gap, sustain_ms)`，
> 而 `tick_text()` 吃的是 `RecordedNote(keys, onset_ms, onset_frame, gap_ms, sustain_ms)`。
> `TickNote` 没有 `gap_ms`，直接 `tick_text(parse_tick_text(t))` 会 `AttributeError`。
> 要文本→文本，请走 `tick_text_from_score(score_from_tick_text(t))`（见 §11）。

---

## 8. tick → BetterGI（`formats/bgi_sheet.py`）

`sheet_from_tick(text, spec, sheet_type="midi" | "yuanqin")` → BetterGI 曲谱 dict。
先 `score_from_tick_text`，再 `build_sheet_typed`：

| 目标 | 能否保住延音 | 能否保住叠音 | 说明 |
|---|---|---|---|
| `midi` | ✅ 精确到毫秒 | ✅ | `D`/`U` 事件流，`bpm=125, ticks=480` → **1 tick = 1ms**；文件里的数字是**增量**，校验要逐 token 累加 |
| `yuanqin` | ⚠️ 量化（就近取档，±10%） | ❌ | token 是「按下 → 按够时值 → 抬起」的**顺序播放**，长音被下一个音起点截断；`yuanqin_sustain_loss()` 会报损 |

**结论：要完整延音/叠音一律导 `midi`。**

---

## 9. 如果要生成/改写 tick 文本

1. **只用间隔**，别写绝对时刻：先把目标 onset 排序，再逐音做差分 `gap = onset_i - Σ已输出`。
2. **50ms 以下不写 `[ms]`**（写出来也会被规范化掉）。
3. 和弦直接用字母连写，顺序不影响语义（读回保留原顺序）。
4. 幂等性（走 Score 通道）：`tick_text_from_score(score_from_tick_text(t)) == t` 对**已规范化**
   的文本成立；这些输入会被规范化（实测）：
   `A[30]100F` → `A100F`、`AA100F` → `A100F`、`A[800][900]F` → `A[800]900F`。
5. 字母必须落在**该乐器**的键集内（`instruments.json` → `note_to_key`），否则音名映射不到、播放静音（不报错、也不崩）。
6. 不要写 `//`、`/* */`、小节线、音名（`+1`/`1-`）：GUI 方言里它们全被当杂字符丢弃。

---

## 10. 常见错误清单

| 错误认知 / 写法 | 实际 |
|---|---|
| "数字是绝对时刻" | 是**相对上一个和弦的间隔** |
| "`A[800]1000F` 里 F 在 1800ms" | 在 **1000ms**：`[800]` 不推进时间轴（见 §3.1） |
| "延音越长，下一个音越晚" | 不会，onset 只由间隔数字决定 |
| "`A.800` 表示延音" | GUI 方言里 `.` 被忽略 → 800 变成间隔；延音写 `A[800]` |
| "数字是帧" | v3 起是毫秒；只有 v2 旧 JSON（`unit: "frame"`）才是帧 |
| "延音会让下一个音晚一点" | 不会，onset 只由间隔决定 |
| "和弦里能分别设每个键的延音" | 不能，一个和弦一个 `sustain` |
| "`A[30]` 会保留" | 写出时 <50ms 被丢；读入时保留 |
| "`[ms]` 可以写多个" | 只认一个，第二个会被当杂字符 + 数字（变成间隔） |
| "空格分隔和弦" | 空格只是可读性，`A B` 与 `AB` 语义不同（前者两个和弦、后者一个和弦） |

---

## 11. 自检片段（改完谱面跑一下）

```python
import sys; sys.path.insert(0, "src")
from yqrec.recording import parse_tick_text, tick_text_from_score
from yqrec.formats import bgi_sheet

text = "A[800]1000DF"
for n in parse_tick_text(text):
    print(n.keys, "gap", n.gap, "sustain", n.sustain_ms)

score = bgi_sheet.score_from_tick_text(text)          # gap → 绝对 onset
print("onset:", [(e.keys, e.onset_ms, e.sustain_ms) for e in score.events])
print("规范化:", tick_text_from_score(score))          # 应等于 text
```

命令行（正式 `.txt` 方言）：

```bash
# 注意：-i/--input 是【正式 .txt 方言（A.800 ...）】，不是 GUI 里的 A[800] 文本
.venv/Scripts/python.exe -m yqrec export -i 谱面.txt -f bgi -o 曲谱.json --name "标题"
```
