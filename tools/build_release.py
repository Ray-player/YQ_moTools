"""一条命令出交付包：`PyInstaller` onedir → `dist/YQ_moTools/` → 附《使用说明.txt》。

为什么要脚本：以前打包是"手敲 PyInstaller + 手动 cp"，有两个容易忘/踩的点 ——
① 覆盖 `dist` 里的 exe 时若旧进程还开着（EBUSY），只有 JSON 被换、exe 还是旧的；
② 打包完成后要额外把使用说明放到 **exe 同级**（放 `_internal/` 里用户是看不到的）。

用法（项目根）：
    .venv/Scripts/python.exe tools/build_release.py
    .venv/Scripts/python.exe tools/build_release.py --skip-build   # 只重新铺 dist（改文案时用）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SPEC = PROJECT / "build" / "yqrec.spec"
APP_NAME = "YQ_moTools"
DIST = PROJECT / "dist" / APP_NAME
GUIDE = PROJECT / "assets" / "使用说明.txt"
_STAGE_PREFIX = "yqrec_build_"
"""临时 dist 目录前缀。**必须清掉**：onedir 一轮约 200MB，之前每打一次包就在
`%TEMP%` 里留下一份，实测堆到 1.2GB 才发现（2026-10-22）。"""
_current_stage: list[Path] = []


def _kill_running() -> None:
    """覆盖 exe 前必须先关掉它（Windows 上被占用的文件只读、写不进去）。"""
    result = subprocess.run(
        ["taskkill", "/F", "/IM", f"{APP_NAME}.exe"], capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"已结束正在运行的 {APP_NAME}.exe")
    else:
        print(f"{APP_NAME}.exe 未在运行（继续）")


def _build() -> Path:
    """打包到临时目录：不碰 dist，避免 --noconfirm 把用户文件（含说明）一起清掉。"""
    stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX))
    _current_stage.append(stage)  # 交给 main() 的 finally 清掉（一轮 ~200MB，别留在 C 盘）
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(stage),
        "--workpath",
        str(PROJECT / "build" / "work_release"),
        str(SPEC),
    ]
    print("运行:", " ".join(command))
    subprocess.run(command, check=True, cwd=PROJECT)
    built = stage / APP_NAME
    if not (built / f"{APP_NAME}.exe").exists():
        raise SystemExit(f"打包失败：{built} 里没有 {APP_NAME}.exe")
    return built


def _cleanup_stage() -> None:
    """删掉本次的临时 dist 目录；顺便清掉历史遗留（同样前缀且不是本次的）。"""
    for stage in _current_stage:
        shutil.rmtree(stage, ignore_errors=True)
    _current_stage.clear()

    tmp_root = Path(tempfile.gettempdir())
    for leftover in tmp_root.glob(f"{_STAGE_PREFIX}*"):
        if leftover.is_dir():
            print(f"清理遗留临时目录: {leftover}")
            shutil.rmtree(leftover, ignore_errors=True)


def _sync(built: Path) -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, DIST, dirs_exist_ok=True)
    # 使用说明放 **exe 同级**：`_internal/` 里的文件用户基本看不到
    shutil.copy2(GUIDE, DIST / GUIDE.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="打交付包（含使用说明）")
    parser.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller，只同步 dist")
    args = parser.parse_args()

    _kill_running()
    try:
        if args.skip_build:
            if not DIST.exists():
                raise SystemExit(f"{DIST} 不存在，先完整打一次包")
            shutil.copy2(GUIDE, DIST / GUIDE.name)
        else:
            _sync(_build())
    finally:
        _cleanup_stage()  # 成功失败都清，别把 200MB 留在 C 盘

    exe = DIST / f"{APP_NAME}.exe"
    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n交付: {exe}  ({size_mb:.1f} MB)")
    print(f"说明: {DIST / GUIDE.name}  ({GUIDE.stat().st_size} 字节)")
    print("整包:", sum(f.stat().st_size for f in DIST.rglob('*') if f.is_file()) // 1024 // 1024, "MB")


if __name__ == "__main__":
    main()
