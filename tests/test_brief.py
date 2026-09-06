from __future__ import annotations

import pytest

from videoworks.brief import build, removed_spans, review
from videoworks.edl import from_spans
from videoworks.models import BackendInfo, Scene, Scenes, Segment, Transcript, Word


def transcript_of(phrases: list[tuple[str, float, float]]) -> Transcript:
    segments = []
    for index, (text, start, end) in enumerate(phrases):
        tokens = text.split()
        step = (end - start) / max(len(tokens), 1)
        words = [
            Word(w=token, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
            for i, token in enumerate(tokens)
        ]
        segments.append(Segment(id=index, start=start, end=end, text=text, words=words))
    return Transcript(
        source="media/raw.mp4",
        duration=60.0,
        language="uk",
        backend=BackendInfo(name="faster-whisper", model="large-v3"),
        segments=segments,
    )


SAMPLE = [
    ("Перша фраза розмови", 1.0, 4.0),
    ("Друга фраза розмови", 20.0, 24.0),
    ("Третя фраза розмови", 40.0, 44.0),
]


# --------------------------------------------------------------------------- #
# Зведення
# --------------------------------------------------------------------------- #


def test_brief_lists_phrases_not_words() -> None:
    text = build("demo", transcript_of(SAMPLE))
    assert "Перша фраза розмови" in text
    # Рівень слова у зведення не потрапляє — саме на цьому тримається компактність.
    assert "Перша]" not in text
    assert text.count("[") == len(SAMPLE)


def test_brief_is_far_smaller_than_word_level_json() -> None:
    transcript = transcript_of(SAMPLE)
    text = build("demo", transcript)
    assert len(text) < len(transcript.model_dump_json()) / 3


def test_brief_includes_scenes_when_available() -> None:
    scenes = Scenes(
        source="raw.mp4",
        detector="content:27.0",
        scenes=[Scene(id=0, start=0.0, end=10.0), Scene(id=1, start=10.0, end=25.0)],
    )
    text = build("demo", transcript_of(SAMPLE), scenes=scenes)
    assert "## Сцени (2)" in text


def test_brief_survives_without_probe_or_scenes() -> None:
    text = build("demo", transcript_of(SAMPLE))
    assert "## Транскрипт" in text


def test_brief_reports_its_own_size() -> None:
    text = build("demo", transcript_of(SAMPLE))
    assert "символів." in text


def test_brief_declines_ukrainian_numerals() -> None:
    text = build("demo", transcript_of(SAMPLE))
    assert "3 фрази" in text  # не «3 фраз»


# --------------------------------------------------------------------------- #
# Що піде під ніж
# --------------------------------------------------------------------------- #


def test_removed_spans_are_the_complement() -> None:
    plan = from_spans("raw.mp4", [(5.0, 10.0), (20.0, 30.0)])
    assert removed_spans(plan, 40.0) == [(0.0, 5.0), (10.0, 20.0), (30.0, 40.0)]


def test_removed_spans_empty_when_nothing_cut() -> None:
    plan = from_spans("raw.mp4", [(0.0, 40.0)])
    assert removed_spans(plan, 40.0) == []


def test_removed_spans_ignore_hairline_gaps() -> None:
    # Проміжок 0.0005 с — нижче допуску, це шум арифметики, а не правка.
    plan = from_spans("raw.mp4", [(0.0, 10.0), (10.0005, 20.0)])
    assert removed_spans(plan, 20.0) == []


def test_review_names_the_words_that_disappear() -> None:
    plan = from_spans("raw.mp4", [(0.0, 10.0), (38.0, 50.0)])
    text = review(plan, transcript_of(SAMPLE), 60.0)
    lines = [line for line in text.splitlines() if line.startswith("[cut ]")]
    assert any("Друга фраза розмови" in line for line in lines)
    assert not any("Перша фраза розмови" in line for line in lines)


def test_review_marks_silent_gaps() -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0), (19.0, 60.0)])
    text = review(plan, transcript_of(SAMPLE), 60.0)
    assert "без мовлення" in text


def test_review_reports_totals() -> None:
    plan = from_spans("raw.mp4", [(0.0, 30.0)])
    text = review(plan, transcript_of(SAMPLE), 60.0)
    assert "вирізано 30.0 с (50%)" in text


def test_review_rows_are_in_source_order() -> None:
    plan = from_spans("raw.mp4", [(10.0, 20.0), (35.0, 45.0)])
    text = review(plan, transcript_of(SAMPLE), 60.0)
    rows = [line for line in text.splitlines() if line.startswith("[")]
    starts = [row.split("]")[1].strip().split("–")[0] for row in rows]
    assert starts == sorted(starts)


@pytest.mark.parametrize("duration", [60.0, 44.0])
def test_review_handles_plan_reaching_the_end(duration: float) -> None:
    plan = from_spans("raw.mp4", [(0.0, duration)])
    assert "вирізано 0.0 с" in review(plan, transcript_of(SAMPLE), duration)
