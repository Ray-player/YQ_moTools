"""全代码合成的视频配乐（不引用任何外部音效/音乐素材，也**不含任何音效**）。

编曲思路
--------
一首 88 秒、A 小调五声音阶的 ambient / 音乐盒小品，段落跟画面一一对应：

| 段落 | 和弦 | 配器 |
|---|---|---|
| 0–7 开场 | Am | 长垫 + 三音主题（空、留白） |
| 7–14 问题 | Fmaj7 | 低音区单音，稀疏 |
| 14–25 流程 | C | 主题上行 + 分解和弦进入 |
| 25–39 识别 | G | 依旧长垫，只多一个长音 |
| 39–55 界面 | Am | 音更少，让位给字幕 |
| 55–67 演奏 | F | 保持稀疏，不抢画面 |
| 67–75 导出 | C | 上行推进 |
| 75–88 收尾 | A(add9) | 大调色彩落下，长尾 |

三层：`pad`（和弦垫，主体）+ `bell`（稀疏长音点缀）+ `shimmer`（极轻的高频微光）。
没有鼓、没有低音脉冲、没有分解和弦 —— 任何固定周期都会变成"节拍器"，这里全部去掉。

产物：out/intro_video/audio.wav（48 kHz / 16 bit / 立体声）
"""

from __future__ import annotations

import pathlib
import wave

import numpy as np

import timeline as TL

SR = 48000
DUR = TL.DURATION
N = int(DUR * SR)
OUT = pathlib.Path(__file__).resolve().parents[2] / "out" / "intro_video"
OUT.mkdir(parents=True, exist_ok=True)

RNG = np.random.default_rng(20261026)


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def tvec(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SR), dtype=np.float64) / SR


def lp(x: np.ndarray, cutoff: float, taps: int = 96) -> np.ndarray:
    """一阶低通的 FIR 近似（指数核）。"""
    a = float(np.exp(-2.0 * np.pi * cutoff / SR))
    k = a ** np.arange(taps)
    k /= k.sum()
    return np.convolve(x, k, mode="same")


def adsr(seconds: float, attack: float, release: float) -> np.ndarray:
    t = tvec(seconds)
    env = np.clip(t / max(attack, 1e-4), 0.0, 1.0)
    env *= np.clip((seconds - t) / max(release, 1e-4), 0.0, 1.0)
    return env


# --------------------------------------------------------------------------- #
# 音色
# --------------------------------------------------------------------------- #
def bell(freq: float, *, dur: float = 3.0, amp: float = 0.3, music_box: bool = True) -> np.ndarray:
    """音乐盒 / 钢片琴：整数谐波 + 指数衰减 + 极短起振。"""
    t = tvec(dur)
    parts = (1.0, 0.26, 0.08, 0.03) if music_box else (1.0, 0.38, 0.14, 0.05)
    sig = np.zeros_like(t)
    for index, weight in enumerate(parts):
        f = freq * (index + 1) * (1.0 + 0.0006 * index)
        sig += weight * np.sin(2 * np.pi * f * t + index * 0.55)
    decay = dur * 0.52
    attack = 0.075
    env = np.clip(t / attack, 0.0, 1.0) * np.exp(-np.maximum(t - attack, 0.0) / decay)
    return (sig * env * amp / 2.0).astype(np.float32)


def pad(freqs: tuple[float, ...], seconds: float, *, amp: float = 0.2) -> np.ndarray:
    """和弦垫：微失谐双振荡 + 慢 LFO + 低通。"""
    t = tvec(seconds)
    sig = np.zeros_like(t)
    for index, f in enumerate(freqs):
        for detune in (-0.0016, 0.0016):
            ff = f * (1.0 + detune)
            sig += 0.5 * np.sin(2 * np.pi * ff * t + index * 1.1)
            sig += 0.15 * np.sin(2 * np.pi * ff * 2 * t + index * 0.4)
            sig += 0.05 * np.sin(2 * np.pi * ff * 3 * t + index * 1.9)
    lfo = 1.0 + 0.025 * np.sin(2 * np.pi * 0.09 * t + 0.6)
    sig = lp(sig * lfo, 1300.0)
    return (sig * adsr(seconds, 2.8, 3.4) * amp / max(len(freqs), 1)).astype(np.float32)


def shimmer(seconds: float, *, amp: float = 0.05) -> np.ndarray:
    t = tvec(seconds)
    sig = np.zeros_like(t)
    for i, f in enumerate((2093.0, 2637.0, 3136.0, 4186.0)):
        trem = 1.0 + 0.45 * np.sin(2 * np.pi * (2.9 + i * 0.6) * t)
        sig += np.sin(2 * np.pi * f * t) * trem / (i + 1.7)
    return (sig * adsr(seconds, 1.0, seconds * 0.6) * amp / 3.0).astype(np.float32)


# --------------------------------------------------------------------------- #
# 混响
# --------------------------------------------------------------------------- #
def reverb(x: np.ndarray, *, amount: float = 0.45) -> np.ndarray:
    wet = np.zeros_like(x)
    taps = ((0.037, 0.44), (0.053, 0.32), (0.079, 0.25), (0.107, 0.18), (0.149, 0.12))
    for delay, gain in taps:
        d = int(delay * SR)
        wet[d:] += x[:-d] * gain
    return (x + lp(wet, 3800.0) * amount).astype(np.float32)


# --------------------------------------------------------------------------- #
# 分轨
# --------------------------------------------------------------------------- #
def add(bus: np.ndarray, audio: np.ndarray, at: float) -> None:
    start = int(at * SR)
    if start >= N:
        return
    end = min(start + audio.size, N)
    bus[start:end] += audio[: end - start]


def track_pads() -> np.ndarray:
    bus = np.zeros(N, dtype=np.float32)
    for start, stop, root, intervals in TL.CHORDS:
        base = TL.NOTE_FREQ[root]
        freqs = tuple(TL.semitone(base, n) for n in intervals)
        add(bus, pad(freqs, stop - start + 2.6, amp=0.32), max(start - 1.2, 0.0))
    return bus


def track_melody() -> np.ndarray:
    bus = np.zeros(N, dtype=np.float32)
    for at, note, dur, amp in TL.MUSIC_MELODY:
        add(bus, bell(TL.NOTE_FREQ[note], dur=dur, amp=amp, music_box=True), at)
    return bus


def track_shimmer() -> np.ndarray:
    bus = np.zeros(N, dtype=np.float32)
    add(bus, shimmer(9.0, amp=0.035), 0.0)
    add(bus, shimmer(14.0, amp=0.030), 74.0)
    return bus


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
def main() -> pathlib.Path:
    music = (
        track_pads() * 1.00
        + reverb(track_melody(), amount=0.55) * 0.95
        + track_shimmer() * 0.55
    )

    # 轻微立体声加宽 + 软限幅
    music = music + np.roll(music, int(0.013 * SR)) * 0.14
    music = np.tanh(music * 1.35) / np.tanh(1.35)

    fade_in = int(0.9 * SR)
    music[:fade_in] *= np.linspace(0, 1, fade_in) ** 0.75
    fade_out = int(4.0 * SR)
    music[-fade_out:] *= np.linspace(1, 0, fade_out) ** 1.5

    peak = float(np.max(np.abs(music))) or 1.0
    music = music / peak * 0.72
    stereo = np.stack([music, np.roll(music, 320) * 0.96], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype(np.int16)

    path = OUT / "audio.wav"
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(SR)
        fh.writeframes(pcm.tobytes())
    print(f"audio -> {path}  ({DUR:.1f}s, peak {peak:.2f}, 纯音乐无音效)")
    return path


if __name__ == "__main__":
    main()
