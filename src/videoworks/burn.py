"""Вжигання субтитрів у кадр і мʼякий мукс.

Вижжений файл не можна ні перекласти, ні виправити, тому поруч завжди кладемо
версію з субтитрами окремим треком.
"""

from __future__ import annotations

from pathlib import Path

from videoworks.ingest import _binary, _run

# ISO 639-1 → 639-2/B, як того хоче Matroska.
_LANG3 = {"uk": "ukr", "en": "eng", "pl": "pol", "de": "deu", "fr": "fra", "es": "spa"}


def burn(
    video: Path,
    subtitles: Path,
    dest: Path,
    *,
    encoder: str = "libx264",
    quality: int = 18,
    preset: str | None = None,
) -> Path:
    """Рендерить відео з вижженими субтитрами.

    ffmpeg приймає шлях до субтитрів усередині рядка фільтра, де двокрапка
    після літери диска та зворотні слеші сприймаються як синтаксис. Замість
    того щоб екранувати це вручну, запускаємо ffmpeg із робочою текою поруч із
    файлом субтитрів і передаємо саму назву — тоді шляху у фільтрі просто немає.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    args = [
        _binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(video.resolve()),
        "-vf", f"subtitles={subtitles.name}",
    ]
    if encoder == "nvenc":
        args += ["-c:v", "h264_nvenc", "-preset", preset or "p5", "-cq", str(quality)]
    else:
        args += ["-c:v", encoder, "-preset", preset or "medium", "-crf", str(quality)]
    args += ["-pix_fmt", "yuv420p", "-c:a", "copy", str(dest.resolve())]

    _run(args, cwd=subtitles.parent)
    return dest


def mux_soft(video: Path, subtitles: Path, dest: Path, *, language: str = "uk") -> Path:
    """Кладе субтитри окремим треком у MKV, не перекодовуючи відео."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    codec = "ass" if subtitles.suffix.lower() == ".ass" else "srt"

    _run([
        _binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(video.resolve()),
        "-i", str(subtitles.resolve()),
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-c:s", codec,
        "-metadata:s:s:0", f"language={_LANG3.get(language, language)}",
        str(dest.resolve()),
    ])
    return dest
