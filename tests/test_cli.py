"""CLI 冒烟：formats / export / record / version。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yqrec.cli import main
from yqrec.formats import tick


def test_formats_command(capsys):
    assert main(["formats"]) == 0
    out = capsys.readouterr().out
    assert "tick" in out


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_export_bgi(tmp_path):
    src = tmp_path / "song.txt"
    src.write_text("/*\n @name 小星星\n @instrument 风物之诗\n*/\nA 200 S 200\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    assert main(["export", "-i", str(src), "-f", "bgi", "-o", str(out_dir), "--name", "小星星"]) == 0
    sheet = next(out_dir.glob("*_bgi/sheet.json"))
    data = json.loads(sheet.read_text(encoding="utf-8"))
    assert len(data["events"]) >= 4


def test_export_tick_rewrite(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("A 100", encoding="utf-8")
    dst = tmp_path / "b.txt"
    assert main(["export", "-i", str(src), "-f", "tick", "-o", str(dst)]) == 0
    assert "A" in dst.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# record 子命令
# --------------------------------------------------------------------------- #


def test_record_synth_writes_parseable_tick(tmp_path: Path, capsys) -> None:
    out = tmp_path / "rec.txt"
    code = main(["record", "--source", "synth", "--duration", "0.6", "--out", str(out)])
    assert code == 0
    assert out.exists()
    score = tick.parse(out.read_text(encoding="utf-8"))
    assert not [d for d in score.diagnostics if d.is_error]
    assert score.note_count >= 1, "0.6s 内至少有 1 次按下（400ms 起）"
    assert "written:" in capsys.readouterr().out


def test_record_synth_json_summary(tmp_path: Path, capsys) -> None:
    out = tmp_path / "rec.txt"
    code = main(
        ["record", "--source", "synth", "--duration", "0.6", "--out", str(out), "--json"]
    )
    assert code == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    summary = json.loads(lines[-1])
    assert summary["status"] == "ok"
    assert summary["events"] >= 1
    assert summary["duration_ms"] > 0
    assert summary["fps"] > 0
    assert summary["black_frames"] == 0
    assert Path(summary["out"]) == out


def test_record_synth_default_out_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["record", "--source", "synth", "--duration", "0.5"]) == 0
    produced = list((tmp_path / "out").glob("*.txt"))
    assert len(produced) == 1
    assert tick.parse(produced[0].read_text(encoding="utf-8")).note_count >= 1
    capsys.readouterr()


def test_record_synth_hints_default_keygrid(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # 避免默认 out/ 落到仓库根目录
    assert main(["record", "--source", "synth", "--duration", "0.4"]) == 0
    assert "内置默认键位布局" in capsys.readouterr().err


def test_record_screen_requires_rect(capsys) -> None:
    assert main(["record", "--source", "screen"]) == 2
    assert "--rect" in capsys.readouterr().err


def test_record_screen_without_profile_reports_config(capsys) -> None:
    """有 rect 但没配置档 → Session 报 CONFIG（不启动后端、无 traceback）。"""
    assert main(["record", "--source", "screen", "--rect", "0,0,320,180"]) == 2
    err = capsys.readouterr().err
    assert "配置档" in err
    assert "Traceback" not in err


def test_record_bad_rect_reports_usage(capsys) -> None:
    assert main(["record", "--source", "screen", "--rect", "1,2,3"]) == 2
    assert "参数错误" in capsys.readouterr().err


def test_record_file_requires_input(capsys) -> None:
    assert main(["record", "--source", "file"]) == 2
    assert "--input" in capsys.readouterr().err


def test_record_file_without_cv2_hints(tmp_path: Path, capsys) -> None:
    from yqrec.capture.file_backend import FileBackend

    if FileBackend.is_available():
        pytest.skip("本环境已安装 cv2，跳过缺依赖提示用例")
    media = tmp_path / "a.mp4"
    media.write_bytes(b"")
    assert main(["record", "--source", "file", "--input", str(media)]) == 2
    assert "OpenCV" in capsys.readouterr().err


def test_record_unknown_source_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["record", "--source", "nope"])
