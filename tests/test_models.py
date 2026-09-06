from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoworks.models import BackendInfo, Edl, EdlSegment, Segment, Transcript, Word


def make_transcript() -> Transcript:
    return Transcript(
        source="media/raw.mp4",
        duration=30.0,
        language="uk",
        backend=BackendInfo(name="faster-whisper", model="large-v3"),
        segments=[
            Segment(
                id=0,
                start=1.0,
                end=3.0,
                text="Привіт світ",
                words=[
                    Word(w="Привіт", start=1.0, end=1.8, prob=0.99),
                    Word(w="світ", start=1.9, end=3.0, prob=0.97),
                ],
            )
        ],
    )


def test_word_rejects_inverted_span() -> None:
    with pytest.raises(ValidationError):
        Word(w="зламане", start=5.0, end=4.0)


def test_transcript_roundtrip(tmp_path) -> None:
    original = make_transcript()
    path = original.save(tmp_path / "transcript.json")
    assert Transcript.load(path) == original


def test_transcript_collects_words() -> None:
    assert [w.w for w in make_transcript().words()] == ["Привіт", "світ"]


def test_edl_rejects_empty_segment() -> None:
    with pytest.raises(ValidationError):
        EdlSegment(src_in=10.0, src_out=10.0, dst_out=0.0)


def test_edl_duration_sums_segments() -> None:
    edl = Edl(
        source="media/raw.mp4",
        segments=[
            EdlSegment(src_in=0.0, src_out=5.0, dst_out=0.0),
            EdlSegment(src_in=10.0, src_out=12.5, dst_out=5.0),
        ],
    )
    assert edl.duration == pytest.approx(7.5)
