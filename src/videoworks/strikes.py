"""Викреслення: з позначених слів і ручних відрізків — у відрізки глушіння.

Чиста логіка без мережі й ffmpeg. Кожне позначене місце розширюємо навколо (ASR
ріже межі слів по живому), близькі сусіди зливаємо, щоб не вийшло «клацання»
звуку в короткому проміжку, і обрізаємо по запису.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from videoworks.models import ManualSpan, Strikes, StruckWord, Transcript

PAD = 0.15
MERGE_GAP = 0.25

PHRASE_PAUSE = 0.6  # довша пауза між словами — нова фраза
PHRASE_MAX_WORDS = 14  # інакше фраза не вміщається в рядок сторінки
MUSIC_GAP = 2.0  # проміжок без мови довший за це показуємо окремим рядком «музика»
LOW_PROB = 0.5  # слова з меншою впевненістю ASR на сторінці тьмяні
REMATCH_SHIFT = 0.3  # на стільки може зсунутись початок слова між двома транскрипціями
MIN_MANUAL = 0.05  # ManualSpan не буває нульовим, тож загублене слово має хоч цю довжину


@dataclass(frozen=True)
class Phrase:
    start: float
    end: float
    first: int  # глобальний індекс першого слова в transcript.words()
    count: int


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


def phrases(transcript: Transcript) -> list[Phrase]:
    """Групує слова у фрази для сторінки; індекс слова лишається глобальним.

    Порожнє слово ні в яку фразу не входить, але індекс споживає, а ще й рве фразу:
    фраза — це неперервний діапазон індексів, дірку в ньому показати нічим.
    """
    found: list[Phrase] = []
    index = 0
    for segment in transcript.segments:
        first = count = 0
        start = last_end = 0.0
        for word in segment.words:
            blank = not word.w.strip()
            if count and (
                blank or word.start - last_end > PHRASE_PAUSE or count >= PHRASE_MAX_WORDS
            ):
                found.append(Phrase(start, last_end, first, count))
                count = 0
            if not blank:
                if not count:
                    first, start = index, word.start
                count += 1
                last_end = word.end
            index += 1
        if count:
            found.append(Phrase(start, last_end, first, count))
    return found


def music_gaps(found: list[Phrase], duration: float) -> list[tuple[float, float]]:
    """Довгі проміжки без мови, включно з початком і кінцем запису."""
    gaps = []
    cursor = 0.0
    for phrase in found:
        if phrase.start - cursor > MUSIC_GAP:
            gaps.append((cursor, phrase.start))
        cursor = max(cursor, phrase.end)
    if duration - cursor > MUSIC_GAP:
        gaps.append((cursor, duration))
    return gaps


def _norm(text: str) -> str:
    """Текст для порівняння слів: без регістру, розділових знаків і пробілів."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def rematch(old: Strikes, transcript: Transcript, transcript_sha: str) -> tuple[Strikes, int]:
    """Переносить позначки на заново зроблений транскрипт.

    Слово шукаємо за текстом і часом разом: той самий текст далеко в часі — це інше
    слово. Що не знайшлось, лишається ручним відрізком зі старими межами: намір
    людини не губимо. Друге значення — скільки слів так «відклеїлось».
    """
    pool = [(i, w) for i, w in enumerate(transcript.words()) if w.w.strip()]
    taken: set[int] = set()
    kept: list[StruckWord] = []
    manual = list(old.manual)
    unglued = 0
    for struck in sorted(old.words, key=lambda item: item.start):
        wanted = _norm(struck.w)
        best = None
        for i, word in pool:
            if i in taken or _norm(word.w) != wanted:
                continue
            overlap = min(word.end, struck.end) - max(word.start, struck.start)
            shift = abs(word.start - struck.start)
            if (overlap > 0 or shift <= REMATCH_SHIFT) and (best is None or shift < best[0]):
                best = (shift, i, word)
        if best is None:
            end = struck.end if struck.end > struck.start else struck.start + MIN_MANUAL
            manual.append(ManualSpan(start=struck.start, end=end))
            unglued += 1
        else:
            _, i, word = best
            taken.add(i)  # одне нове слово не може нести дві позначки
            kept.append(StruckWord(w=word.w, start=word.start, end=word.end))
    carried = old.model_copy(
        update={"transcript_sha256": transcript_sha, "words": kept, "manual": manual, "spans": []}
    )
    return carried, unglued


def load(
    path: Path, *, transcript: Transcript, transcript_sha: str, source: str
) -> tuple[Strikes, int]:
    """Читає strikes.json; з іншим відбитком транскрипту переносить позначки, але не пише."""
    path = Path(path)
    if not path.exists():
        return Strikes(source=source, transcript_sha256=transcript_sha), 0
    try:
        # utf-8-sig: редактор у Windows міг дописати BOM, файл від цього не зіпсований
        saved = Strikes.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path.name} зіпсований: {exc}") from exc
    if saved.transcript_sha256 == transcript_sha:
        return saved, 0
    return rematch(saved, transcript, transcript_sha)


def indices(strikes: Strikes, transcript: Transcript) -> list[int]:
    """Глобальні індекси викреслених слів; слово, якого вже нема в транскрипті, пропускаємо."""
    where = {(w.w, w.start, w.end): i for i, w in enumerate(transcript.words())}
    found = (where.get((s.w, s.start, s.end)) for s in strikes.words)
    return sorted({i for i in found if i is not None})


def from_indices(
    chosen: Sequence[int],
    manual: Sequence[tuple[float, float]],
    *,
    transcript: Transcript,
    transcript_sha: str,
    source: str,
) -> Strikes:
    """Strikes з номерів слів і ручних відрізків; spans рахує store."""
    everything = transcript.words()
    words = []
    for i in sorted(set(chosen)):
        if not 0 <= i < len(everything):
            raise ValueError(f"слова з індексом {i} немає: у транскрипті {len(everything)} слів")
        word = everything[i]
        if not word.w.strip():
            raise ValueError(f"слово {i} порожнє, викреслювати нічого")
        words.append(StruckWord(w=word.w, start=word.start, end=word.end))
    return Strikes(
        source=source,
        transcript_sha256=transcript_sha,
        words=words,
        manual=[ManualSpan(start=start, end=end) for start, end in manual],
    )


def _r(value: float) -> float:
    """Число для JSON: три знаки, і жодних NaN/Infinity, яких JSON не знає."""
    return round(value, 3) if math.isfinite(value) else 0.0


def state(transcript: Transcript, strikes: Strikes, *, title: str, unglued: int = 0) -> dict:
    """Усе, що малює сторінка. Відрізки рахуємо заново, а не беремо зі strikes.spans."""
    found = phrases(transcript)
    cuts = spans(
        strikes.words,
        strikes.manual,
        duration=transcript.duration,
        pad=strikes.pad,
        merge_gap=strikes.merge_gap,
    )
    return {
        "title": title,
        "duration": _r(transcript.duration),
        "transcript": strikes.transcript_sha256,
        "words": [
            {
                "w": w.w,
                "start": _r(w.start),
                "end": _r(w.end),
                "dim": w.prob is not None and w.prob < LOW_PROB,
            }
            for w in transcript.words()
        ],
        "phrases": [
            {"start": _r(p.start), "end": _r(p.end), "first": p.first, "count": p.count}
            for p in found
        ],
        "gaps": [[_r(a), _r(b)] for a, b in music_gaps(found, transcript.duration)],
        "struck": indices(strikes, transcript),
        "manual": [[_r(m.start), _r(m.end)] for m in strikes.manual],
        "spans": [[_r(a), _r(b)] for a, b in cuts],
        "counts": {
            "words": len(strikes.words),
            "manual": len(strikes.manual),
            "seconds": round(sum(b - a for a, b in cuts), 2),
        },
        "unglued": unglued,
    }
