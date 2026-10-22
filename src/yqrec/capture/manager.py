"""捕获后端管理：注册、基准测速、自动回退（03 §2/§4.1）。

优先级（按目标类型路由）：
- ScreenTarget  : 默认 dxgi → gdi → file；用户在「后端」里选 **j-HDR** 时走
  hdr → dxgi → gdi（`FALLBACK_CHAINS`）
- WindowTarget  : wgc → printwindow
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logging import get_logger
from ..types import CaptureStats, CaptureTarget
from .base import BaseBackend

_log = get_logger("capture.manager")

FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    # 用户显式选 j-HDR：HDR 屏优先用它，失败才退 dxgi，再退 gdi。
    "hdr": ("hdr", "dxgi", "gdi"),
    # 保持不变：dxgi 打不开就退 gdi（不要偷偷换成 hdr，免得换了后端却不知道）。
    "dxgi": ("dxgi", "gdi"),
    "gdi": ("gdi",),
}
"""后端偏好 → 回退链。列表里**没有**出现的后端会被排到链尾（保持原有顺序）。"""

BACKEND_LABELS: dict[str, str] = {
    "hdr": "j-HDR",
    "dxgi": "dxgi",
    "gdi": "gdi",
    "wgc": "wgc",
    "printwindow": "PrintWindow",
    "file": "file",
}
"""后端内部名 → 界面显示名（`hdr` 对用户叫 `j-HDR`）。"""


def backend_label(name: str) -> str:
    return BACKEND_LABELS.get(name, name)


def order_candidates(
    cands: list[type[BaseBackend]], prefer: str | None
) -> list[type[BaseBackend]]:
    """按偏好对应的回退链排序候选后端（稳定排序，未列出的保持原顺序排在后面）。"""
    chain = FALLBACK_CHAINS.get((prefer or "").strip(), ())
    if not chain:
        return list(cands)

    def key(cls: type[BaseBackend]) -> int:
        name = getattr(cls, "name", "")
        return chain.index(name) if name in chain else len(chain)

    return sorted(cands, key=key)



@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    available: bool
    opened: bool
    fps: float = 0.0
    frames: int = 0
    error: str | None = None


def default_backends() -> list[type[BaseBackend]]:
    from .dxgi_backend import DxgiBackend
    from .file_backend import FileBackend
    from .gdi_backend import GdiBackend
    from .hdr_backend import HdrBackend
    from .printwindow_backend import PrintWindowBackend
    from .wgc_backend import WgcBackend

    # 顺序即「未指定偏好(或 auto)」时的默认优先级：M3e 起与产品默认一致 —— hdr 优先，
    # 再 dxgi、gdi（CLI 的 `--backend auto`、以及内部不带 prefer 的调用都走这条）。
    return [HdrBackend, DxgiBackend, WgcBackend, GdiBackend, PrintWindowBackend, FileBackend]


def backend_availability() -> dict[str, bool]:
    """各后端当前是否可用（`is_available()` 的结果）。

    打包后最常见的坑：`dxcam` 没进包 → DXGI 不可用 → 静默回退到 GDI（帧率骤降、
    短按被跳过）。启动时把这张表写进日志，一眼就能看出用的是哪个后端。
    """
    out: dict[str, bool] = {}
    for cls in default_backends():
        name = getattr(cls, "name", cls.__name__)
        try:
            out[name] = bool(cls.is_available())
        except Exception:
            out[name] = False
    return out


class CaptureManager:
    """对外只暴露 `start()/latest()/stop()/stats()`；内部负责选优与回退。"""

    def __init__(self, backends: list[type[BaseBackend]] | None = None) -> None:
        self._classes = backends or default_backends()
        self._current: BaseBackend | None = None
        self._target: CaptureTarget | None = None
        self._remaining: list[type[BaseBackend]] = []
        self._consecutive_errors = 0
        self._degraded_cb: Callable[[str], None] | None = None
        self._file_path: str | None = None
        self._start_errors: list[str] = []
        """本次 `start()` 里各后端的失败原因（按尝试顺序），供 UI 解释"为什么回退了"。"""

    # -- 后端选择 ---------------------------------------------------------- #

    def candidates(self, target: CaptureTarget) -> list[type[BaseBackend]]:
        out: list[type[BaseBackend]] = []
        for cls in self._classes:
            try:
                if not cls.is_available():
                    continue
            except Exception:
                continue
            inst = cls() if cls.__name__ != "FileBackend" else cls(self._file_path or "")
            try:
                if inst.supports(target):
                    out.append(cls)
            except Exception:
                continue
        return out

    def probe(self, target: CaptureTarget, *, frames: int = 30) -> list[ProbeResult]:
        """对每个候选后端打开并抓 frames 帧，测有效帧率。"""
        results: list[ProbeResult] = []
        for cls in self._classes:
            name = getattr(cls, "name", cls.__name__)
            try:
                avail = cls.is_available()
            except Exception as exc:
                results.append(ProbeResult(name=name, available=False, opened=False, error=str(exc)))
                continue
            if not avail:
                results.append(ProbeResult(name=name, available=False, opened=False))
                continue
            inst = cls() if cls.__name__ != "FileBackend" else cls(self._file_path or "")
            try:
                if not inst.supports(target):
                    results.append(ProbeResult(name=name, available=True, opened=False, error="不支持该目标"))
                    continue
                inst.open(target)
                t0 = time.perf_counter()
                got = 0
                while got < frames and time.perf_counter() - t0 < 5.0:
                    if inst.latest(timeout_ms=50) is not None:
                        got += 1
                elapsed = max(1e-6, time.perf_counter() - t0)
                results.append(
                    ProbeResult(name=name, available=True, opened=True, frames=got, fps=got / elapsed)
                )
                inst.close()
            except Exception as exc:
                results.append(ProbeResult(name=name, available=True, opened=False, error=str(exc)))
                try:
                    inst.close()
                except Exception:
                    pass
        return results

    # -- 生命周期 ---------------------------------------------------------- #

    def start(
        self,
        target: CaptureTarget,
        *,
        prefer: str | None = None,
        target_fps: int = 60,
    ) -> BaseBackend:
        self.stop()
        self._start_errors = []
        self._target = target
        cands = self.candidates(target)
        if not cands:
            raise RuntimeError("没有可用的捕获后端")
        cands = order_candidates(cands, prefer)
        last_exc: Exception | None = None
        for cls in cands:
            inst = cls() if cls.__name__ != "FileBackend" else cls(self._file_path or "")
            try:
                inst.open(target, target_fps=target_fps)
            except Exception as exc:
                last_exc = exc
                name = getattr(cls, "name", cls)
                self._start_errors.append(f"{name}: {exc}")
                _log.warning("后端 %s 打开失败: %s", name, exc)
                continue
            self._current = inst
            self._remaining = cands[cands.index(cls) + 1 :]
            self._consecutive_errors = 0
            _log.info("捕获后端: %s", getattr(inst, "name", cls))
            return inst
        raise RuntimeError(f"所有后端均打开失败: {last_exc}")

    @property
    def start_errors(self) -> list[str]:
        """本次 `start()` 里失败的后端与原因（`["dxgi: Invalid Region ..."]`）。"""
        return list(self._start_errors)

    def latest(self, timeout_ms: int = 100):
        if self._current is None:
            raise RuntimeError("尚未 start")
        try:
            got = self._current.latest(timeout_ms=timeout_ms)
            if got is not None:
                self._consecutive_errors = 0
            return got
        except Exception as exc:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 20:
                self._degrade(exc)
            return None

    def _degrade(self, exc: Exception) -> None:
        name = getattr(self._current, "name", "?")
        _log.error("后端 %s 连续失败，尝试回退: %s", name, exc)
        try:
            self._current.close()  # type: ignore[union-attr]
        except Exception:
            pass
        self._current = None
        if self._degraded_cb:
            self._degraded_cb(name)
        if self._target is not None and self._remaining:
            cands = self._remaining
            self._remaining = []
            for cls in cands:
                inst = cls() if cls.__name__ != "FileBackend" else cls(self._file_path or "")
                try:
                    inst.open(self._target)
                except Exception:
                    continue
                self._current = inst
                self._consecutive_errors = 0
                return

    def stop(self) -> None:
        if self._current is not None:
            try:
                self._current.close()
            finally:
                self._current = None
        self._consecutive_errors = 0

    def stats(self) -> CaptureStats:
        if self._current is None:
            return CaptureStats(fps=0.0)
        return self._current.stats()

    def on_degraded(self, cb: Callable[[str], None]) -> None:
        self._degraded_cb = cb

    def set_file_source(self, path: str) -> None:
        """回归测试用：FileBackend 的输入路径。"""
        self._file_path = path
