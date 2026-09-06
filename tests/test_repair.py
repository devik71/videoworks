from __future__ import annotations

import pytest

from videoworks.asr.repair import spread_degenerate
from videoworks.models import BackendInfo, Segment, Transcript, Word


def transcript_of(spans: list[tuple[str, float, float]]) -> Transcript:
    words = [Word(w=w, start=s, end=e) for w, s, e in spans]
    return Transcript(
        source="raw.mp4",
        duration=100.0,
        language="uk",
        backend=BackendInfo(name="test", model="test"),
        segments=[
            Segment(
                id=0,
                start=words[0].start,
                end=words[-1].end,
                text=" ".join(w.w for w in words),
                words=words,
            )
        ],
    )


def test_healthy_transcript_is_untouched() -> None:
    original = transcript_of([("один", 1.0, 1.5), ("два", 1.6, 2.0)])
    repaired, count = spread_degenerate(original)
    assert count == 0
    assert repaired is original


def test_single_degenerate_word_gets_the_gap() -> None:
    repaired, count = spread_degenerate(
        transcript_of([("один", 1.0, 1.5), ("два", 1.8, 1.8), ("три", 2.4, 3.0)])
    )
    assert count == 1
    middle = repaired.words()[1]
    assert middle.start == pytest.approx(1.5)
    assert middle.end == pytest.approx(2.4)


def test_run_of_degenerate_words_is_spread_evenly() -> None:
    # Саме цей випадок ламав субтитри: п'ять слів з однаковим часом підряд.
    repaired, count = spread_degenerate(
        transcript_of([
            ("нуль", 0.0, 1.0),
            ("a", 2.0, 2.0),
            ("b", 2.0, 2.0),
            ("c", 2.0, 2.0),
            ("край", 4.0, 5.0),
        ])
    )
    assert count == 3
    spans = [(w.start, w.end) for w in repaired.words()[1:4]]
    assert spans == [
        pytest.approx((1.0, 2.0)),
        pytest.approx((2.0, 3.0)),
        pytest.approx((3.0, 4.0)),
    ]


def test_repaired_words_are_strictly_positive() -> None:
    repaired, _ = spread_degenerate(
        transcript_of([("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 1.0, 1.0)])
    )
    assert all(w.end > w.start for w in repaired.words())


def test_degenerate_run_at_the_very_end_still_gets_duration() -> None:
    repaired, count = spread_degenerate(
        transcript_of([("один", 1.0, 2.0), ("хвіст", 2.0, 2.0)])
    )
    assert count == 1
    assert repaired.words()[-1].end > repaired.words()[-1].start


def test_degenerate_run_at_the_very_start_is_handled() -> None:
    repaired, count = spread_degenerate(
        transcript_of([("голова", 1.0, 1.0), ("далі", 3.0, 4.0)])
    )
    assert count == 1
    assert repaired.words()[0].end > repaired.words()[0].start


def test_word_order_and_text_survive() -> None:
    original = transcript_of([("а", 0.0, 1.0), ("б", 2.0, 2.0), ("в", 3.0, 4.0)])
    repaired, _ = spread_degenerate(original)
    assert [w.w for w in repaired.words()] == ["а", "б", "в"]
    assert len(repaired.segments) == len(original.segments)
