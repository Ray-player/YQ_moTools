# YQ_moTools

原神演奏界面实时识谱器（Python + PySide6）：框选任意屏幕区域（游戏 / 浏览器视频 / 播放器）→ 逐帧判键 → 录制出谱（tick）→ 试听核对 → 导出 tick 谱或 BetterGI 脚本。

> **文档是唯一事实来源**，先读 [docs/README.md](docs/README.md)。
>
> - [01 · 产品设计与技术架构](docs/01-设计-产品与技术架构.md)
> - [02 · 开发计划与协作](docs/02-计划-开发与协作.md)
> - [03 · 实现规格与接口契约](docs/03-实现规格与接口契约.md)
> - [CONTRACTS.md](docs/CONTRACTS.md)（接口契约 **v1.33**）
> - [M2-完成记录.md](docs/M2-完成记录.md)（**接手先读**：设计决定 / 踩坑 / 已知限制）
> - [M3-计划.md](docs/M3-计划.md)（M3 工作计划：**UI 优化 + 其他乐器适配**，拆 M3a–M3f）

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"      # 含 pygame（试听必需）
pytest -q                    # 期望 406 passed / 2 skipped
yqrec gui                    # 或直接双击 dist/YQ_moTools/YQ_moTools.exe
```

## 目录

```
src/yqrec/       业务代码（capture / vision / timeline / formats / player / recording / scorefile / editor / ui）
assets/          乐器参数、音频采样（147 mp3）、图标、配置档
tests/           单元测试 + 端到端 + 已知谱语料
tools/           capture_spike / bgi_sim / dump_debug / bench / eval
docs/            设计与协作文档（含每个里程碑的完成记录）
build/           打包（PyInstaller spec / Inno Setup）
```

## 状态

**M3e（当前）**：UI 重做为日/夜双主题三段卡片；产物与数据目录改名 `YQ_moTools`；**新增 `j-HDR` 截图后端并设为默认**（HDR 屏唯一颜色正确的路径，SDR 下与 GDI 逐像素一致），截图方式写入 `settings.json` 记住；回退链 `hdr → dxgi → gdi`。`dist/YQ_moTools/YQ_moTools.exe` 200MB / **406 passed / 2 skipped**，契约 **v1.33**（见 [M3e-完成记录](docs/M3e-完成记录.md)、[05 · HDR 调研](docs/05-HDR截图方案调研.md)）。

**M2 完成**：能录谱、能存谱（tick / BetterGI midi / BetterGI yuanqin 三选一）、能读取、能试听。
- 「录制谱面」按钮在工具栏「检测」下拉右侧；**前置条件**：已「学习基线」+ 预览已在跑，否则灰置；再点结束，点预览「停止」也会同步收尾录制
- 录制中在「键位状态」下方实时显示 tick（**毫秒间隔**写法：`A1000F` = 按 A 后隔 1000ms 再按 F；误差 ≤0.5ms，不依赖采集帧率）
- 「保存文件」前置**格式下拉**（三种，都能直接丢进 BGI 的 `AutoYuanQin/assets/score_file`）：
  - `tick（默认）` → 本项目 JSON（`谱面_tick_*.json`，**谱面只存 `tick.text`**，v3 起 `tick.unit="ms"`）
  - `BGI · midi` → AutoYuanQin `type: "midi"`（`谱面_bgi_midi_*.json`），`bpm=125 / ticks=480` 使 **1 tick = 1ms**（零量化）
  - `BGI · yuanqin` → AutoYuanQin `type: "yuanqin"`（`谱面_bgi_yuanqin_*.json`），音符时值记号 `A[8] / (ASD)[4] / @[2]`，`bpm=300`（一拍 200ms）
- 「读取文件」自动识别三种来源（YQRec JSON / BetterGI `midi` / BetterGI `yuanqin` 五线谱制谱器格式），回填文本框并可直接试听；保存/读取对话框**记住上次目录**（重启仍生效）
- 「录制内容」文本框**可编辑**，且它就是谱面的唯一来源：录制来的、读取来的、你手打的，只要里面有内容就能「保存文件」与「播放试听」
- **录制中文本框自动只读**（内容由程序实时刷新），点「停止录制」后恢复可编辑
- 互校工具：`python tools/compare_sheets.py <A> <B>` —— 谱面画像 + `Score→BGI(midi)→Score` 往返误差 + 跨文件相似度
- 「播放试听」→ 预览停止且有谱面时可用，1.0x 播放 + 高亮跟随和弦
- 界面为**窄长条竖排**（键位状态 → 预览 → 录制内容，默认 660×980），工具栏第一行右端**图钉 = 窗口置顶**（默认开，状态持久化）
- **窗口尺寸 + 位置记忆**：拖动缩放/挪动后自动记住（`settings.json` 的 `ui.window_width/height/x/y`），下次启动直接沿用；换显示器/改分辨率时会自动夹进屏幕可用区域，位置不可见则回主屏居中
- **采样口径可选**（工具栏「检测」下拉）：
  - `环形（外圈）`（默认）—— 只取键帽外圈，跳过中心的游戏音符图标，信噪比高（风物之诗 55~80 信号 / 1~2 干扰）
  - `小方块（含中心）`—— 边长 1R 的正方形（原为 2R），可用右侧「偏移」滑条（±80px）上下微调采样窗，值会记住
- **两个几何旋钮写在配置里、所有乐器共用**（`assets/instruments.json` 的 `sampling` 段）：
  环带宽 `ring_width_ratio`（0.43 → 内径 0.55R）、方块边长 `square_side_ratio`（1.0 = 边长 1R）
- **各乐器的灵敏度 Δ 也在同一个文件里**（`instruments[].delta`，如 `{"id": "fwzs", "delta": 30}`），界面不再暴露滑条
- **改配置不用重启**：程序每 1.5 秒看一眼 `instruments.json`，改了环宽/边长/Δ 存盘即生效
  （打包后改的是 `dist/YQ_moTools/_internal/assets/instruments.json`），状态栏会提示「配置已重载…建议重新学习基线」
- **学习基线时会把采样区涂成蓝色半透明**（环形画圆环、方块画方框），学完自动消失 —— 一眼看出"程序到底在采哪块像素"
- 关键模块：`recording.py`（录制缓冲 + tick 文本 + 逆解析/高亮区间）、`scorefile.py`（谱面 JSON + 还原）、`formats/bgi_sheet.py`（BetterGI 曲谱互转）、`ui/playback.py`（试听桥）、`ui/icons.py`（置顶图钉）
- 格式转换规则见 `docs/03` §13（BGI 互转）/ §14（tick → BGI 双格式，毫秒制）/ **§15（检测方式·纵向偏移·采样可视化·窗口位置·采样几何与热重载）**；设计决定与踩坑见 [M2-完成记录](docs/M2-完成记录.md)；契约 **v1.33**，tag `M2p`

**M1 完成**：可运行 GUI 已交付，用户实机测试通过。
- 屏幕区域框选（16:9 锁）+ 实时预览 + 自动检测键位（颜色法 + 网格拟合）+ 手动编辑（增删拖拽）+ 基线学习 + 按键状态灯
- 判据按乐器配置（`assets/instruments.json` 的 `detection` 段），两种可切换：
  - `max_drop` — 三通道最大跌幅（谐律键琴等不透明键帽）
  - `chroma` — 通道对的相对色偏（风物之诗：键帽半透明，背景亮度会压缩绝对跌幅，色偏不受影响）
- **外环采样**（风物之诗 `sample_inner_ratio=0.55`）：只取键帽外圈、跳过中心的音符图标 —— 图标按下时几乎不变，留在均值里只会稀释信号；实测信噪比从 4~12 倍提到 27~55 倍
- 标定持久化（区域 + 21 圈 + 阈值/Δ，重启自动恢复），双屏/负坐标适配；真图 golden 用例（21 圈 / 按下 {+2,7}）

**M0 基线**：接口契约冻结（[CONTRACTS.md](docs/CONTRACTS.md)），纯逻辑模块 76 测试，`yqrec` CLI 可导出 tick 与 BetterGI 脚本。

**下一步（M3）**：见 [M3-计划.md](docs/M3-计划.md) —— 双工作流：① UI 优化（谱面编辑器 Dock / 试听变速·暂停·进度 / 乐器状态可视化 / 框选打磨）；② 其他乐器适配（jhzq/ljdsq/ykq/yy 实机标定 + golden，含 chord 区）。导出（BetterGI/tick）已在 M2c–M2f 基本完成，剩余 M4 重点是安装包与首启自检。
