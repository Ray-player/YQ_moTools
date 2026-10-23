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
pytest -q                          # 期望 415 项全绿

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
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | 冻结的接口契约清单 |
| [docs/07-tick格式曲谱规则](docs/07-tick格式曲谱规则.md) | **曲谱语法速查**（给 Agent / 外部工具）：tick 文本语法、延音、JSON 封装、BGI 转换、常见错误 |
| [docs/M0 / M2 / M3-完成记录](docs/) | 各里程碑交付物、设计理由、踩坑、已知限制（**接手先读 M3-完成记录**） |

文档更新规则（谁改哪份）见 [02 §文档更新规则](docs/02-计划-开发与协作.md)。

## 目录

```
src/yqrec/       业务代码（capture / vision / timeline / formats / player / recording / scorefile / editor / ui）
assets/          乐器参数、音频采样（147 mp3）、图标、配置档
tests/           单元测试 + 端到端 + 已知谱语料
tools/           capture_spike / bgi_sim / compare_sheets / build_release 等
docs/            设计与协作文档（含每个里程碑的完成记录）
build/yqrec.spec PyInstaller 打包配置
```

## 当前状态

**M3 收口（当前，tag `v0.1.0`）**：

- **UI**：日/夜双主题、三段竖排布局（键位状态 → 预览 → 录制内容），窗口几何与采样口径持久化
- **截图**：`j-HDR`（默认，HDR 屏唯一颜色正确的路径）/ DXGI / GDI 三档，回退链 `hdr → dxgi → gdi`
- **录制识别**：逐键基线 + weighted 判据；录制启动可选输入注入（`instruments.json` 的 `record_click`：0=关 / 1=激活目标窗口+模拟空格 / 2=模拟鼠标点击），详见 `docs/03` §5.1
- **导出**：tick（默认）/ BetterGI midi / BetterGI yuanqin 三种，互转往返误差 ≤0.5ms；`tools/compare_sheets.py` 可做谱面互校
- 质量：**415 项 pytest 全绿**；后续方向见 [02 §迭代记录](docs/02-计划-开发与协作.md)
