"""命令行入口：`yqrec formats|export|bench|record|version`（03 §2）。

`record` 支持的三个来源：

- `screen`：抓屏幕区域（`--rect` 必填，需真实捕获后端）；
- `file`：读 mp4 / PNG 目录（回归测试用，需要 OpenCV）；
- `synth`：内置合成帧源（纯 numpy，无真机也能验证整条链路）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import __version__
from .errors import YQRecError
from .formats import all_formats, bgi_js, tick
from .logging import setup_logging
from .types import CaptureStats, KeyGrid, KeyPoint

_SYNTH_BRIGHT = 255
"""合成源的非按下亮度（R=255 > 阈值上界 190 的滞回上限 218 → 稳定判为"未按下"）。"""

_SYNTH_DARK = 40
"""合成源的按下亮度（R=40 <= 阈值上界 190 → 判为"按下"）。"""


def _default_keygrid(spec) -> KeyGrid:  # type: ignore[no-untyped-def]
    """按 7 列网格生成内置默认键位布局（仅合成源/冒烟用）。

    顺序与 `spec.key_order` 对齐（网格顺序 = 采样顺序 = 判按顺序）。
    """
    count = spec.key_count
    radius = float(spec.button_radius) / 1920.0
    points: list[KeyPoint] = []
    for i in range(count):
        row, col = divmod(i, 7)
        cols = min(7, count - row * 7)
        u = 0.5 if cols <= 1 else 0.15 + 0.7 * (col / (cols - 1))
        v = min(0.85, 0.5 + 0.15 * row)
        points.append(KeyPoint(u=u, v=v, r=radius))
    return KeyGrid(points=tuple(points), key_count=count)


class _SynthSource:
    """内置合成帧源：在 keygrid 点上按方波画暗/亮方块（无真机冒烟用）。

    信号设计：前 `lead_ms` 帧全亮（静默），随后逐键按下 `press_ms`，相邻两键间隔
    `gap_ms`；21 键走完一轮不再循环（便于回归比对事件数与时刻）。

    帧生成按 `perf_counter` 节流到 `fps`：同一帧号只产出一帧，其余返回 None，
    与真实后端的"暂无新帧"语义一致。
    """

    def __init__(
        self,
        spec,  # type: ignore[no-untyped-def]
        grid: KeyGrid,
        *,
        size: tuple[int, int] = (640, 360),
        fps: int = 60,
        press_ms: float = 300.0,
        gap_ms: float = 120.0,
        lead_ms: float = 400.0,
    ) -> None:
        if size[0] <= 0 or size[1] <= 0:
            raise ValueError(f"合成帧尺寸必须为正: {size!r}")
        self._grid = grid
        self._width = int(size[0])
        self._height = int(size[1])
        self._fps = max(1, int(fps))
        self._presses = self._build_schedule(press_ms, gap_ms, lead_ms)
        self._rects = self._build_rects()
        self._t0 = time.perf_counter()
        self._last_idx = -1
        self._frames = 0

    # -- 构造辅助 ---------------------------------------------------------- #

    def _build_schedule(
        self, press_ms: float, gap_ms: float, lead_ms: float
    ) -> tuple[tuple[int, float, float], ...]:
        """`[(键序号, 按下毫秒, 抬起毫秒), ...]`。"""
        out: list[tuple[int, float, float]] = []
        cursor = float(lead_ms)
        for index in range(len(self._grid.points)):
            out.append((index, cursor, cursor + float(press_ms)))
            cursor += float(press_ms) + float(gap_ms)
        return tuple(out)

    def _build_rects(self) -> tuple[tuple[int, int, int, int], ...]:
        """每个键要涂黑的像素矩形 `(y0, y1, x0, x1)`（比采样 ROI 大 1px 以求稳）。"""
        rects: list[tuple[int, int, int, int]] = []
        for point in self._grid.points:
            cx, cy, radius = point.to_pixels(self._width, self._height)
            x0 = max(0, int(round(cx - radius)) - 1)
            x1 = min(self._width, int(round(cx + radius)) + 1)
            y0 = max(0, int(round(cy - radius)) - 1)
            y1 = min(self._height, int(round(cy + radius)) + 1)
            x1 = max(x0 + 1, x1)
            y1 = max(y0 + 1, y1)
            rects.append((y0, y1, x0, x1))
        return tuple(rects)

    # -- 帧源 -------------------------------------------------------------- #

    @property
    def size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def frames(self) -> int:
        return self._frames

    def reset(self) -> None:
        """重新开始（时间基准归零）。"""
        self._t0 = time.perf_counter()
        self._last_idx = -1
        self._frames = 0

    def latest(self) -> tuple[np.ndarray, float] | None:
        """按 `fps` 节流产出一帧；同一帧号不重复产出（返回 None）。"""
        rel = time.perf_counter() - self._t0
        index = int(rel * self._fps)
        if index <= self._last_idx:
            return None
        self._last_idx = index
        self._frames += 1
        ts = index / float(self._fps)
        return self.render(ts), ts

    def render(self, ts: float) -> np.ndarray:
        """渲染 `ts`（秒，相对起点）时刻的帧：按下中的键涂暗，其余全亮。"""
        frame = np.full((self._height, self._width, 3), _SYNTH_BRIGHT, dtype=np.uint8)
        cursor_ms = float(ts) * 1000.0
        for index, start_ms, end_ms in self._presses:
            if start_ms <= cursor_ms < end_ms:
                y0, y1, x0, x1 = self._rects[index]
                frame[y0:y1, x0:x1] = _SYNTH_DARK
        return frame


class _SynthManager:
    """`CaptureManager` 的最小替身：让 `Session` 直接消费 `_SynthSource`。"""

    def __init__(self, source: _SynthSource, *, size: tuple[int, int]) -> None:
        self._source = source
        self._size = size
        self._degraded_cb = None
        self._started_at = time.perf_counter()

    def start(self, target, *, prefer=None, target_fps=60) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002
        self._source.reset()
        self._started_at = time.perf_counter()

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        got = self._source.latest()
        if got is None:
            time.sleep(min(0.001, max(0.0, timeout_ms / 1000.0)))
        return got

    def stop(self) -> None:
        return None

    def stats(self) -> CaptureStats:
        elapsed = max(1e-6, time.perf_counter() - self._started_at)
        return CaptureStats(fps=self._source.frames / elapsed, dropped=0, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self._degraded_cb = cb


def _parse_rect(text: str) -> tuple[int, int, int, int]:
    """解析 `L,T,W,H`（逗号分隔，允许空格）。"""
    parts = [p for p in text.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"--rect 需要 4 个整数 L,T,W,H，实际: {text!r}")
    left, top, width, height = (int(float(p)) for p in parts)
    if width <= 0 or height <= 0:
        raise ValueError(f"--rect 宽高必须为正: {width}x{height}")
    return (left, top, width, height)


def _wait_until(started_at: float, duration_s: float) -> None:
    """睡到时长结束（分片 sleep，便于 Ctrl+C 及时响应）。"""
    while True:
        remain = duration_s - (time.perf_counter() - started_at)
        if remain <= 0:
            return
        time.sleep(min(0.05, remain))


def _cmd_formats(_args: argparse.Namespace) -> int:
    for info in all_formats():
        flags = ("R" if info.can_read else "-") + ("W" if info.can_write else "-")
        print(f"{info.id:<8} {flags}  {info.ext:<6} {info.name}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    text = Path(args.input).read_text(encoding="utf-8")
    score = tick.parse(text)
    errors = [d for d in score.diagnostics if d.is_error]
    if errors:
        for d in errors:
            print(f"ERROR @{d.offset}: {d.message}", file=sys.stderr)
        return 2
    if args.format == "tick":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(tick.format(score), encoding="utf-8")
        print(f"written: {out}")
        return 0
    if args.format == "bgi":
        target = bgi_js.export(score, args.out, name=args.name)
        print(f"written: {target}")
        return 0
    print(f"未知导出格式: {args.format}", file=sys.stderr)
    return 2


def _cmd_bench(args: argparse.Namespace) -> int:
    """采样性能基准（02 §7 T7 的基础）。"""
    from .instruments import load_instrument
    from .vision.sampler import KeySampler

    spec = load_instrument(args.instrument)
    k = spec.key_count
    pts = tuple(
        KeyPoint(u=0.1 + 0.8 * (i % 7) / 6, v=0.6 + 0.1 * (i // 7), r=spec.button_radius / 1920.0)
        for i in range(k)
    )
    sampler = KeySampler(KeyGrid(points=pts, key_count=k), 1600, 300)
    frame = np.random.default_rng(0).integers(0, 255, (300, 1600, 3), dtype=np.uint8)
    for _ in range(5):  # 预热
        sampler.sample(frame)
    t0 = time.perf_counter()
    for _ in range(args.frames):
        sampler.sample(frame)
    dt = (time.perf_counter() - t0) / args.frames * 1000
    print(json.dumps({"instrument": spec.id, "keys": k, "frames": args.frames,
                      "ms_per_frame": round(dt, 3)}))
    return 0


def _build_record_session(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """按 `--source` 组装 (Session, target, keygrid, thresholds)。"""
    from .capture.file_backend import FileBackend
    from .capture.manager import CaptureManager
    from .config import load_profile
    from .instruments import load_instrument
    from .session import Session
    from .types import Rect, ScreenTarget

    spec = load_instrument(args.instrument)
    keygrid: KeyGrid | None = None
    thresholds = None
    if args.profile:
        profile = load_profile(args.profile)
        keygrid = profile.keygrid
        thresholds = dict(profile.thresholds) or None
        if profile.instrument != args.instrument:
            print(
                f"提示: 配置档 {profile.name!r} 的乐器是 {profile.instrument!r}，"
                f"与 --instrument {args.instrument!r} 不同",
                file=sys.stderr,
            )

    manager = None
    if args.source == "screen":
        if not args.rect:
            print(
                "用法: yqrec record --source screen --rect L,T,W,H "
                "[--profile NAME] [--duration SEC]",
                file=sys.stderr,
            )
            return None
        left, top, width, height = _parse_rect(args.rect)
        target = ScreenTarget(monitor=0, rect=Rect(left, top, width, height))
        manager = CaptureManager()
    elif args.source == "file":
        if not args.input:
            print("用法: yqrec record --source file --input PATH [--profile NAME]", file=sys.stderr)
            return None
        if not FileBackend.is_available():
            print(
                "文件源需要 OpenCV（cv2），当前环境未安装：pip install opencv-python-headless",
                file=sys.stderr,
            )
            return None
        target = ScreenTarget(monitor=0, rect=Rect(0, 0, 1920, 1080))
        # 只允许 FileBackend：否则dxgi/gdi 可用时，缺文件会"静默降级成抓真实屏幕"
        manager = CaptureManager(backends=[FileBackend])
        manager.set_file_source(str(args.input))
    else:  # synth
        size = (640, 360)
        if args.rect:
            _left, _top, width, height = _parse_rect(args.rect)
            size = (width, height)
        if keygrid is None:
            keygrid = _default_keygrid(spec)
            print("提示: 未指定 --profile，使用内置默认键位布局（仅 --source synth）", file=sys.stderr)
        source = _SynthSource(spec, keygrid, size=size, fps=args.fps)
        manager = _SynthManager(source, size=size)
        target = ScreenTarget(monitor=0, rect=Rect(0, 0, size[0], size[1]))

    return Session(spec, manager=manager), target, keygrid, thresholds


def _cmd_record(args: argparse.Namespace) -> int:
    """录制 → 落盘 tick 谱 → （可选）打印 JSON 摘要。"""
    from .session import SessionState

    built = _build_record_session(args)
    if built is None:
        return 2
    session, target, keygrid, thresholds = built

    duration = float(args.duration) if args.duration else 0.0
    try:
        session.start_record(
            target,
            keygrid=keygrid,
            thresholds=thresholds,
            backend=args.backend,
            target_fps=args.fps,
        )
        if duration > 0:
            _wait_until(time.perf_counter(), duration)
        else:
            print("录制中… 按 Ctrl+C 结束", file=sys.stderr)
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("已中断，正在收尾…", file=sys.stderr)
    finally:
        score = session.stop_record() if session.state is SessionState.RECORDING else None

    if score is None:  # start_record 已抛错（已被 main 捕获），这里仅防御
        return 2

    out_path = Path(args.out) if args.out else Path("out") / f"{datetime.now():%Y%m%d-%H%M%S}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(tick.format(score), encoding="utf-8")

    stats = session.stats
    thread_error = score.metadata.get("thread_error")
    summary: dict[str, object] = {
        "status": "partial" if thread_error is not None else "ok",
        "events": len(score.events),
        "notes": score.note_count,
        "duration_ms": int(score.metadata.get("duration_ms", "0") or 0),
        "fps": round(float(stats.fps), 1),
        "dropped": int(stats.dropped),
        "black_frames": session.black_frames,
        "ever_black": session.ever_black,
        "out": str(out_path),
    }
    if thread_error is not None:
        summary["thread_error"] = thread_error
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"written: {out_path}")
        print(json.dumps(summary, ensure_ascii=False))

    # 黑帧提示放在写文件之后（且走 stderr），不污染 --json 的单行 stdout
    if session.ever_black:
        print(
            "警告: 检测到连续全黑帧（可能是 DRM 保护/独占全屏/窗口化优化），谱面可能为空。"
            "建议换窗口目标（WGC）或关闭播放器硬件加速。",
            file=sys.stderr,
        )
    return 0


class _DynamicStderrHandler(logging.StreamHandler):
    """始终写到**当前** `sys.stderr` 的控制台 handler。

    `logging.StreamHandler` 构造时就把流绑死了，而 `setup_logging()` 是幂等的全局配置，
    因此同一进程内第二次调用 `main()`（pytest capsys、Qt 嵌入、把 CLI 当库用）时，
    handler 仍会往**已关闭**的旧流写 → logging 自己打 `--- Logging error ---` + traceback，
    破坏"CLI 不出 traceback"的验收口径。这里在每次 `emit()` 前重新解析 `sys.stderr`，
    从根上避免持有过期流引用（也不去 flush/close 宿主的流）。
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr  # 每次输出前刷新，宿主换流/关流都不会踩到旧对象
        if self.stream is None:  # pythonw 等没有 stderr 的场景
            return
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


def _install_console_handler(root: logging.Logger) -> None:
    """把 `yqrec` logger 的控制台 handler 换成"动态 stderr"版本（文件 handler 不动）。"""
    stale = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and not isinstance(handler, _DynamicStderrHandler)
    ]
    for handler in stale:
        root.removeHandler(handler)  # 只摘掉，不 close（避免误关宿主流的包装对象）
    if not any(isinstance(handler, _DynamicStderrHandler) for handler in root.handlers):
        root.addHandler(_DynamicStderrHandler())


def _cmd_gui(_args: argparse.Namespace) -> int:
    """启动 GUI（PySide6 在 `run_gui` 内部才导入，CLI 其余功能不依赖 Qt）。"""
    from .app import run_gui

    return run_gui()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yqrec", description="YQ_moTools 原神演奏界面实时识谱器")
    p.add_argument("--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("formats", help="列出已注册的谱面格式")
    sp.set_defaults(func=_cmd_formats)

    sp = sub.add_parser("export", help="把 tick 谱导出为其他格式")
    sp.add_argument("--input", "-i", required=True, help="输入 .txt（tick）")
    sp.add_argument("--format", "-f", choices=["tick", "bgi"], default="bgi")
    sp.add_argument("--out", "-o", required=True, help="输出目录 / 文件")
    sp.add_argument("--name", default=None, help="曲名（BGI manifest 用）")
    sp.set_defaults(func=_cmd_export)

    sp = sub.add_parser("bench", help="采样性能基准")
    sp.add_argument("--instrument", default="fwzs")
    sp.add_argument("--frames", type=int, default=200)
    sp.set_defaults(func=_cmd_bench)

    sp = sub.add_parser("record", help="录制识谱（屏幕区域 / 文件 / 合成源）")
    sp.add_argument("--source", choices=["screen", "file", "synth"], required=True)
    sp.add_argument("--rect", default=None, help="屏幕区域 L,T,W,H（--source screen 必填）")
    sp.add_argument("--input", default=None, help="文件源路径（mp4 / PNG 目录）")
    sp.add_argument("--profile", default=None, help="配置档名（提供 keygrid / thresholds）")
    sp.add_argument("--instrument", default="fwzs", help="乐器 id（默认 fwzs）")
    sp.add_argument("--backend", default="auto", help="捕获后端名 / auto")
    sp.add_argument("--duration", type=float, default=0.0, help="录制时长（秒；0 = 手动停止）")
    sp.add_argument("--fps", type=int, default=60, help="目标帧率")
    sp.add_argument("--out", default=None, help="输出 tick 谱路径（默认 out/<时间戳>.txt）")
    sp.add_argument("--json", action="store_true", help="只打印单行 JSON 摘要")
    sp.set_defaults(func=_cmd_record)

    sp = sub.add_parser("gui", help="启动图形界面（实时预览 + 键位状态灯）")
    sp.set_defaults(func=_cmd_gui)

    sp = sub.add_parser("version", help="打印版本")
    sp.set_defaults(func=lambda _a: print(__version__) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 0
    if args.cmd == "gui":
        # 文件日志交给 run_gui()（用真实日志目录初始化）；这里若先调无目录的
        # setup_logging() 会把 _configured 置位，run_gui 里幂等跳过 → 文件
        # handler 永远装不上（源码路径 `python -m yqrec gui` 无日志文件）。
        return int(args.func(args) or 0)
    _install_console_handler(setup_logging())
    try:
        return int(args.func(args) or 0)
    except YQRecError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:  # --rect 等参数格式错误
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
