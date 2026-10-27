"""把帧渲染 + 音轨合成成 mp4。

用法
----
    python tools/intro_video/make.py --preview 3,9,18,30   # 抽帧检查排版
    python tools/intro_video/make.py                        # 渲染全片 → dist/
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

import render  # noqa: E402
import timeline as TL  # noqa: E402

FPS = 60
OUT_DIR = ROOT / "out" / "intro_video"
DIST = ROOT / "dist"


def make_images() -> tuple[QImage, QImage]:
    canvas = QImage(render.W, render.H, QImage.Format.Format_RGB888)
    scratch = QImage(render.W, render.H, QImage.Format.Format_ARGB32_Premultiplied)
    return canvas, scratch


def preview(times: list[float]) -> None:
    app = QGuiApplication(sys.argv[:1])
    render.init_fonts()
    canvas, scratch = make_images()
    out = OUT_DIR / "preview"
    out.mkdir(parents=True, exist_ok=True)
    for t in times:
        render.render_frame(t, canvas, scratch)
        path = out / f"t{t:05.1f}.png".replace(".", "_", 1)
        canvas.save(str(path))
        print("preview ->", path)
    app.quit()


def encode(music: pathlib.Path | None = None) -> pathlib.Path:
    """`music` 给了就用外部曲目替换配乐（自动裁剪到 88s + 头尾淡入淡出）。"""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    audio = OUT_DIR / "audio.wav"
    if music is None and not audio.exists():
        import audio as audio_mod

        audio_mod.main()

    audio_filters: list[str] = []
    if music is not None:
        audio = music
        audio_filters = [
            f"atrim=0:{TL.DURATION:.2f}",
            "asetpts=PTS-STARTPTS",
            "afade=t=in:st=0:d=1.5",
            f"afade=t=out:st={TL.DURATION - 4.0:.2f}:d=4.0",
            "volume=0.85",
        ]

    app = QGuiApplication(sys.argv[:1])
    render.init_fonts()
    canvas, scratch = make_images()
    out_path = DIST / "YQ_moTools_介绍片.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frames = int(round(TL.DURATION * FPS))
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{render.W}x{render.H}", "-r", str(FPS), "-i", "-",
        "-i", str(audio),
        "-c:v", "libx264", "-preset", "slow", "-crf", "17",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
        "-x264-params", "keyint=120:min-keyint=60:ref=3",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest", "-movflags", "+faststart", str(out_path),
    ]
    if audio_filters:
        cmd[cmd.index("-c:a"):cmd.index("-c:a")] = ["-af", ",".join(audio_filters)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    started = time.time()
    for index in range(frames):
        t = index / FPS
        render.render_frame(t, canvas, scratch)
        proc.stdin.write(canvas.constBits().tobytes())  # type: ignore[union-attr]
        if index % 300 == 0:
            elapsed = time.time() - started
            print(f"  {index}/{frames}  {elapsed:5.1f}s", flush=True)
    proc.stdin.close()  # type: ignore[union-attr]
    code = proc.wait()
    if code != 0:
        raise SystemExit(f"ffmpeg 失败: {code}")
    print(f"video -> {out_path}  ({time.time() - started:.1f}s, {frames} 帧)")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", type=str, default="")
    ap.add_argument("--music", type=str, default="", help="外部配乐文件（mp3/wav），替换自合成音轨")
    args = ap.parse_args()
    if args.preview:
        preview([float(x) for x in args.preview.split(",") if x.strip()])
    else:
        encode(pathlib.Path(args.music) if args.music else None)


if __name__ == "__main__":
    main()
