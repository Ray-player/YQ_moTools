# YQ_moTools

原神演奏界面实时识谱器（Python + PySide6）：框选任意屏幕区域（游戏 / 浏览器视频 / 播放器）→ 逐帧判键 → 录制出谱（tick）→ 试听核对 → 导出 tick 谱或 BetterGI 脚本。

## 从零构建

**前置**：Windows 10/11 64 位、Python ≥ 3.12、git。仓库已包含运行与测试所需的全部资源（乐器参数、147 个音频采样、图标、测试语料），克隆后无需额外下载。

```bash
git clone <本仓库地址> YQRec
cd YQRec

python -m venv .venv
.venv\Scripts\activate

pip install -e ".[dev,build]"     # dev=pytest/ruff；build=PyInstaller（只跑程序可不装）
pytest -q                          # 期望 583 项全绿（末尾偶见 offscreen 退出 segfault，可忽略）

yqrec gui                          # 源码运行 GUI
python tools/build_release.py      # 打包 → dist/YQ_moTools/YQ_moTools.exe（onedir，双击运行）
```

说明：

- 首次运行会自动在 `%APPDATA%\YQ_moTools\` 建配置目录（界面状态与标定档），不写程序目录
- 打包脚本自带旧进程清理与临时目录管理，产物在 `dist/YQ_moTools/`
- 运行期改 `assets/instruments.json`（源码态）或 `dist/YQ_moTools/_internal/assets/instruments.json`（打包态）后按 F5 热重载

## 文档地图

| 文档 | 内容 |
|---|---|
| [docs/01-设计-产品与技术架构](docs/01-设计-产品与技术架构.md) | 产品定义、决策、数据链路、架构、关键算法、输出格式、UI/截图/键鼠三项技术总结 |
| [docs/02-计划-开发与协作](docs/02-计划-开发与协作.md) | 里程碑与工期、协作规则、文档更新规则、迭代记录 |
| [docs/03-实现规格与接口契约](docs/03-实现规格与接口契约.md) | **代码级规格**（写码必读）：数据模型、算法规格、格式语法、JSON schema、踩坑清单 |
| [docs/04-tick格式曲谱规则](docs/04-tick格式曲谱规则.md) | **曲谱语法速查**（给 Agent / 外部工具）：tick 文本语法、延音、JSON 封装、BGI 转换、常见错误 |

> 原 `docs/CONTRACTS.md` 已并入 [03 附录 A](docs/03-实现规格与接口契约.md)；M0–M4 记录（交付物/设计理由/踩坑）于 2026-10-26 归档到 git 历史。

文档更新规则（谁改哪份）见 [02 §文档更新规则](docs/02-计划-开发与协作.md)。

## 目录结构与核心代码

```
YQRec/
├─ src/yqrec/                 # 业务代码（唯一源码目录）
│  ├─ app.py                  #   GUI 启动入口：环境自检、日志装配、cwd 迁移、首启建档
│  ├─ cli.py                  #   命令行入口（yqrec gui / 子命令分发）
│  ├─ types.py                #   全部 dataclass 契约（Rect / KeyGrid / NoteEvent / SamplingSpec / KeyThreshold / Profile…）
│  ├─ config.py               #   settings.json 读写 + CONFIG_VERSION 迁移 + 数据目录定位与可写探测
│  ├─ instruments.py          #   instruments.json 装载（识别参数、archive_path、key_play_delay_ms）
│  ├─ session.py              #   会话状态机
│  ├─ recording.py            #   录制缓冲：逐帧进 → 实时 tick 文本（谱面唯一数据源）
│  ├─ scorefile.py            #   谱面 JSON 读写（RECORD_VERSION=5）与还原
│  ├─ inputsim.py             #   键鼠注入：前台 SendInput（扫描码）+ 后台 PostMessage 双通道 + 自检
│  ├─ errors.py / logging.py  #   错误码、日志（文件 handler 失败时界面红字报警）
│  ├─ capture/                # —— 截图层 ——
│  │  ├─ base.py              #   CaptureBackend 协议与异常
│  │  ├─ manager.py           #   后端路由 + 回退链 hdr → dxgi → gdi（回退必提示）
│  │  ├─ hdr_backend.py       #   j-HDR（默认：FP16 抓帧 + 逐屏 SDR 白点归一，HDR 屏唯一颜色正确路径）
│  │  ├─ dxgi_backend.py      #   DXGI（dxcam；副屏按区域选输出 + 局部坐标换算）
│  │  ├─ gdi_backend.py       #   GDI（兜底）
│  │  ├─ wgc / printwindow / file_backend.py   # 窗口目标 / PrintWindow / 文件回放
│  │  └─ ringbuffer.py / windowing.py
│  ├─ vision/                 # —— 视觉层 ——
│  │  ├─ keygrid.py           #   键位自动检测（填洞→距离变换→局部极大 + 网格拟合 + 圆→音名），手动点选兜底
│  │  ├─ pointsampler.py      #   分段点采样（环形 0.80R×8 / 方块 0.55R 半边×16）
│  │  ├─ sampler.py           #   KeySampler 协议
│  │  └─ thresholds.py        #   weighted 三通道加权判据 + 逐通道死区 + 逐键基线
│  ├─ timeline/               # —— 判按时序层 ——
│  │  ├─ detector.py          #   PressDetector：逐键基线 + confirm_frames 确认状态机
│  │  └─ assembler.py         #   事件装配（min_gap 补认、抬起阈值 <0.85Δ、延音收尾）
│  ├─ formats/                # —— 谱面格式层 ——
│  │  ├─ registry.py          #   Formatter 协议 + @register（读取自动识别格式）
│  │  ├─ tick.py              #   tick 文本解析/序列化（毫秒制 + 延音 A[ms]，语法见 docs/04）
│  │  ├─ bgi_sheet.py         #   BetterGI midi / yuanqin 双向转换（延音裁剪 + 损失统计，规则注释含证据链）
│  │  ├─ bgi_js.py            #   BGI 脚本导出
│  │  └─ fengwu.py / domiso.py   # 只读外部格式
│  ├─ player/                 # —— 播放层 ——
│  │  ├─ engine.py            #   Player：绝对时间表 + 变速 / seek / 循环
│  │  ├─ audio.py             #   pygame.mixer 采样混音（21 键预加载）
│  │  └─ keys.py              #   KeyPlayer：与 Player 同接口，谱面 → 按键注入时间轴（漏 up = 游戏卡键，收尾必补抬）
│  ├─ ui/                     # —— 界面层（PySide6）——
│  │  ├─ main_window.py       #   主窗：三段竖排布局、录制/试听/演奏编排、几何防抖落盘
│  │  ├─ region_picker.py     #   区域框选（16:9 锁 + 多屏含负坐标 + DPI）
│  │  ├─ keys_panel.py        #   键位卡、Δ 预警与采样点可视化
│  │  ├─ preview.py           #   实时预览
│  │  ├─ playback.py          #   PlaybackController：播放头唯一数据源（回调经 Qt Signal 回主线程）
│  │  ├─ capture_worker.py    #   捕获 → 分析工作线程（每帧 <5ms，不落盘不编码）
│  │  └─ theme.py / qss.py / widgets.py / icons.py   # 主题令牌 / 单模板 QSS / 通用控件 / 图标
│  └─ editor/                 #   预留（谱面编辑器 M3d 顺延未做，现为可编辑纯文本框）
├─ assets/                    # instruments.json（识别参数 + archive_path + key_play_delay_ms）、audio/（147 mp3）、icons/、profiles/（标定档）、refTex/（仅作颜色参考，不能验收键位检测）
├─ tests/                     # unit_capture / unit_vision / unit_timeline / unit_formats / unit_ui 等 + e2e/ + data/ + 真图 golden（共 583 项）
├─ tools/                     # build_release.py（出包）、capture_spike.py、compare_sheets.py（谱面互校）、compare_detectors.py、eval_reftex.py、reftex_colors.py、make_icon.py、bgi_sim.js（BGI 时序模拟器）、hdr_probe/
├─ build/                     # yqrec.spec（PyInstaller 配置）+ version_info.txt
├─ design/                    # UI 设计包（令牌定义、界面规格）
├─ docs/                      # 01-设计 · 02-计划 · 03-实现规格 · 04-tick 规则
└─ dist/YQ_moTools/           # 打包产物（onedir，双击 YQ_moTools.exe 运行）
```

## 当前状态

**v0.1.6（当前）**：

- **UI**：日/夜双主题、三段竖排布局（键位状态 → 预览 → 录制内容），窗口几何、乐器选择与采样口径持久化
- **截图**：`j-HDR`（默认，HDR 屏唯一颜色正确的路径）/ DXGI / GDI 三档，回退链 `hdr → dxgi → gdi`，回退必提示
- **录制识别**：逐键基线 + weighted 判据；录制启动可选输入注入（`record_click`：0=关 / 1=激活目标窗口+模拟空格 / 2=模拟鼠标点击），启动自动注入自检，日志写明注入目标与输入环境
- **试听**：播放器式控制条（播放/暂停/快进快退 + 进度条拖动跳转）；试听刚录的谱面时键位灯随录制实况点亮（按下/弹起/按下时 Δ）
- **按键演奏**：谱面直接按键注入游戏——前台 SendInput（扫描码）/ 后台 PostMessage 双通道，通道由 `instruments.json` 的全局参数 `key_play_channel`（int：0=后台 默认 / 1=前台）决定，界面无切换按钮；起播/续播延迟全局可配（`key_play_delay_ms`，默认 3s 倒计时）；被挡逐拍跳过不中断、回焦补发抬起（M6 待实机验证）
- **读谱（v0.1.6 修复）**：读入 BetterGI midi / yuanqin 谱面时，tick 文本按谱面所标乐器自动裁剪延音——无延音乐器不再带 `A[ms]` 标记（此前无论乐器一律带延音）；未标乐器按风物之诗处理
- **导出**：tick（默认，支持延音 `A[ms]`，JSON 含 `type` / `Average_Delta` / `Min_Delta` 统计）/ BetterGI midi / BetterGI yuanqin 三种，互转往返误差 ≤0.5ms；`tools/compare_sheets.py` 可做谱面互校；曲谱语法速查见 [docs/04](docs/04-tick格式曲谱规则.md)
- **乐器**：6 乐器参数内置（风物之诗/谐律键琴/镜花之琴/老旧的诗琴实机可用；悠可琴/余音暂为仅预览），改 `instruments.json` 后 F5 热重载
- **存档**：数据目录只认绝对路径并实测可写，写不进自动兜底（exe 同级 `data` → `%LOCALAPPDATA%`）并在消息区提示；存档位置可经 `instruments.json` 的 `archive_path` 配置（`Local` / `Temp` / 绝对路径）
- 质量：**583 项 pytest 全绿**；后续方向见 [02 §迭代记录](docs/02-计划-开发与协作.md)
