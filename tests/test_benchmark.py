from __future__ import annotations

from pathlib import Path

from videoworks.benchmark import Run, compare, measure, table
from videoworks.metrics import tokens_from_text, tokens_of
from videoworks.models import BackendInfo, Segment, Transcript, Word


def build(text: str, *, with_words: bool = True, speaker: str | None = None) -> Transcript:
    pieces = text.split()
    words = [
        Word(w=piece, start=round(i * 0.5, 3), end=round(i * 0.5 + 0.4, 3))
        for i, piece in enumerate(pieces)
    ]
    return Transcript(
        source="raw.mp4",
        duration=len(pieces) * 0.5,
        language="uk",
        backend=BackendInfo(name="fake", model="m", device="cuda"),
        segments=[
            Segment(
                id=0,
                start=0.0,
                end=len(pieces) * 0.5,
                text=text,
                speaker=speaker,
                words=words if with_words else [],
            )
        ],
    )


class FakeBackend:
    name = "fake"

    def __init__(self, transcript: Transcript, *, warmable: bool = True) -> None:
        self._transcript = transcript
        self.prepared = False
        if warmable:
            self.prepare = self._prepare

    def _prepare(self) -> None:
        self.prepared = True

    def transcribe(self, audio, *, language=None, source=None) -> Transcript:
        return self._transcript


def test_measure_warms_the_backend_before_timing() -> None:
    backend = FakeBackend(build("один два три"))
    run = measure(backend, Path("a.wav"), label="fake", audio_seconds=10.0)
    assert backend.prepared
    assert run.warm


def test_measure_marks_backends_that_cannot_warm_up() -> None:
    backend = FakeBackend(build("один два три"), warmable=False)
    run = measure(backend, Path("a.wav"), label="fake", audio_seconds=10.0)
    assert not run.warm


def test_realtime_factor_uses_audio_length() -> None:
    run = Run("fake", "cuda", elapsed=2.0, audio_seconds=10.0, transcript=build("один"))
    assert run.realtime == 5.0


def test_realtime_is_zero_when_nothing_elapsed() -> None:
    run = Run("fake", "cuda", elapsed=0.0, audio_seconds=10.0, transcript=build("один"))
    assert run.realtime == 0.0


def test_compare_without_reference_reports_speed_only() -> None:
    run = measure(FakeBackend(build("один два")), Path("a.wav"), label="f", audio_seconds=1.0)
    result = compare(run, None)
    assert result.words is None
    assert result.boundaries is None


def test_compare_computes_word_error_against_plain_text() -> None:
    run = measure(FakeBackend(build("один два три")), Path("a.wav"), label="f", audio_seconds=1.0)
    result = compare(run, tokens_from_text("один два три"))
    assert result.words.rate == 0.0
    # Еталон без таймкодів — межі слів рахувати нема з чим.
    assert result.boundaries is None


def test_compare_skips_boundaries_for_phrase_level_backends() -> None:
    # MOSS віддає лише репліки: його «таймкоди слів» були б початком репліки,
    # і метрика меж перетворилась би на вигадку.
    run = measure(
        FakeBackend(build("один два три", with_words=False)),
        Path("a.wav"),
        label="moss",
        audio_seconds=1.0,
    )
    result = compare(run, tokens_of(build("один два три")))
    assert result.words is not None
    assert result.boundaries is None


def test_compare_measures_boundaries_when_both_sides_have_words() -> None:
    run = measure(FakeBackend(build("один два три")), Path("a.wav"), label="f", audio_seconds=1.0)
    result = compare(run, tokens_of(build("один два три")))
    assert result.boundaries is not None
    assert result.boundaries.median == 0.0


def test_table_marks_cold_runs() -> None:
    cold = measure(
        FakeBackend(build("один"), warmable=False), Path("a.wav"), label="cold", audio_seconds=1.0
    )
    rendered = table([compare(cold, None)])
    assert "~" in rendered
    assert "не вміє прогрітись" in rendered


def test_table_stays_clean_for_warm_runs() -> None:
    warm = measure(FakeBackend(build("один")), Path("a.wav"), label="warm", audio_seconds=1.0)
    assert "не вміє прогрітись" not in table([compare(warm, None)])
