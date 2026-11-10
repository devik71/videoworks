"""Викреслення: з позначених слів і ручних відрізків — у відрізки глушіння.

Чиста логіка без мережі й ffmpeg. Кожне позначене місце розширюємо навколо (ASR
ріже межі слів по живому), близькі сусіди зливаємо, щоб не вийшло «клацання»
звуку в короткому проміжку, і обрізаємо по запису.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

from videoworks.models import ManualSpan, Strikes, StruckWord

PAD = 0.15
MERGE_GAP = 0.25


def spans(
    words: Sequence[StruckWord],
    manual: Sequence[ManualSpan],
    *,
    duration: float,
    pad: float = PAD,
    merge_gap: float = MERGE_GAP,
) -> list[tuple[float, float]]:
    """Відсортовані, непересічні відрізки глушіння з точністю до мілісекунди."""
    padded = []
    for item in (*words, *manual):
        start, end = max(0.0, item.start - pad), min(duration, item.end + pad)
        if end > start:  # позначка за межами запису нічого не глушить
            padded.append((start, end))
    padded.sort()

    merged: list[list[float]] = []
    for start, end in padded:
        if merged and start - merged[-1][1] < merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def sha256(path: Path) -> str:
    """Відбиток файлу; порожній рядок, якщо файлу немає (транскрипту ще не було)."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return ""


def store(path: Path, strikes: Strikes, *, duration: float) -> None:
    """Перераховує spans і пише файл атомарно: читач бачить або старий, або новий цілий."""
    strikes.spans = spans(
        strikes.words,
        strikes.manual,
        duration=duration,
        pad=strikes.pad,
        merge_gap=strikes.merge_gap,
    )
    tmp = path.with_name(path.name + ".tmp")
    try:
        strikes.save(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
