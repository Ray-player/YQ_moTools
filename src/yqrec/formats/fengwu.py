"""fengwu（风物文本谱）只读解析 —— 计划在 P2 实现（03 §3.8）。

语法要点：
- 头部 `t = <float>` 与 `gc = ...`；缺省 t=0.22；
- 空格是休止，但"空格后紧跟 `/`、换行或文件尾"算 Blank；
- `/` 小节线；`(...)` 分组/和弦；
- 每个休止计数 = 166 ticks。
"""

from __future__ import annotations

from ..errors import NotImplementedYet
from ..types import Score


def parse(text: str) -> Score:  # noqa: ARG001
    raise NotImplementedYet("fengwu 解析", phase="P2")
