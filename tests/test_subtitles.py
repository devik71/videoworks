from __future__ import annotations

from itertools import pairwise

from videoworks.models import BackendInfo, Segment, Transcript, Word
from videoworks.subtitles import (
    CueOptions,
    build_cues,
    build_ssa,
    wrap_lines,
)
from videoworks.text import join_words


def transcript_from(pairs: list[tuple[str, float, float]]) -> Transcript:
    words = [Word(w=w, start=s, end=e) for w, s, e in pairs]
    return Transcript(
        source="media/raw.mp4",
        duration=(words[-1].end if words else 0.0) + 1.0,
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


def evenly(text: str, *, start: float = 0.0, step: float = 0.3) -> Transcript:
    pairs = []
    clock = start
    for token in text.split():
        pairs.append((token, round(clock, 3), round(clock + step * 0.9, 3)))
        clock += step
    return transcript_from(pairs)


# --------------------------------------------------------------------------- #
# Розкладка на рядки
# --------------------------------------------------------------------------- #


def test_short_text_stays_one_line() -> None:
    assert wrap_lines("Коротка репліка") == ["Коротка репліка"]


def test_lines_respect_char_limit() -> None:
    text = "Агент читає транскрипт замість того щоб дивитися саме відео покадрово"
    opts = CueOptions(max_line_chars=40)
    lines = wrap_lines(text, opts)
    assert len(lines) == 2
    assert all(len(line) <= 40 for line in lines)
    assert " ".join(lines) == text


def test_lines_are_balanced() -> None:
    text = "Агент читає транскрипт замість того щоб дивитися саме відео покадрово"
    lines = wrap_lines(text, CueOptions(max_line_chars=40))
    # Жадібна розкладка дала б 39 і 29; DP тримає рядки приблизно рівними.
    assert abs(len(lines[0]) - len(lines[1])) <= 6


def test_greedy_fallback_when_text_exceeds_all_lines() -> None:
    # 68 символів не влазять у два рядки по 30 — свідомо переповнюємо, але
    # не втрачаємо жодного слова.
    text = "Агент читає транскрипт замість того щоб дивитися саме відео покадрово"
    lines = wrap_lines(text, CueOptions(max_line_chars=30))
    assert " ".join(lines) == text


def test_line_does_not_end_with_preposition() -> None:
    # Наївний баланс розрізав би саме після «в».
    text = "Ми поклали субтитри в окремий трек"
    lines = wrap_lines(text, CueOptions(max_line_chars=20))
    assert not lines[0].lower().endswith(" в")
    assert " ".join(lines) == text


def test_wrapping_prefers_punctuation_seam() -> None:
    text = "Спочатку транскрипт, потім монтаж"
    lines = wrap_lines(text, CueOptions(max_line_chars=20))
    assert lines[0].endswith(",")


def test_overlong_word_still_returns_all_text() -> None:
    text = "Слово " + "х" * 60
    lines = wrap_lines(text, CueOptions(max_line_chars=20))
    assert " ".join(lines) == text


# --------------------------------------------------------------------------- #
# Складання реплік
# --------------------------------------------------------------------------- #


def test_cue_breaks_on_long_pause() -> None:
    transcript = transcript_from([
        ("Перша", 0.0, 0.5),
        ("частина", 0.6, 1.2),
        ("розмови", 1.3, 1.9),
        # пауза 2 с
        ("Друга", 3.9, 4.4),
        ("частина", 4.5, 5.1),
        ("розмови", 5.2, 5.8),
    ])
    cues = build_cues(transcript)
    assert len(cues) == 2
    assert cues[0].text.startswith("Перша")
    assert cues[1].text.startswith("Друга")


def test_cues_never_overlap() -> None:
    cues = build_cues(evenly(" ".join(f"слово{i}" for i in range(60))))
    assert len(cues) > 1
    for earlier, later in pairwise(cues):
        assert earlier.end <= later.start


def test_short_cue_is_stretched_to_minimum() -> None:
    transcript = transcript_from([("Так", 0.0, 0.2), ("справді", 0.25, 0.5)])
    cue = build_cues(transcript)[0]
    # Допуск у мілісекунду: таймкоди округлюються, а SRT усе одно не має
    # точності вище мілісекунди.
    assert cue.duration >= 5 / 6 - 0.001


def test_reading_speed_is_capped_when_room_allows() -> None:
    words = "Це доволі довга репліка яку неможливо прочитати за таку коротку мить".split()  # noqa: SIM905
    pairs = [(w, round(i * 0.12, 3), round(i * 0.12 + 0.1, 3)) for i, w in enumerate(words)]
    transcript = transcript_from(pairs)
    cue = build_cues(transcript)[0]
    assert cue.cps <= 17.0 + 1e-6


def test_cue_respects_max_duration() -> None:
    opts = CueOptions(max_duration=3.0, pause_break=99.0)
    cues = build_cues(evenly(" ".join(f"слово{i}" for i in range(40)), step=0.4), opts)
    assert all(cue.duration <= 3.0 + 1e-6 for cue in cues)


def test_cue_never_exceeds_max_lines() -> None:
    # 63 символи влазять у бюджет 2×32, але валідного шва між словами немає:
    # межа репліки має враховувати саме здійсненність розкладки.
    transcript = evenly("This is a smoke test for the VideoWorks transcription pipeline.")
    cues = build_cues(transcript, CueOptions(max_line_chars=32))
    assert len(cues) > 1
    assert all(len(cue.lines) <= 2 for cue in cues)
    assert all(len(line) <= 32 for cue in cues for line in cue.lines)


def test_orphan_word_is_avoided_by_moving_the_seam() -> None:
    # Наївна розбивка лишила б окрему репліку «pipeline.» на одне слово.
    transcript = evenly("This is a smoke test for the VideoWorks transcription pipeline.")
    cues = build_cues(transcript, CueOptions(max_line_chars=32))
    assert all(len(cue.text) >= 16 for cue in cues)


def test_no_text_is_lost() -> None:
    transcript = evenly("перше друге третє четверте пʼяте шосте сьоме восьме девʼяте десяте")
    joined = " ".join(cue.text for cue in build_cues(transcript))
    assert joined.split() == [w.w for w in transcript.words()]


def test_empty_transcript_gives_no_cues() -> None:
    transcript = Transcript(
        source="x.mp4",
        duration=1.0,
        language="uk",
        backend=BackendInfo(name="test", model="test"),
        segments=[],
    )
    assert build_cues(transcript) == []


# --------------------------------------------------------------------------- #
# Склеювання токенів і експорт
# --------------------------------------------------------------------------- #


def test_join_words_handles_hyphen_and_punctuation() -> None:
    words = [
        Word(w="Word", start=0.0, end=0.2),
        Word(w="-level", start=0.2, end=0.4),
        Word(w="таймкоди", start=0.4, end=0.8),
        Word(w=",", start=0.8, end=0.9),
        Word(w="так", start=0.9, end=1.1),
    ]
    assert join_words(words) == "Word-level таймкоди, так"


def test_srt_roundtrip(tmp_path) -> None:
    from videoworks.subtitles import save

    cues = build_cues(evenly("перше друге третє четверте пʼяте шосте"))
    path = save(cues, tmp_path / "uk.srt")
    body = path.read_text(encoding="utf-8")
    assert "1\n" in body
    assert "-->" in body


def test_ass_uses_frame_relative_size() -> None:
    cues = build_cues(evenly("перше друге третє"))
    subs = build_ssa(cues, resolution=(1920, 1080))
    assert subs.info["PlayResY"] == "1080"
    assert subs.styles["Default"].fontsize == 54
