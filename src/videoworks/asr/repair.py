"""Лагодження вироджених таймкодів, які видає сам ASR.

На довгому матеріалі Whisper повертає помітну частку слів із `start == end`:
на 17-хвилинному запису це було 117 слів із 2249, тобто 5%. Іноді такі слова
йдуть поспіль, і тоді ціла фраза не має тривалості взагалі.

Для пайплайну, що ріже по межах слів, слово без тривалості — дірка: на ньому
не можна ні поставити шов, ні побудувати репліку субтитрів. Тут ми не вигадуємо
дані, а розподіляємо вироджену послідовність по проміжку між сусідами, час яких
відомий.
"""

from __future__ import annotations

from videoworks.models import Segment, Transcript, Word


def spread_degenerate(transcript: Transcript) -> tuple[Transcript, int]:
    """Повертає транскрипт із рознесеними таймкодами і кількість полагоджених слів."""
    words = transcript.words()
    if not words:
        return transcript, 0

    fixed: dict[int, tuple[float, float]] = {}
    index = 0
    while index < len(words):
        if words[index].end > words[index].start:
            index += 1
            continue

        run_end = index
        while run_end < len(words) and words[run_end].end <= words[run_end].start:
            run_end += 1

        # Проміжок, у якому насправді звучала вироджена послідовність.
        left = words[index - 1].end if index > 0 else words[index].start
        right = words[run_end].start if run_end < len(words) else words[run_end - 1].end
        if right <= left:
            right = left + 0.04 * (run_end - index)

        step = (right - left) / (run_end - index)
        for offset, position in enumerate(range(index, run_end)):
            start = left + offset * step
            fixed[position] = (round(start, 3), round(start + step, 3))

        index = run_end

    if not fixed:
        return transcript, 0

    position = 0
    segments: list[Segment] = []
    for segment in transcript.segments:
        rebuilt: list[Word] = []
        for word in segment.words:
            span = fixed.get(position)
            rebuilt.append(
                Word(w=word.w, start=span[0], end=span[1], prob=word.prob) if span else word
            )
            position += 1
        segments.append(
            Segment(
                id=segment.id,
                start=rebuilt[0].start if rebuilt else segment.start,
                end=rebuilt[-1].end if rebuilt else segment.end,
                text=segment.text,
                speaker=segment.speaker,
                words=rebuilt,
            )
        )

    repaired = transcript.model_copy(update={"segments": segments})
    return repaired, len(fixed)
