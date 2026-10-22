"""`Session` 诊断信息与 CLI 兜底提示的单测（Lead 评审收口）。

覆盖：
1. 分析线程因不可恢复异常猝死 → `stop_record()` 不抛，`thread_error` 落到 `Score.metadata`
   与只读属性上（已录数据仍可落盘）；
2. 正常录制 → metadata 里**没有** `thread_error` key；
3. CLI `--json` 摘要含 `ever_black`（正常录制为 False、`status == "ok"`）；
4. 黑帧场景 → `ever_black` 为 True、`status` 仍是 ok（黑帧不中断），且 stderr 有可操作提示。

注意：本文件的假对象全部自带，不依赖 `test_session_record.py`（QA 可能在改它的邻居文件）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from yqrec import cli
from yqrec.session import Session, SessionState
from yqrec.types import (
    CaptureStats,
    CaptureTarget,
    InstrumentSpec,
    KeyGrid,
    KeyPoint,
    Rect,
    ScreenTarget,
)

BRIGHT = 255


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def make_grid(spec: InstrumentSpec) -> KeyGrid:
    """7 列 × N 行网格（顺序 = `spec.key_order`）。"""
    count = spec.key_count
    radius = float(spec.button_radius) / 1920.0
    points = tuple(
        KeyPoint(
            u=0.15 + 0.7 * ((i % 7) / 6),
            v=min(0.85, 0.5 + 0.15 * (i // 7)),
            r=radius,
        )
        for i in range(count)
    )
    return KeyGrid(points=points, key_count=count)


def target_for(size: tuple[int, int] = (64, 36)) -> ScreenTarget:
    return ScreenTarget(monitor=0, rect=Rect(0, 0, size[0], size[1]))


class FailOnDemandManager:
    """先持续吐亮帧，收到 `fail_now` 后抛 `SystemExit`（模拟分析循环猝死）。"""

    def __init__(self, size: tuple[int, int] = (64, 36), fps: float = 240.0) -> None:
        self._frame = np.full((size[1], size[0], 3), BRIGHT, dtype=np.uint8)
        self._fps = fps
        self.fail_now = threading.Event()
        self.stopped = 0
        self.started = 0
        self._ts = 0.0

    def start(
        self, target: CaptureTarget, *, prefer: str | None = None, target_fps: int = 60
    ) -> None:
        self.started += 1
        self._ts = 0.0

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float] | None:
        if self.fail_now.is_set():
            raise SystemExit("boom: 分析线程猝死")
        self._ts += 1.0 / self._fps
        return self._frame.copy(), self._ts

    def stop(self) -> None:
        self.stopped += 1

    def stats(self) -> CaptureStats:
        return CaptureStats(fps=240.0, dropped=0, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self._cb = cb


def wait_for(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# 1. 线程异常 → 不抛、记 metadata
# --------------------------------------------------------------------------- #


def test_thread_error_recorded_in_metadata(spec_fwzs: InstrumentSpec) -> None:
    grid = make_grid(spec_fwzs)
    manager = FailOnDemandManager()
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target_for(), keygrid=grid)

    manager.fail_now.set()
    assert wait_for(lambda: session.thread_error is not None), "分析线程应已异常退出"

    score = session.stop_record()  # 关键：不抛异常
    assert session.state is SessionState.EDIT
    assert "thread_error" in score.metadata
    assert "boom" in score.metadata["thread_error"]
    assert session.thread_error is not None
    assert manager.stopped >= 1, "线程死后仍必须关闭后端"


def test_thread_error_property_is_none_while_recording(
    spec_fwzs: InstrumentSpec,
) -> None:
    grid = make_grid(spec_fwzs)
    manager = FailOnDemandManager()
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target_for(), keygrid=grid)
    assert session.thread_error is None
    session.stop_record()


# --------------------------------------------------------------------------- #
# 2. 正常录制 → metadata 干净
# --------------------------------------------------------------------------- #


def test_clean_recording_has_no_thread_error_key(spec_fwzs: InstrumentSpec) -> None:
    grid = make_grid(spec_fwzs)
    manager = FailOnDemandManager()
    session = Session(spec_fwzs, manager=manager)
    session.start_record(target_for(), keygrid=grid)
    score = session.stop_record()
    assert "thread_error" not in score.metadata
    assert session.thread_error is None


# --------------------------------------------------------------------------- #
# 3/4. CLI 摘要里的 ever_black 与黑帧提示
# --------------------------------------------------------------------------- #


def test_cli_json_contains_ever_black_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "rec.txt"
    code = cli.main(
        ["record", "--source", "synth", "--duration", "0.6", "--out", str(out), "--json"]
    )
    assert code == 0
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, "--json 只能有一行 stdout"
    summary = json.loads(lines[0])
    assert summary["ever_black"] is False
    assert summary["status"] == "ok"
    assert "thread_error" not in summary
    assert "全黑" not in captured.err


def test_cli_warns_when_frames_are_black(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """把合成源的渲染换成全黑帧：ever_black 置真、status 仍 ok、stderr 有提示。"""

    def black_render(self, ts):  # type: ignore[no-untyped-def]  # noqa: ARG001
        return np.zeros((self._height, self._width, 3), dtype=np.uint8)  # noqa: SLF001

    monkeypatch.setattr(cli._SynthSource, "render", black_render)  # noqa: SLF001
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "black.txt"
    code = cli.main(
        ["record", "--source", "synth", "--duration", "0.8", "--out", str(out), "--json"]
    )
    assert code == 0
    captured = capsys.readouterr()
    summary = json.loads([ln for ln in captured.out.splitlines() if ln.strip()][-1])
    assert summary["ever_black"] is True
    assert summary["black_frames"] > 0
    assert summary["status"] == "ok", "黑帧不中断录制，状态仍是 ok"
    assert "全黑" in captured.err
    assert "DRM" in captured.err
    assert out.exists(), "提示必须发生在写文件之后，文件应已生成"


def test_cli_status_partial_when_thread_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """合成源在录制中途猝死 → 谱面仍落盘，但 `status` 变 `partial` 并带 `thread_error`。"""
    healthy_latest = cli._SynthManager.latest  # noqa: SLF001
    started_at: dict[str, float] = {}

    def broken_latest(self, timeout_ms: int = 100):  # type: ignore[no-untyped-def]
        if not started_at:
            started_at["t"] = time.perf_counter()
        if time.perf_counter() - started_at["t"] > 0.25:
            raise SystemExit("boom: 合成源猝死")
        return healthy_latest(self, timeout_ms)

    monkeypatch.setattr(cli._SynthManager, "latest", broken_latest)  # noqa: SLF001
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "partial.txt"
    code = cli.main(
        ["record", "--source", "synth", "--duration", "0.6", "--out", str(out), "--json"]
    )
    assert code == 0, "线程猝死不应让命令失败（已录数据要能落盘）"
    summary = json.loads([ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1])
    assert summary["status"] == "partial"
    assert "boom" in summary["thread_error"]
    assert out.exists()
