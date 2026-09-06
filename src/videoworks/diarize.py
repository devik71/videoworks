"""Перенесення міток спікерів на транскрипт із таймкодами слів.

Жоден бекенд не дає одночасно найкращого рівня слова й діаризації без закритих
ваг: faster-whisper має слова, але не знає спікерів; MOSS знає спікерів, але не
має слів; whisperX має обидва, проте його діаризація тягне gated-ваги pyannote.

Тому спікерів переносимо з одного транскрипту на інший за перекриттям у часі.
"""

from __future__ import annotations

from videoworks.models import Segment, Speaker, Transcript, Word


def _overlap(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def speaker_at(start: float, end: float, source: Transcript) -> str | None:
    """Спікер, чия репліка найбільше перекриває заданий проміжок."""
    best: tuple[float, str] | None = None
    for segment in source.segments:
        if segment.speaker is None:
            continue
        overlap = _overlap(start, end, segment.start, segment.end)
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, segment.speaker)
    return best[1] if best else None


def assign_speakers(transcript: Transcript, diarization: Transcript) -> Transcript:
    """Розмічає слова спікерами з іншого транскрипту, ріжучи сегменти по змінах.

    Сегмент, усередині якого змінюється спікер, розділяється: інакше репліка
    двох людей злилася б в одну й субтитри приписали б її одному.
    """
    groups: list[tuple[str | None, list[Word]]] = []

    for segment in transcript.segments:
        for word in segment.words:
            speaker = speaker_at(word.start, word.end, diarization)
            if groups and groups[-1][0] == speaker:
                groups[-1][1].append(word)
            else:
                groups.append((speaker, [word]))

    from videoworks.text import join_words

    segments = [
        Segment(
            id=index,
            start=words[0].start,
            end=words[-1].end,
            text=join_words(words),
            speaker=speaker,
            words=words,
        )
        for index, (speaker, words) in enumerate(groups)
    ]

    labels = sorted({s.speaker for s in segments if s.speaker})
    return Transcript(
        source=transcript.source,
        duration=transcript.duration,
        language=transcript.language,
        backend=transcript.backend,
        speakers=[Speaker(id=label) for label in labels],
        segments=segments,
    )
