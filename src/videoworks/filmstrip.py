"""Контактні аркуші — картинка на вимогу, коли тексту вже не досить.

Агент не дивиться відео підряд: це не влазить у контекст і нічого не додає.
Але коли треба перевірити, що саме в кадрі на певній ділянці, дешевше зібрати
один PNG із сіткою кадрів, ніж декодувати відео цілком.
"""

from __future__ import annotations

from pathlib import Path

from videoworks.ingest import _binary, _run


def contact_sheet(
    video: Path,
    dest: Path,
    *,
    start: float,
    end: float,
    columns: int = 4,
    rows: int = 3,
    width: int = 320,
) -> list[float]:
    """Збирає сітку кадрів і повертає час кожної клітинки.

    Підписів на картинці немає навмисно: drawtext на Windows вимагає шляху до
    файлу шрифту й екранування всередині фільтра. Замість цього віддаємо список
    таймкодів — агент читає його з виводу й співставляє з клітинками сам.
    """
    duration = end - start
    if duration <= 0:
        raise ValueError(f"порожній діапазон: {start}–{end}")

    cells = columns * rows
    # Беремо крок як duration/(cells+1): так гарантовано набереться кадрів
    # не менше, ніж клітинок, навіть із похибкою частоти.
    step = duration / (cells + 1)
    dest.parent.mkdir(parents=True, exist_ok=True)

    _run([
        _binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(video.resolve()),
        "-t", f"{duration:.3f}",
        "-vf",
        f"fps={1 / step:.6f},scale={width}:-2,tile={columns}x{rows}:padding=4:color=0x202020",
        "-frames:v", "1",
        str(dest.resolve()),
    ])

    return [round(start + index * step, 3) for index in range(cells)]
