"""Витяг метаданих і нормалізованої аудіодоріжки через ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE = 16_000


class FfmpegMissing(RuntimeError):
    pass


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FfmpegMissing(f"{name} не знайдено в PATH. Постав: winget install Gyan.FFmpeg")
    return path


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(cwd) if cwd else None,
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-12:]
        raise RuntimeError("ffmpeg впав:\n" + "\n".join(tail))
    return result


def probe(video: Path) -> dict:
    """ffprobe → сирий словник зі стрімами й форматом."""
    result = _run([
        _binary("ffprobe"),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video),
    ])
    return json.loads(result.stdout)


def duration_of(probe_data: dict) -> float:
    return float(probe_data["format"]["duration"])


def has_audio(probe_data: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe_data.get("streams", []))


def resolution_of(probe_data: dict) -> tuple[int, int] | None:
    """Розмір кадру — потрібен, щоб кегль субтитрів був відносним, а не фіксованим."""
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            return int(stream["width"]), int(stream["height"])
    return None


def extract_audio(video: Path, dest: Path, sample_rate: int = SAMPLE_RATE) -> Path:
    """Аудіо → моно PCM 16 кГц. Саме те, що чекає Whisper.

    Гучність не чіпаємо: VAD і сам Whisper краще працюють на непозміненому
    сигналі, а нормалізація потрібна лише на фінальному рендері.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        _binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(video),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(dest),
    ])
    return dest


def ffmpeg_version() -> str:
    result = _run([_binary("ffmpeg"), "-version"])
    return result.stdout.splitlines()[0]
