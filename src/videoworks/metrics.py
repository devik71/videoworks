"""Метрики якості ASR: WER, точність меж слів, узгодженість спікерів.

Вибір бекенда без цифр — вгадування. Тут рівно те, що дозволяє порівняти
faster-whisper, whisperX і MOSS на тому самому матеріалі.

Вирівнювання квадратичне за довжиною: для бенчмарку бери уривок на 2–5 хвилин,
не годинний запис.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass

from videoworks.models import Transcript

# Апострофи в українській пишуть щонайменше чотирма символами; для порівняння
# текстів це та сама літера, і різниця в них не має рахуватись за помилку.
_APOSTROPHES = dict.fromkeys(map(ord, "'`ʼ‘’´"), "'")
_PUNCT = re.compile(r"[^\w'\-]+", re.UNICODE)


@dataclass(frozen=True)
class Token:
    text: str
    start: float = 0.0
    end: float = 0.0
    speaker: str | None = None

    @property
    def key(self) -> str:
        return normalize(self.text)


def normalize(word: str) -> str:
    """Регістр, апострофи й пунктуація не мають впливати на WER."""
    folded = unicodedata.normalize("NFC", word).translate(_APOSTROPHES).casefold()
    return _PUNCT.sub("", folded).strip("-'")


def tokens_of(transcript: Transcript) -> list[Token]:
    """Слова з таймкодами; для бекендів без рівня слова — текст реплік.

    Так WER лишається порівнянним навіть із MOSS, який віддає лише репліки.
    Точність меж слів на таких транскриптах рахувати не можна — це перевіряється
    окремо через `Transcript.has_word_timings`.
    """
    tokens: list[Token] = []
    for segment in transcript.segments:
        if segment.words:
            tokens += [
                Token(text=word.w, start=word.start, end=word.end, speaker=segment.speaker)
                for word in segment.words
            ]
        else:
            tokens += [
                Token(text=word, start=segment.start, end=segment.end, speaker=segment.speaker)
                for word in segment.text.split()
            ]
    return tokens


def tokens_from_text(text: str) -> list[Token]:
    """Еталон без таймкодів — тоді доступний лише WER."""
    return [Token(text=word) for word in text.split() if normalize(word)]


# --------------------------------------------------------------------------- #
# Вирівнювання
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Op:
    kind: str  # "hit" | "sub" | "del" | "ins"
    reference: int | None
    hypothesis: int | None


def align(reference: list[str], hypothesis: list[str]) -> list[Op]:
    """Класична редакційна відстань зі збереженням шляху."""
    rows, columns = len(reference), len(hypothesis)
    distance = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows + 1):
        distance[row][0] = row
    for column in range(columns + 1):
        distance[0][column] = column

    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            cost = 0 if reference[row - 1] == hypothesis[column - 1] else 1
            distance[row][column] = min(
                distance[row - 1][column - 1] + cost,
                distance[row - 1][column] + 1,
                distance[row][column - 1] + 1,
            )

    operations: list[Op] = []
    row, column = rows, columns
    while row > 0 or column > 0:
        if row > 0 and column > 0:
            cost = 0 if reference[row - 1] == hypothesis[column - 1] else 1
            if distance[row][column] == distance[row - 1][column - 1] + cost:
                operations.append(
                    Op("hit" if cost == 0 else "sub", row - 1, column - 1)
                )
                row, column = row - 1, column - 1
                continue
        if row > 0 and distance[row][column] == distance[row - 1][column] + 1:
            operations.append(Op("del", row - 1, None))
            row -= 1
            continue
        operations.append(Op("ins", None, column - 1))
        column -= 1

    operations.reverse()
    return operations


# --------------------------------------------------------------------------- #
# WER
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WordError:
    hits: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def reference_length(self) -> int:
        return self.hits + self.substitutions + self.deletions

    @property
    def rate(self) -> float:
        if not self.reference_length:
            return 0.0
        return (self.substitutions + self.deletions + self.insertions) / self.reference_length

    def __str__(self) -> str:
        return (
            f"WER {self.rate:.1%} (заміни {self.substitutions}, "
            f"пропуски {self.deletions}, вставки {self.insertions})"
        )


def word_error(reference: list[Token], hypothesis: list[Token]) -> WordError:
    operations = align([t.key for t in reference], [t.key for t in hypothesis])
    counted = {kind: 0 for kind in ("hit", "sub", "del", "ins")}
    for operation in operations:
        counted[operation.kind] += 1
    return WordError(counted["hit"], counted["sub"], counted["del"], counted["ins"])


# --------------------------------------------------------------------------- #
# Точність меж слів
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoundaryError:
    matched: int
    mean: float
    median: float
    p90: float
    within_100ms: float

    def __str__(self) -> str:
        return (
            f"медіана {self.median * 1000:.0f} мс · p90 {self.p90 * 1000:.0f} мс"
            f" · у межах 100 мс {self.within_100ms:.0%}"
        )


def boundary_error(reference: list[Token], hypothesis: list[Token]) -> BoundaryError | None:
    """Розбіжність початків слів, порахована лише на впізнаних словах.

    Це та метрика, що вирішує долю монтажу: різка йде по межах слів, і похибка
    в 200 мс — це зрізаний склад.
    """
    operations = align([t.key for t in reference], [t.key for t in hypothesis])
    deltas = [
        abs(reference[op.reference].start - hypothesis[op.hypothesis].start)
        for op in operations
        if op.kind == "hit"
    ]
    if not deltas:
        return None

    ordered = sorted(deltas)
    index = min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))
    return BoundaryError(
        matched=len(deltas),
        mean=statistics.fmean(deltas),
        median=statistics.median(deltas),
        p90=ordered[index],
        within_100ms=sum(1 for d in deltas if d <= 0.1) / len(deltas),
    )


# --------------------------------------------------------------------------- #
# Спікери
# --------------------------------------------------------------------------- #


def speaker_agreement(reference: list[Token], hypothesis: list[Token]) -> float | None:
    """Частка впізнаних слів із правильним спікером.

    Це не DER: мітки спікерів довільні, тому спершу зіставляємо їх жадібно за
    найчастішим збігом, а вже потім рахуємо влучність. Значення порівнянне між
    бекендами, але з опублікованим DER його зіставляти не можна.
    """
    operations = [op for op in align([t.key for t in reference], [t.key for t in hypothesis])
                  if op.kind == "hit"]
    pairs = [
        (reference[op.reference].speaker, hypothesis[op.hypothesis].speaker)
        for op in operations
    ]
    pairs = [(ref, hyp) for ref, hyp in pairs if ref is not None and hyp is not None]
    if not pairs:
        return None

    counts: dict[tuple[str, str], int] = {}
    for pair in pairs:
        counts[pair] = counts.get(pair, 0) + 1

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for (ref, hyp), _ in sorted(counts.items(), key=lambda item: -item[1]):
        if hyp not in mapping and ref not in taken:
            mapping[hyp] = ref
            taken.add(ref)

    correct = sum(1 for ref, hyp in pairs if mapping.get(hyp) == ref)
    return correct / len(pairs)
