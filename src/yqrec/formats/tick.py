"""tick 格式：解析 + 序列化（语法与语义见 03 §3.6/§3.7）。

格式示例::

    /*
      @name 光辉岁月
      @instrument 风物之诗
    */
    DBZ.731 731 MXB.731 731

语义：
- 字母串 = 同时按下（和弦）；
- `.数字` 紧跟音符，表示该音延音；同和弦取最大值；
- 数字 token = 休止毫秒；
- `/* */` 与 `//` 为注释，注释内 `@key value` 为元数据（仅在首个音符/休止前生效）。
"""

from __future__ import annotations

import re

from ..types import Diagnostic, NoteEvent, RestEvent, Score, ScoreEvent
from .registry import FormatInfo, register

_META_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_\-]*)\s+(.+)$")
MAX_SUSTAIN_UNLIMITED = 1e9


@register
class TickFormat:
    info = FormatInfo(id="tick", name="数字 Tick 谱", ext=".txt")

    # -- 解析 -------------------------------------------------------------- #

    def parse(self, text: str) -> Score:
        score = Score()
        n = len(text)
        pos = 0
        cursor_ms = 0.0
        seen_event = False

        while pos < n:
            ch = text[pos]
            if ch.isspace():
                pos += 1
                continue
            if text.startswith("/*", pos):
                start = pos
                end = text.find("*/", pos + 2)
                if end < 0:
                    score.diagnostics.append(
                        Diagnostic("多行注释未闭合", start, 2, "error")
                    )
                    pos = n
                    break
                raw = text[start : end + 2]
                if not seen_event:
                    self._parse_metadata(raw, start, score)
                pos = end + 2
                continue
            if text.startswith("//", pos):
                start = pos
                end = text.find("\n", pos + 2)
                end = n if end < 0 else end
                raw = text[start:end]
                if not seen_event:
                    self._parse_metadata(raw, start, score)
                pos = end
                continue
            if ch.isdigit():
                start = pos
                while pos < n and text[pos].isdigit():
                    pos += 1
                raw = text[start:pos]
                ms = int(raw)
                score.events.append(RestEvent(ms=float(ms)))
                cursor_ms += ms
                seen_event = True
                continue
            if ch.isalpha():
                start = pos
                keys: list[str] = []
                sustain = 0.0
                while pos < n and text[pos].isalpha():
                    keys.append(text[pos].upper())
                    pos += 1
                    if pos < n and text[pos] == ".":
                        j = pos + 1
                        while j < n and (text[j].isdigit() or text[j] == "."):
                            j += 1
                        raw_num = text[pos + 1 : j]
                        try:
                            val = float(raw_num)
                        except ValueError:
                            val = 0.0
                        if val > sustain:
                            sustain = val
                        pos = j
                # 同一 token 内重复键（如颤音写法 HVH）去重，保留首次出现顺序
                keys = list(dict.fromkeys(keys))
                score.events.append(
                    NoteEvent(keys=tuple(keys), onset_ms=cursor_ms, sustain_ms=sustain)
                )
                seen_event = True
                continue

            # 未知字符：跳过并记 warning
            start = pos
            while pos < n and not (
                text[pos].isspace()
                or text[pos].isdigit()
                or text[pos].isalpha()
                or text.startswith("/*", pos)
                or text.startswith("//", pos)
            ):
                pos += 1
            if pos == start:  # 理论上不会发生，防御性前进
                pos += 1
            score.diagnostics.append(
                Diagnostic(
                    f"未识别的字符: {text[start:pos]!r}", start, pos - start, "warning"
                )
            )
        return score

    def _parse_metadata(self, raw: str, offset: int, score: Score) -> None:
        body = raw
        if body.startswith("/*"):
            body = body[2:-2] if body.endswith("*/") else body[2:]
        elif body.startswith("//"):
            body = body[2:]
        line_offset = offset
        for line in body.splitlines():
            stripped = line.strip()
            m = _META_RE.match(stripped)
            if m:
                score.metadata.setdefault(m.group(1), m.group(2).strip())
            line_offset += len(line) + 1

    # -- 序列化 ------------------------------------------------------------ #

    def format(self, score: Score) -> str:
        """canonical 形式：有延音 `A.500 S.500`；无延音 `AS`；事件间以空格分隔。"""
        parts: list[str] = []
        if score.metadata:
            lines = ["/*"]
            for k, v in score.metadata.items():
                lines.append(f"  @{k} {v}")
            lines.append("*/")
            parts.append("\n".join(lines))

        buf: list[str] = []
        for ev in score.events:
            buf.append(self._format_event(ev))
        if buf:
            parts.append(" ".join(buf))
        return "\n".join(parts).strip() + "\n"

    def _format_event(self, ev: ScoreEvent) -> str:
        if isinstance(ev, RestEvent):
            return str(int(round(ev.ms)))
        keys = "".join(ev.keys)
        if ev.sustain_ms >= 50.0:
            dur = str(int(round(ev.sustain_ms)))
            return " ".join(f"{k}.{dur}" for k in ev.keys)
        return keys


# -- 便捷函数（供 CLI/测试使用） ------------------------------------------- #

_DEFAULT = TickFormat()


def parse(text: str) -> Score:
    return _DEFAULT.parse(text)


def format(score: Score) -> str:  # noqa: A001 - 与协议同名
    return _DEFAULT.format(score)
