"""CLI 端到端冒烟（`yqrec record` → tick → BGI → `tools/bgi_sim.js`）。

与 `tests/test_cli.py`（工程师单测）分工：那边用 `main()` 覆盖分支与退出码；
这边跑**真链路**并把产物互相校验：

- `--json` 摘要字段/类型契约 + tick 回读一致性（摘要里的 events/notes 必须与文件对得上）；
- 默认输出目录只创建 `out/`，不往 cwd 里撒文件；
- 分析线程死掉 → `status=partial` + `thread_error`，退出码仍为 0（数据要能落盘）；
- `--source file` 在装了 cv2 的机器上遇到不存在的路径：退出码 2 + 可读提示（不是 traceback）；
- tick → BGI 导出后 `sheet.json` 与 `build_events()` 完全一致，且 `bgi_sim.js` 回放事件序列一致。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from yqrec.capture.file_backend import FileBackend
from yqrec.cli import main
from yqrec.formats import bgi_js, tick
from yqrec.types import CaptureStats, KeyGrid, KeyPoint

REPO_ROOT = Path(__file__).resolve().parents[2]
BGI_SIM = REPO_ROOT / "tools" / "bgi_sim.js"

SUMMARY_KEYS = {
    "status",
    "events",
    "notes",
    "duration_ms",
    "fps",
    "dropped",
    "black_frames",
    "ever_black",
    "out",
}


class FatalError(BaseException):
    """分析线程内的不可恢复异常。"""


class EndlessFrames:
    """一直吐亮帧（CLI 等待 duration 期间维持录制）。"""

    def __init__(self, frame: np.ndarray) -> None:
        self._frame = frame
        self.degraded_cb = None

    def start(self, target, *, prefer=None, target_fps=60) -> None:  # noqa: ARG002
        return None

    def latest(self, timeout_ms: int = 100) -> tuple[np.ndarray, float]:  # noqa: ARG002
        return self._frame.copy(), time.perf_counter()

    def stop(self) -> None:
        return None

    def stats(self) -> CaptureStats:
        return CaptureStats(fps=60.0, dropped=0, last_error=None)

    def on_degraded(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.degraded_cb = cb


class SlowFatalSampler:
    """首帧握手已完成后再崩（先睡 50ms，避开 `start_record` 的首帧竞态）。"""

    frame_size = (640, 360)

    def sample(self, frame: np.ndarray) -> np.ndarray:  # noqa: ARG002
        time.sleep(0.05)
        raise FatalError("模拟驱动崩溃")


def _single_json_line(capsys) -> dict:  # type: ignore[no-untyped-def]
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"--json 必须只输出一行，实际 {len(lines)} 行: {lines}"
    return json.loads(lines[0])


def test_record_json_contract_matches_tick_file(tmp_path: Path, capsys) -> None:
    """`--json` 摘要的字段/类型/取值必须与落盘 tick 完全对得上。"""
    out = tmp_path / "rec.txt"
    code = main(["record", "--source", "synth", "--duration", "0.8", "--out", str(out), "--json"])
    assert code == 0

    summary = _single_json_line(capsys)
    assert SUMMARY_KEYS <= set(summary), set(summary)
    assert summary["status"] == "ok"
    assert isinstance(summary["events"], int) and summary["events"] >= 1
    assert isinstance(summary["notes"], int) and summary["notes"] >= 1
    assert isinstance(summary["duration_ms"], int) and summary["duration_ms"] > 0
    assert summary["fps"] > 0
    assert summary["dropped"] >= 0
    assert summary["black_frames"] == 0
    assert summary["ever_black"] is False
    assert Path(summary["out"]) == out

    score = tick.parse(out.read_text(encoding="utf-8"))
    assert [d for d in score.diagnostics if d.is_error] == []
    assert len(score.events) == summary["events"], "摘要 events 与文件不一致"
    assert score.note_count == summary["notes"], "摘要 notes 与文件不一致"
    assert score.metadata["instrument_id"] == "fwzs"


def test_record_default_out_only_creates_out_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    """不带 `--out`：只在 cwd 下新建 `out/`，不得把文件撒到 cwd 根部。"""
    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    assert main(["record", "--source", "synth", "--duration", "0.5", "--json"]) == 0
    after = {p.name for p in tmp_path.iterdir()}

    assert after - before == {"out"}, after - before
    produced = list((tmp_path / "out").glob("*.txt"))
    assert len(produced) == 1
    score = tick.parse(produced[0].read_text(encoding="utf-8"))
    assert score.note_count >= 1
    summary = _single_json_line(capsys)
    assert Path(summary["out"]).resolve() == produced[0].resolve()


def test_partial_summary_still_satisfies_contract(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """线程猝死时：`status=partial` + `thread_error`，**其余字段契约不变**，产物仍是合法 tick。

    与工程师 `test_session_diagnostics.py::test_cli_status_partial_when_thread_dies` 的分工：
    那边验「partial 标记能出现」；这里验「失败路径的摘要字段集合/类型不回退、落盘文件可解析」，
    避免 GUI/脚本按 §8.1 字段契约取不到 key 而 KeyError。
    """
    grid = KeyGrid(
        points=tuple(
            KeyPoint(u=0.15 + 0.7 * (i / 20), v=0.5, r=0.02) for i in range(21)
        ),
        key_count=21,
    )
    frame = np.full((360, 640, 3), 255, dtype=np.uint8)

    from yqrec.instruments import load_instrument
    from yqrec.session import Session
    from yqrec.types import Rect, ScreenTarget

    def build(args):  # type: ignore[no-untyped-def]
        """替换 CLI 的会话装配：保留真 instrument/参数，只换掉 manager 与 sampler。"""
        spec = load_instrument(args.instrument)
        session = Session(
            spec,
            manager=EndlessFrames(frame),
            sampler_factory=lambda _g, _w, _h: SlowFatalSampler(),
        )
        return session, ScreenTarget(0, Rect(0, 0, 640, 360)), grid, None

    monkeypatch.setattr("yqrec.cli._build_record_session", build)

    out = tmp_path / "partial.txt"
    code = main(["record", "--source", "synth", "--duration", "0.3", "--out", str(out), "--json"])
    assert code == 0

    summary = _single_json_line(capsys)
    assert summary["status"] == "partial"
    assert "驱动崩溃" in summary["thread_error"]
    assert SUMMARY_KEYS <= set(summary), f"partial 路径缺字段: {SUMMARY_KEYS - set(summary)}"
    assert summary["black_frames"] == 0 and summary["ever_black"] is False
    assert out.exists()
    score = tick.parse(out.read_text(encoding="utf-8"))
    assert [d for d in score.diagnostics if d.is_error] == []


def test_file_source_missing_path_exits_2_without_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """模拟「已装 cv2 + 有配置档」的机器：路径不存在 → 可读提示 + 退出码 2。

    注意（QA 发现）：不带 `--profile` 时 CLI 会先在 keygrid 校验处报 CONFIG，
    因此「文件不存在」这条提示只有在有配置档时才可能被用户看到。
    """
    monkeypatch.setattr(FileBackend, "is_available", classmethod(lambda _cls: True))

    from yqrec.types import KeyPoint, Profile, Rect, ScreenTarget

    keygrid = KeyGrid(
        points=tuple(KeyPoint(u=0.15 + 0.7 * (i / 20), v=0.5, r=0.02) for i in range(21)),
        key_count=21,
    )
    profile = Profile(
        name="qa",
        target=ScreenTarget(0, Rect(0, 0, 1920, 1080)),
        instrument="fwzs",
        keygrid=keygrid,
    )
    monkeypatch.setattr("yqrec.config.load_profile", lambda _name, root=None: profile)

    missing = tmp_path / "nope.mp4"
    code = main(
        [
            "record",
            "--source",
            "file",
            "--input",
            str(missing),
            "--profile",
            "qa",
            "--duration",
            "0.1",
        ]
    )
    err = capsys.readouterr().err
    assert code == 2

    # 只校验 CLI 自己的语义契约（错误码透传 + 可读原因）。
    # 「stderr 里不许有 traceback」放在 `test_file_source_missing_path_in_clean_process`
    # 里用真子进程验：同进程内 logging 的 StreamHandler 绑定的是上一个用例已经关掉的流，
    # 会额外喷 "--- Logging error ---"（已作为源码问题上报，与本用例关注的 CLI 语义无关）。
    assert "[E_CAPTURE_OPEN]" in err, err
    assert "无法启动捕获" in err, err
    assert "文件不存在" in err, "原始错误原因不得在包装时丢掉"


def test_file_source_missing_path_in_clean_process(tmp_path: Path) -> None:
    """干净子进程里跑真 CLI：缺 cv2 / 缺文件都必须是 2 + 可读提示，绝无 traceback。"""
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "yqrec",
            "record",
            "--source",
            "file",
            "--input",
            str(tmp_path / "nope.mp4"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.stderr.strip(), "至少要给一句可读提示"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑 bgi_sim.js")
def test_record_export_chain_matches_bgi_sim(tmp_path: Path, capsys) -> None:
    """record → tick → export bgi → bgi_sim.js：事件序列与 sheet.json 逐条一致。"""
    out = tmp_path / "rec.txt"
    assert main(["record", "--source", "synth", "--duration", "0.8", "--out", str(out)]) == 0
    capsys.readouterr()

    bgi_dir = tmp_path / "bgi"
    assert main(["export", "-f", "bgi", "-i", str(out), "-o", str(bgi_dir), "--name", "QA"]) == 0
    bundle = bgi_dir / "QA_bgi"
    sheet = json.loads((bundle / "sheet.json").read_text(encoding="utf-8"))

    score = tick.parse(out.read_text(encoding="utf-8"))
    assert sheet["events"] == bgi_js.build_events(score), "导出事件表与 build_events 不一致"
    assert sheet["events"], "空谱面无法验证链路"

    proc = subprocess.run(
        ["node", str(BGI_SIM), str(bundle)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    sim = json.loads(proc.stdout)
    assert sim["errors"] == [], sim["errors"]

    played = [e for e in sim["events"] if "error" not in e]
    head = played[: len(sheet["events"])]
    base = head[0]["t"] - float(sheet["events"][0]["t"])
    for got, expected in zip(head, sheet["events"], strict=True):
        assert abs(got["t"] - base - float(expected["t"])) <= 1.0, (got, expected)
        assert (got["key"], got["down"]) == (expected["key"], expected["down"]), (got, expected)
    assert len(played) >= len(sheet["events"])
