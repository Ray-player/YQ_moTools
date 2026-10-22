"""音频引擎：预加载乐器采样并播放（03 §4.2）。

一期用 pygame.mixer；`enabled=False` 时不导入 pygame（便于无音频环境测试）。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..logging import get_logger
from ..types import InstrumentSpec

_log = get_logger("player.audio")

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
"""别让 pygame 在导入时往控制台打宣传语（窗口化 exe 用不到，日志也干净）。"""


class AudioEngine:
    def __init__(
        self,
        spec: InstrumentSpec,
        *,
        assets_dir: Path | str,
        enabled: bool = True,
        volume: float = 0.8,
    ) -> None:
        self._spec = spec
        self._assets = Path(assets_dir)
        self._enabled = enabled
        self._volume = volume
        self._pygame = None
        self._sounds: dict[str, object] = {}

    def preload(self) -> None:
        if not self._enabled:
            return
        try:
            import pygame
        except Exception as exc:  # pragma: no cover - 依赖缺失
            _log.warning("pygame 不可用，音频关闭: %s", exc)
            self._enabled = False
            return
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        pygame.mixer.set_num_channels(32)
        self._pygame = pygame
        for note, rel in self._spec.note_to_audio.items():
            path = self._assets / rel
            if not path.exists():
                _log.warning("缺少采样: %s", path)
                continue
            snd = pygame.mixer.Sound(str(path))
            snd.set_volume(self._volume)
            self._sounds[note] = snd
        _log.info("预加载采样 %d/%d", len(self._sounds), self._spec.key_count)

    def play(self, note: str) -> None:
        if not self._enabled or self._pygame is None:
            return
        snd = self._sounds.get(note)
        if snd is None:
            return
        try:
            snd.play()
        except Exception as exc:  # pragma: no cover
            _log.warning("播放失败 %s: %s", note, exc)

    def stop_all(self) -> None:
        if self._pygame is not None:
            self._pygame.mixer.stop()

    def close(self) -> None:
        self.stop_all()
        if self._pygame is not None:
            try:
                self._pygame.mixer.quit()
            except Exception:
                pass
        self._pygame = None
        self._sounds.clear()
