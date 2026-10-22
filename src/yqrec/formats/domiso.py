"""domiso（DoMiSo 简谱）只读解析 —— 计划在 P2 实现（03 §3.9）。

语法要点：
- 头部 `bpm = <int>`、`1 = <A-G><oct?><#|b>`；
- 音符正则 `^(?<arpeggio>~)?(?<scale>-*|\\+*)(?<note>[0-7])(?<semitone>#|b)?(?<notelen>(?:/|-|\\.)*)\\s?$`；
- 音高 = BaseMidi + BaseOffset[note] + 八度 ±12 + 升降；
- 时值：`/`×n 二分、`-`×n 加拍、`.`×n 附点；
- `.dms` 为私有二进制，需先走 DomisoDmsDecoder（简单 XOR）。
"""

from __future__ import annotations

from ..errors import NotImplementedYet
from ..types import Score


def parse(text: str) -> Score:  # noqa: ARG001
    raise NotImplementedYet("domiso 解析", phase="P2")


def decode_dms(data: bytes) -> bytes:
    """`.dms` 私有格式解码占位（P2 实现，简单 XOR）。"""
    raise NotImplementedYet("domiso .dms 解码", phase="P2")
