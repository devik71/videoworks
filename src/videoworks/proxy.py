"""Легка копія відео для викреслювача.

Вертикальний HEVC із телефона не кожен браузер грає, а 4K перемотується повільно.
Копія — H.264 висотою 960 з ключовим кадром щосекунди: перемотування точне й
швидке. Час у копії той самий, що в оригіналі, тож позначки, зроблені на ній,
лягають на оригінал без перерахунку.
"""

from __future__ import annotations

import os
from pathlib import Path

from videoworks import ingest


def command(video: Path, target: Path) -> list[str]:
    """Команда ffmpeg, що збирає копію `video` у `target`."""
    # Пара «ключ значення» в одному рядку читається легше, як і в ingest.
    # fmt: off
    return [
        ingest._binary("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i", str(video),
        # Лише перша відео- й аудіодоріжка: data-потоки телефона mp4 не прийме.
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        # Парна ширина — вимога yuv420p; пропорції зберігаються.
        "-vf", "scale=-2:960",
        "-pix_fmt", "yuv420p",
        # Ключовий кадр щосекунди: браузер перемотує до найближчого, тож точно.
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        "-c:a", "aac",
        "-b:a", "96k",
        # moov на початку: браузер почне грати, не дочитавши файл до кінця.
        "-movflags", "+faststart",
        "-f", "mp4",
        str(target),
    ]
    # fmt: on


def stale(video: Path, target: Path) -> bool:
    """Копії немає або оригінал новіший — її треба зібрати заново."""
    try:
        return target.stat().st_mtime < video.stat().st_mtime
    except FileNotFoundError:
        return True


def build(video: Path, target: Path) -> Path:
    """Збирає копію через тимчасовий файл: обірвана збірка не лишає півфайлу."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        ingest._run(command(video, tmp))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
