"""EDL: побудова, перевірка, перемапування транскрипту.

EDL — контракт між агентом і рендером. Агент не пише команд ffmpeg: він
декларує, які інтервали лишити й у якому порядку. Це дає апрув плану до
рендеру, повторюваність і можливість перерахувати таймкоди без другого ASR.
"""

from __future__ import annotations

import math
from fractions import Fraction
from itertools import pairwise

from videoworks.diagnostics import Problem, errors
from videoworks.models import AudioSettings, Edl, EdlSegment, Segment, Transcript, Word
from videoworks.text import join_words

__all__ = [
    "EPSILON",
    "PreflightFailed",
    "Problem",
    "errors",
    "from_spans",
    "locate",
    "preflight",
    "quantize",
    "remap_transcript",
    "spans_from_speech",
]

# Допуск на арифметику з плаваючою комою: усе, що дрібніше за мілісекунду,
# не має значення ні для відео, ні для субтитрів.
EPSILON = 0.001


# --------------------------------------------------------------------------- #
# Побудова
# --------------------------------------------------------------------------- #


def from_spans(source: str, spans: list[tuple[float, float]], audio: AudioSettings | None = None) -> Edl:
    """Інтервали оригіналу → EDL із послідовною розкладкою на таймлайні."""
    segments: list[EdlSegment] = []
    clock = 0.0
    for start, end in spans:
        if end - start <= EPSILON:
            continue
        segments.append(
            EdlSegment(src_in=round(start, 3), src_out=round(end, 3), dst_out=round(clock, 3))
        )
        clock += end - start
    return Edl(source=source, segments=segments, audio=audio or AudioSettings())


def spans_from_speech(
    transcript: Transcript,
    *,
    max_pause: float = 0.6,
    pad: float = 0.08,
    duration: float | None = None,
) -> list[tuple[float, float]]:
    """Інтервали з мовленням: паузи довші за `max_pause` вирізаються.

    Padding обовʼязковий: різати рівно по межі слова означає зрізати атаку
    приголосного на початку й хвіст на кінці, і монтаж чути на слух.
    """
    words = [w for w in transcript.words() if w.w.strip()]
    if not words:
        return []

    limit = duration if duration is not None else transcript.duration
    spans: list[tuple[float, float]] = []
    start = words[0].start
    previous = words[0].end

    for word in words[1:]:
        if word.start - previous > max_pause:
            spans.append((start, previous))
            start = word.start
        previous = word.end
    spans.append((start, previous))

    padded = [(max(0.0, s - pad), min(limit, e + pad)) for s, e in spans]

    merged: list[tuple[float, float]] = []
    for span in padded:
        if merged and span[0] <= merged[-1][1] + EPSILON:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)
    return merged


def quantize(edl: Edl, fps: float | Fraction) -> Edl:
    """Притягує межі сегментів до сітки кадрів.

    Без цього ffmpeg округлює кожен сегмент сам, похибки накопичуються, і
    змонтоване відео виявляється довшим за план: на 203 сегментах — майже на
    секунду. Таймкоди ж перемаплюються точною арифметикою, тож субтитри
    поїхали б тим сильніше, чим далі від початку.

    Після притягування довжина кожного сегмента — ціле число кадрів, і
    округлювати вже нічого.
    """
    rate = Fraction(fps).limit_denominator(1000000)
    segments: list[EdlSegment] = []
    clock = 0  # у кадрах, щоб не накопичувати похибку в секундах

    for segment in edl.segments:
        # Розширюємо назовні, а не округлюємо до найближчого: інакше
        # квантування зрізало б до 20 мс на межі, тобто атаку приголосного.
        start = math.floor(Fraction(segment.src_in).limit_denominator(1000000) * rate)
        end = math.ceil(Fraction(segment.src_out).limit_denominator(1000000) * rate)
        if end <= start:
            continue  # сегмент коротший за кадр — його все одно нема чим показати
        segments.append(
            EdlSegment(
                src_in=float(round(Fraction(start) / rate, 6)),
                src_out=float(round(Fraction(end) / rate, 6)),
                dst_out=float(round(Fraction(clock) / rate, 6)),
            )
        )
        clock += end - start

    return Edl(source=edl.source, segments=segments, audio=edl.audio)


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


class PreflightFailed(RuntimeError):
    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("; ".join(p.message for p in problems if p.level == "error"))


def preflight(edl: Edl, *, source_duration: float | None = None, min_frame: float = 0.04) -> list[Problem]:
    """Усе, що перевіряємо ДО запуску ffmpeg. Fail-closed: помилка блокує рендер."""
    problems: list[Problem] = []

    if not edl.segments:
        problems.append(Problem("error", "EDL порожній: жодного сегмента"))
        return problems

    for index, segment in enumerate(edl.segments):
        if segment.src_in < -EPSILON:
            problems.append(Problem("error", f"сегмент {index}: src_in {segment.src_in} < 0"))
        if source_duration is not None and segment.src_out > source_duration + EPSILON:
            problems.append(
                Problem(
                    "error",
                    f"сегмент {index}: src_out {segment.src_out} за межами джерела "
                    f"({source_duration:.3f} с)",
                )
            )
        if segment.duration < min_frame:
            problems.append(
                Problem(
                    "warning",
                    f"сегмент {index}: тривалість {segment.duration:.3f} с коротша за кадр",
                )
            )

    # Таймлайн призначення має бути суцільним: діра чи наїзд означають, що
    # dst_out порахований неправильно, і субтитри поїдуть після перемапування.
    ordered = sorted(edl.segments, key=lambda s: s.dst_out)
    clock = 0.0
    for index, segment in enumerate(ordered):
        if abs(segment.dst_out - clock) > EPSILON:
            problems.append(
                Problem(
                    "error",
                    f"сегмент {index}: dst_out {segment.dst_out:.3f} не збігається з "
                    f"очікуваним {clock:.3f} — розрив або наїзд на таймлайні",
                )
            )
            break
        clock += segment.duration

    for left, right in pairwise(sorted(edl.segments, key=lambda s: s.src_in)):
        if right.src_in < left.src_out - EPSILON:
            problems.append(
                Problem(
                    "warning",
                    f"інтервали джерела перетинаються: [{left.src_in}, {left.src_out}] "
                    f"і [{right.src_in}, {right.src_out}]",
                )
            )

    if edl.audio.normalize and not -70.0 <= edl.audio.target_lufs <= -5.0:
        problems.append(
            Problem("error", f"target_lufs {edl.audio.target_lufs} поза розумними межами")
        )

    if edl.duration <= 0:
        problems.append(Problem("error", "сумарна тривалість нульова"))

    return problems


# --------------------------------------------------------------------------- #
# Перемапування таймкодів
# --------------------------------------------------------------------------- #


def locate(time: float, segments: list[EdlSegment]) -> int | None:
    for index, segment in enumerate(segments):
        if segment.src_in - EPSILON <= time < segment.src_out + EPSILON:
            return index
    return None


def _shift(time: float, segment: EdlSegment) -> float:
    clamped = min(max(time, segment.src_in), segment.src_out)
    return clamped - segment.src_in + segment.dst_out


def remap_transcript(transcript: Transcript, edl: Edl) -> Transcript:
    """Перераховує таймкоди транскрипту під змонтоване відео.

    Серце пайплайну. Повторний ASR по вже змонтованому файлу дав би інші слова
    й іншу пунктуацію і зруйнував би звʼязок із рішеннями агента; тут же
    перетворення чисто арифметичне й детерміноване.

    Слово вважається збереженим, якщо його середина потрапила в один із
    інтервалів EDL: край, зрізаний на межі, все одно чути у фіналі.
    """
    kept: list[tuple[int, int, str | None, Word]] = []

    for segment in transcript.segments:
        for word in segment.words:
            index = locate((word.start + word.end) / 2, edl.segments)
            if index is None:
                continue
            target = edl.segments[index]
            start = _shift(word.start, target)
            end = _shift(word.end, target)
            kept.append((
                index,
                segment.id,
                segment.speaker,
                Word(w=word.w, start=round(start, 3), end=round(max(end, start), 3), prob=word.prob),
            ))

    kept.sort(key=lambda item: (item[3].start, item[3].end))

    # Новий сегмент починається там, де змінився шматок EDL або вихідний
    # сегмент: інакше склеїлися б репліки з різних місць запису.
    groups: list[tuple[int, int, str | None, list[Word]]] = []
    for index, source_id, speaker, word in kept:
        if groups and (groups[-1][0], groups[-1][1]) == (index, source_id):
            groups[-1][3].append(word)
        else:
            groups.append((index, source_id, speaker, [word]))

    segments = [
        Segment(
            id=position,
            start=round(words[0].start, 3),
            end=round(words[-1].end, 3),
            text=join_words(words),
            speaker=speaker,
            words=words,
        )
        for position, (_, _, speaker, words) in enumerate(groups)
    ]

    return Transcript(
        source=transcript.source,
        duration=round(edl.duration, 3),
        language=transcript.language,
        backend=transcript.backend,
        speakers=transcript.speakers,
        segments=segments,
    )
