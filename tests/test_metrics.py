from __future__ import annotations

import pytest

from videoworks.diarize import assign_speakers, speaker_at
from videoworks.metrics import (
    Token,
    align,
    boundary_error,
    normalize,
    speaker_agreement,
    tokens_from_text,
    tokens_of,
    word_error,
)
from videoworks.models import BackendInfo, Segment, Transcript, Word


def transcript_of(
    phrases: list[tuple[str, float, float, str | None]],
    *,
    with_words: bool = True,
) -> Transcript:
    segments = []
    for index, (text, start, end, speaker) in enumerate(phrases):
        pieces = text.split()
        step = (end - start) / max(len(pieces), 1)
        words = (
            [
                Word(w=piece, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
                for i, piece in enumerate(pieces)
            ]
            if with_words
            else []
        )
        segments.append(
            Segment(id=index, start=start, end=end, text=text, speaker=speaker, words=words)
        )
    return Transcript(
        source="raw.mp4",
        duration=phrases[-1][2],
        language="uk",
        backend=BackendInfo(name="test", model="test"),
        segments=segments,
    )


def timed(pairs: list[tuple[str, float]], speaker: str | None = None) -> list[Token]:
    return [Token(text=text, start=start, end=start + 0.3, speaker=speaker) for text, start in pairs]


# --------------------------------------------------------------------------- #
# Нормалізація
# --------------------------------------------------------------------------- #


def test_normalize_ignores_case_and_punctuation() -> None:
    assert normalize("Привіт,") == normalize("привіт")


@pytest.mark.parametrize("apostrophe", ["'", "ʼ", "’", "`", "´"])
def test_normalize_unifies_apostrophes(apostrophe: str) -> None:
    # Українську апострофну літеру пишуть щонайменше пʼятьма символами;
    # різниця в них не має рахуватись за помилку розпізнавання.
    assert normalize(f"пам{apostrophe}ять") == normalize("пам'ять")


def test_normalize_drops_bare_punctuation() -> None:
    assert normalize("—") == ""


# --------------------------------------------------------------------------- #
# Вирівнювання і WER
# --------------------------------------------------------------------------- #


def test_identical_sequences_have_zero_error() -> None:
    tokens = tokens_from_text("один два три")
    result = word_error(tokens, tokens)
    assert result.rate == 0.0
    assert result.hits == 3


def test_substitution_is_counted() -> None:
    result = word_error(tokens_from_text("один два три"), tokens_from_text("один ДВА? три"))
    assert result.substitutions == 0  # нормалізація прибирає регістр і знак


def test_real_substitution_is_counted() -> None:
    result = word_error(tokens_from_text("один два три"), tokens_from_text("один п'ять три"))
    assert (result.substitutions, result.deletions, result.insertions) == (1, 0, 0)
    assert result.rate == pytest.approx(1 / 3)


def test_deletion_and_insertion_are_counted() -> None:
    dropped = word_error(tokens_from_text("один два три"), tokens_from_text("один три"))
    assert (dropped.deletions, dropped.insertions) == (1, 0)

    added = word_error(tokens_from_text("один три"), tokens_from_text("один два три"))
    assert (added.deletions, added.insertions) == (0, 1)


def test_wer_can_exceed_one() -> None:
    result = word_error(tokens_from_text("один"), tokens_from_text("зовсім інші п'ять слів тут"))
    assert result.rate > 1.0


def test_empty_reference_gives_zero_rate() -> None:
    assert word_error([], tokens_from_text("щось")).rate == 0.0


def test_align_covers_every_position() -> None:
    operations = align(["a", "b", "c"], ["a", "x", "c", "d"])
    assert sum(1 for op in operations if op.reference is not None) == 3
    assert sum(1 for op in operations if op.hypothesis is not None) == 4


# --------------------------------------------------------------------------- #
# Межі слів
# --------------------------------------------------------------------------- #


def test_boundary_error_is_zero_on_identical_timings() -> None:
    tokens = timed([("один", 1.0), ("два", 2.0)])
    result = boundary_error(tokens, tokens)
    assert result.median == 0.0
    assert result.within_100ms == 1.0


def test_boundary_error_measures_the_shift() -> None:
    reference = timed([("один", 1.0), ("два", 2.0)])
    hypothesis = timed([("один", 1.2), ("два", 2.2)])
    result = boundary_error(reference, hypothesis)
    assert result.median == pytest.approx(0.2)
    assert result.within_100ms == 0.0


def test_boundary_error_ignores_misrecognised_words() -> None:
    # Слово, якого немає в еталоні, не має псувати статистику таймкодів.
    reference = timed([("один", 1.0), ("два", 2.0)])
    hypothesis = timed([("один", 1.0), ("вигадка", 5.0), ("два", 2.0)])
    result = boundary_error(reference, hypothesis)
    assert result.matched == 2
    assert result.median == 0.0


def test_boundary_error_is_none_without_matches() -> None:
    assert boundary_error(timed([("один", 1.0)]), timed([("зовсім", 9.0)])) is None


# --------------------------------------------------------------------------- #
# Спікери
# --------------------------------------------------------------------------- #


def test_speaker_agreement_survives_relabelling() -> None:
    # Мітки довільні: A↔B у гіпотезі — це та сама розмітка, не помилка.
    reference = timed([("один", 1.0), ("два", 2.0)], "A") + timed([("три", 3.0)], "B")
    hypothesis = timed([("один", 1.0), ("два", 2.0)], "S2") + timed([("три", 3.0)], "S1")
    assert speaker_agreement(reference, hypothesis) == 1.0


def test_speaker_agreement_catches_real_confusion() -> None:
    reference = timed([("один", 1.0)], "A") + timed([("два", 2.0), ("три", 3.0)], "B")
    hypothesis = timed([("один", 1.0), ("два", 2.0), ("три", 3.0)], "S1")
    assert speaker_agreement(reference, hypothesis) == pytest.approx(2 / 3)


def test_speaker_agreement_is_none_without_labels() -> None:
    assert speaker_agreement(timed([("один", 1.0)]), timed([("один", 1.0)])) is None


# --------------------------------------------------------------------------- #
# Токенізація транскриптів без рівня слова
# --------------------------------------------------------------------------- #


def test_tokens_fall_back_to_phrase_text() -> None:
    # MOSS віддає лише репліки — WER має лишатись порівнянним.
    transcript = transcript_of([("один два три", 0.0, 3.0, "S1")], with_words=False)
    assert [t.text for t in tokens_of(transcript)] == ["один", "два", "три"]
    assert not transcript.has_word_timings


def test_word_level_transcript_reports_timings() -> None:
    assert transcript_of([("один два", 0.0, 2.0, None)]).has_word_timings


# --------------------------------------------------------------------------- #
# Перенесення спікерів
# --------------------------------------------------------------------------- #


def test_speaker_at_picks_the_largest_overlap() -> None:
    diarization = transcript_of(
        [("перший каже", 0.0, 5.0, "S1"), ("другий каже", 5.0, 10.0, "S2")],
        with_words=False,
    )
    assert speaker_at(1.0, 2.0, diarization) == "S1"
    assert speaker_at(4.8, 6.0, diarization) == "S2"
    assert speaker_at(20.0, 21.0, diarization) is None


def test_assign_speakers_splits_segment_on_speaker_change() -> None:
    words = transcript_of([("один два три чотири", 0.0, 8.0, None)])
    diarization = transcript_of(
        [("перший", 0.0, 4.0, "S1"), ("другий", 4.0, 8.0, "S2")], with_words=False
    )
    result = assign_speakers(words, diarization)

    assert [s.speaker for s in result.segments] == ["S1", "S2"]
    assert [s.text for s in result.segments] == ["один два", "три чотири"]
    assert [s.id for s in result.speakers] == ["S1", "S2"]


def test_assign_speakers_keeps_every_word() -> None:
    words = transcript_of([("один два три чотири", 0.0, 8.0, None)])
    diarization = transcript_of([("усе", 0.0, 8.0, "S1")], with_words=False)
    result = assign_speakers(words, diarization)
    assert len(result.words()) == 4
    assert len(result.segments) == 1


def test_assign_speakers_tolerates_gaps_in_diarization() -> None:
    words = transcript_of([("один два три чотири", 0.0, 8.0, None)])
    diarization = transcript_of([("шматок", 0.0, 2.0, "S1")], with_words=False)
    result = assign_speakers(words, diarization)
    assert result.segments[0].speaker == "S1"
    assert result.segments[-1].speaker is None
    assert len(result.words()) == 4
