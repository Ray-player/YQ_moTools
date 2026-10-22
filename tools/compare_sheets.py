"""谱面互校工具：把任意两份谱面（tick / YQRec JSON / BetterGI 曲谱）放在一起对比。

用法::

    python tools/compare_sheets.py a.txt b.json
    python tools/compare_sheets.py a.txt b.json --json     # 机器可读

它会做三件事：

1. **各自的自洽性**：解析 → 导出成 BetterGI `type:"midi"` → 再解析，比较音数/时刻误差
   （这条能量化"我们的转换有没有丢东西"）；
2. **风格画像**：音数、和弦比例、复音度、时长、音域；
3. **跨文件比对**：和弦序列相似度（difflib）+ 时间轴线性拟合（斜率=节拍比例，残差=节奏偏差）。

注意：跨文件比对只有在**同一编配**下才有意义；不同编配（例如一份只有旋律、一份带伴奏）
相似度必然很低，这时请看第 1 项而不是第 3 项。
"""

from __future__ import annotations

import argparse
import difflib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yqrec.formats import bgi_sheet, tick  # noqa: E402
from yqrec.instruments import load_instrument  # noqa: E402
from yqrec.scorefile import record_tick_text, score_from_record  # noqa: E402
from yqrec.types import NoteEvent, Score  # noqa: E402


def load_any(path: Path) -> tuple[str, Score]:
    """按扩展名/内容猜格式并解析成 `Score`。"""
    if path.suffix.lower() == ".txt":
        return "tick", tick.parse(path.read_text(encoding="utf-8"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "tick" in data and "notes" not in data:
        _ = record_tick_text(data)
        return "yqrec", score_from_record(data)
    if bgi_sheet.is_bgi_sheet(data):
        return f"bgi:{data.get('type', '?')}", bgi_sheet.parse_sheet(data)
    raise SystemExit(f"认不出格式：{path}")


def notes_of(score: Score) -> list[NoteEvent]:
    return [e for e in score.events if isinstance(e, NoteEvent)]


def profile(score: Score) -> dict:
    notes = notes_of(score)
    if not notes:
        return {"notes": 0}
    sizes = [len(n.keys) for n in notes]
    end = max(n.onset_ms + n.sustain_ms for n in notes)
    pitches = [semi for n in notes for semi in n.keys]
    return {
        "notes": len(notes),
        "chords": sum(1 for s in sizes if s > 1),
        "max_polyphony": max(sizes),
        "duration_ms": round(end),
        "duration": f"{int(end // 60000)}:{int(end % 60000 // 1000):02d}",
        "unique_keys": len(set(pitches)),
    }


def roundtrip(score: Score, spec) -> dict:
    """Score → BGI(midi) → Score，统计时刻/音高误差。"""
    notes = notes_of(score)
    if not notes:
        return {"error": "空谱面"}
    sheet = bgi_sheet.build_sheet(score, spec, name="roundtrip")
    back = notes_of(bgi_sheet.parse_sheet(sheet))
    if len(back) != len(notes):
        return {
            "error": f"音数不一致：{len(notes)} → {len(back)}",
            "tokens": sheet["notes"].count("|") + 1,
        }
    onset_err = [abs(a.onset_ms - b.onset_ms) for a, b in zip(notes, back, strict=True)]
    key_bad = sum(1 for a, b in zip(notes, back, strict=True) if set(a.keys) != set(b.keys))
    return {
        "tokens": sheet["notes"].count("|") + 1,
        "notes": len(back),
        "onset_max_ms": round(max(onset_err), 2),
        "onset_p95_ms": round(statistics.quantiles(onset_err, n=20)[-1], 2),
        "tuple_mismatch": key_bad,
        "match_rate": f"{(1 - key_bad / len(notes)) * 100:.2f}%",
    }


def compare(a: Score, b: Score) -> dict:
    na, nb = notes_of(a), notes_of(b)
    seq_a = [tuple(sorted(n.keys)) for n in na]
    seq_b = [tuple(sorted(n.keys)) for n in nb]
    ratio = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False).ratio()
    out: dict = {
        "chord_sequence_ratio": round(ratio, 4),
        "count_ratio": f"{len(seq_a)}:{len(seq_b)}",
    }
    if len(seq_a) == len(seq_b) and na:
        oa = [n.onset_ms for n in na]
        ob = [n.onset_ms for n in nb]
        denom = sum(x * x for x in ob) or 1.0
        scale = sum(x * y for x, y in zip(oa, ob, strict=True)) / denom
        resid = [x - scale * y for x, y in zip(oa, ob, strict=True)]
        out["tempo_scale"] = round(scale, 4)
        out["residual_rms_ms"] = round((sum(r * r for r in resid) / len(resid)) ** 0.5, 1)
        out["residual_max_ms"] = round(max(abs(r) for r in resid), 1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="谱面互校")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    spec = load_instrument("fwzs")
    fmt_a, score_a = load_any(args.first)
    fmt_b, score_b = load_any(args.second)

    report = {
        "first": {"path": str(args.first), "format": fmt_a, **profile(score_a)},
        "second": {"path": str(args.second), "format": fmt_b, **profile(score_b)},
        "roundtrip_first": roundtrip(score_a, spec),
        "roundtrip_second": roundtrip(score_b, spec),
        "compare": compare(score_a, score_b),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    for tag in ("first", "second"):
        item = report[tag]
        print(f"[{tag}] {item['format']}  {Path(item['path']).name}")
        print(
            f"        音数 {item['notes']}（和弦 {item['chords']}，最大复音 {item['max_polyphony']}）"
            f" 时长 {item['duration']}  音域 {item['unique_keys']} 键"
        )
        rt = report[f"roundtrip_{tag}"]
        if "error" in rt:
            print(f"        → 往返失败：{rt['error']}")
        else:
            print(
                f"        → 往返 BGI(midi)：{rt['tokens']} tokens / {rt['notes']} 音，"
                f"时刻误差 max {rt['onset_max_ms']}ms（p95 {rt['onset_p95_ms']}ms），"
                f"音高一致率 {rt['match_rate']}"
            )
    cmp_ = report["compare"]
    print("[对比]")
    print(f"        和弦序列相似度 {cmp_['chord_sequence_ratio']}（词数比 {cmp_['count_ratio']}）")
    if "tempo_scale" in cmp_:
        print(
            f"        时间轴：斜率 {cmp_['tempo_scale']}（A/B），"
            f"残差 RMS {cmp_['residual_rms_ms']}ms / max {cmp_['residual_max_ms']}ms"
        )
    else:
        print("        时间轴：音数不同，无法逐音拟合")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
