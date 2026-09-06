"""Порівняння ASR-бекендів на тому самому матеріалі."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from videoworks.asr.base import ASRBackend
from videoworks.metrics import (
    BoundaryError,
    Token,
    WordError,
    boundary_error,
    speaker_agreement,
    tokens_of,
    word_error,
)
from videoworks.models import Transcript


@dataclass
class Run:
    label: str
    device: str
    elapsed: float
    audio_seconds: float
    transcript: Transcript
    warm: bool = True

    @property
    def realtime(self) -> float:
        """У скільки разів швидше за реальний час. Нижче 1 — повільніше за запис."""
        return self.audio_seconds / self.elapsed if self.elapsed else 0.0


@dataclass
class Comparison:
    run: Run
    words: WordError | None = None
    boundaries: BoundaryError | None = None
    speakers: float | None = None


def measure(
    backend: ASRBackend,
    audio: Path,
    *,
    label: str,
    audio_seconds: float,
    language: str | None = None,
    source: str | None = None,
) -> Run:
    # Завантаження й перше скачування ваг не мають потрапити в замір: інакше
    # маленька модель, яку щойно завантажили, виглядає повільнішою за велику.
    prepare = getattr(backend, "prepare", None)
    warm = prepare is not None
    if warm:
        prepare()

    started = time.perf_counter()
    transcript = backend.transcribe(audio, language=language, source=source)
    elapsed = time.perf_counter() - started

    return Run(
        label=label,
        device=transcript.backend.device or "?",
        elapsed=elapsed,
        audio_seconds=audio_seconds,
        transcript=transcript,
        warm=warm,
    )


def compare(run: Run, reference: list[Token] | None) -> Comparison:
    if not reference:
        return Comparison(run=run)

    hypothesis = tokens_of(run.transcript)
    # Межі слів мають сенс лише коли обидві сторони їх мають: MOSS віддає самі
    # репліки, і його «таймкоди слів» були б просто початком репліки.
    timed = any(token.start for token in reference) and run.transcript.has_word_timings
    labelled = any(token.speaker for token in reference)

    return Comparison(
        run=run,
        words=word_error(reference, hypothesis),
        boundaries=boundary_error(reference, hypothesis) if timed else None,
        speakers=speaker_agreement(reference, hypothesis) if labelled else None,
    )


def table(comparisons: list[Comparison]) -> str:
    """Проста текстова таблиця — щоб результат можна було вставити в нотатки."""
    header = f"{'бекенд':<28}{'пристрій':<10}{'×RT':>7}{'WER':>9}{'медіана':>10}{'спікери':>10}"
    lines = [header, "-" * len(header)]

    for item in comparisons:
        wer = f"{item.words.rate:.1%}" if item.words else "—"
        median = f"{item.boundaries.median * 1000:.0f} мс" if item.boundaries else "—"
        speakers = f"{item.speakers:.0%}" if item.speakers is not None else "—"
        speed = f"{item.run.realtime:>6.1f}×" if item.run.warm else f"{item.run.realtime:>6.1f}~"
        lines.append(
            f"{item.run.label:<28}{item.run.device:<10}{speed}{wer:>9}{median:>10}{speakers:>10}"
        )

    if any(not item.run.warm for item in comparisons):
        lines += ["", "~ бекенд не вміє прогрітись: у час входить завантаження моделі."]

    return "\n".join(lines)
