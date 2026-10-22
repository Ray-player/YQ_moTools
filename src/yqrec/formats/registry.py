"""格式注册表（03 §2）：`Formatter` 协议 + 注册器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..types import Score


@dataclass(frozen=True, slots=True)
class FormatInfo:
    id: str
    name: str
    ext: str
    can_read: bool = True
    can_write: bool = True


@runtime_checkable
class Formatter(Protocol):
    info: FormatInfo

    def parse(self, text: str) -> Score: ...

    def format(self, score: Score) -> str: ...


_REGISTRY: dict[str, Formatter] = {}


def register(cls: type[Formatter]) -> type[Formatter]:
    """类装饰器：实例化并注册（要求类无参可构造）。"""
    inst = cls()
    if inst.info.id in _REGISTRY:
        raise ValueError(f"格式 id 重复: {inst.info.id}")
    _REGISTRY[inst.info.id] = inst
    return cls


def get(fmt_id: str) -> Formatter:
    try:
        return _REGISTRY[fmt_id]
    except KeyError as exc:
        raise KeyError(f"未知格式: {fmt_id!r}，可用: {sorted(_REGISTRY)}") from exc


def all_formats() -> list[FormatInfo]:
    return [f.info for f in _REGISTRY.values()]
