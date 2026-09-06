from __future__ import annotations

import pytest

from videoworks.diagnostics import errors, warnings
from videoworks.subtitles import Cue, CueOptions, load_cues, save
from videoworks.translate import (
    Glossary,
    GlossaryEntry,
    apply_draft,
    budget,
    check,
    parse_draft,
    worksheet,
)


def cue(text: str, start: float = 0.0, end: float = 4.0) -> Cue:
    return Cue(start=start, end=end, lines=[text])


SAMPLE = [
    cue("Привіт, сьогодні розбираємо монтаж.", 0.0, 4.0),
    cue("VideoWorks читає транскрипт.", 4.5, 8.0),
]


# --------------------------------------------------------------------------- #
# Глосарій
# --------------------------------------------------------------------------- #


def test_glossary_reads_tsv(tmp_path) -> None:
    path = tmp_path / "glossary.tsv"
    path.write_text(
        "# коментар\n\nVideoWorks\t\nтранскрипт\ttranscript\n",
        encoding="utf-8",
    )
    glossary = Glossary.load(path)
    assert glossary.entries == (
        GlossaryEntry("VideoWorks", ""),
        GlossaryEntry("транскрипт", "transcript"),
    )


def test_glossary_missing_file_is_empty(tmp_path) -> None:
    assert Glossary.load(tmp_path / "nope.tsv").entries == ()


def test_glossary_survives_a_bom(tmp_path) -> None:
    # Блокнот і PowerShell лишають BOM — без utf-8-sig перший термін
    # приходив із невидимим символом і ніколи не збігався.
    path = tmp_path / "glossary.tsv"
    path.write_bytes("﻿VideoWorks\t\n".encode())
    assert Glossary.load(path).entries == (GlossaryEntry("VideoWorks", ""),)


def test_entry_without_target_expects_itself() -> None:
    assert GlossaryEntry("VideoWorks", "").expected == "VideoWorks"


def test_glossary_flags_dropped_term() -> None:
    glossary = Glossary((GlossaryEntry("транскрипт", "transcript"),))
    assert glossary.violations("читає транскрипт", "reads the recording")


def test_glossary_accepts_correct_term() -> None:
    glossary = Glossary((GlossaryEntry("транскрипт", "transcript"),))
    assert glossary.violations("читає транскрипт", "reads the transcript") == []


def test_glossary_ignores_absent_term() -> None:
    glossary = Glossary((GlossaryEntry("транскрипт", "transcript"),))
    assert glossary.violations("зовсім інша фраза", "a different phrase") == []


def test_untranslatable_term_must_survive_verbatim() -> None:
    glossary = Glossary((GlossaryEntry("VideoWorks", ""),))
    assert glossary.violations("VideoWorks читає", "ВідеоВоркс reads")
    assert glossary.violations("VideoWorks читає", "VideoWorks reads") == []


# --------------------------------------------------------------------------- #
# Бюджет
# --------------------------------------------------------------------------- #


def test_budget_is_capped_by_reading_speed_on_short_cues() -> None:
    # 1 с × 17 симв/с = 17 символів, хоча в два рядки влізло б 84.
    assert budget(cue("текст", 0.0, 1.0), CueOptions()) == 17


def test_budget_is_capped_by_line_capacity_on_long_cues() -> None:
    # 30 с дали б 510 символів, але фізично влазить лише 84.
    assert budget(cue("текст", 0.0, 30.0), CueOptions()) == 84


def test_budget_never_drops_to_zero() -> None:
    assert budget(cue("текст", 0.0, 0.01), CueOptions()) >= 1


# --------------------------------------------------------------------------- #
# Аркуш
# --------------------------------------------------------------------------- #


def test_worksheet_numbers_every_cue() -> None:
    text = worksheet(SAMPLE, source_lang="uk", target_lang="en")
    assert "[1]" in text
    assert "[2]" in text
    assert "Привіт, сьогодні розбираємо монтаж." in text


def test_worksheet_states_budget_per_cue() -> None:
    text = worksheet(SAMPLE, source_lang="uk", target_lang="en")
    assert f"≤{budget(SAMPLE[0], CueOptions())} симв" in text


def test_worksheet_demands_whole_document_translation() -> None:
    text = worksheet(SAMPLE, source_lang="uk", target_lang="en")
    assert "увесь документ разом" in text


def test_worksheet_warns_about_language_growth() -> None:
    assert "коротша" in worksheet(SAMPLE, source_lang="uk", target_lang="en")
    assert "довша" in worksheet(SAMPLE, source_lang="en", target_lang="uk")


def test_worksheet_embeds_glossary() -> None:
    glossary = Glossary((GlossaryEntry("VideoWorks", ""),))
    text = worksheet(SAMPLE, source_lang="uk", target_lang="en", glossary=glossary)
    assert "## Глосарій" in text
    assert "лишити як є" in text


def test_worksheet_omits_glossary_section_when_empty() -> None:
    assert "## Глосарій" not in worksheet(SAMPLE, source_lang="uk", target_lang="en")


# --------------------------------------------------------------------------- #
# Розбір чернетки
# --------------------------------------------------------------------------- #


def test_parse_draft_reads_numbered_blocks() -> None:
    draft = "## Репліки\n\n[1] ≤56 симв · 4.0 с\nHello, today we edit.\n\n[2]\nVideoWorks reads it.\n"
    assert parse_draft(draft) == {1: "Hello, today we edit.", 2: "VideoWorks reads it."}


def test_parse_draft_joins_wrapped_lines() -> None:
    # Перекладач міг перенести рядки — розкладку однаково робимо заново.
    draft = "[1]\nHello, today\nwe edit video.\n"
    assert parse_draft(draft) == {1: "Hello, today we edit video."}


def test_parse_draft_skips_empty_blocks() -> None:
    assert parse_draft("[1]\n\n[2]\nDone.\n") == {2: "Done."}


def test_parse_draft_keeps_numbering_not_order() -> None:
    assert parse_draft("[7]\nSeven.\n[3]\nThree.\n") == {7: "Seven.", 3: "Three."}


# --------------------------------------------------------------------------- #
# Підстановка
# --------------------------------------------------------------------------- #


def test_apply_keeps_timing_untouched() -> None:
    result = apply_draft(SAMPLE, {1: "Hello there.", 2: "It reads."})
    assert [(c.start, c.end) for c in result] == [(0.0, 4.0), (4.5, 8.0)]


def test_apply_rewraps_translated_text() -> None:
    long_text = "This translated line is definitely longer than a single subtitle row allows"
    result = apply_draft(SAMPLE, {1: long_text}, CueOptions(max_line_chars=40))
    assert len(result[0].lines) == 2
    assert " ".join(result[0].lines) == long_text


def test_apply_keeps_original_where_translation_missing() -> None:
    result = apply_draft(SAMPLE, {1: "Hello there."})
    assert result[1].text == SAMPLE[1].text


# --------------------------------------------------------------------------- #
# Перевірка придатності
# --------------------------------------------------------------------------- #


def test_check_passes_reasonable_subtitles() -> None:
    assert check([cue("Коротка репліка тут.", 0.0, 4.0)]) == []


def test_check_warns_when_reading_speed_exceeded() -> None:
    fast = cue("Дуже довгий текст, який ніяк не встигнути прочитати за мить", 0.0, 1.0)
    problems = check([fast])
    assert warnings(problems)
    assert "не встигне" in warnings(problems)[0].message


def test_check_errors_when_text_cannot_be_wrapped() -> None:
    huge = cue("Слово " + "х" * 60, 0.0, 30.0)
    assert errors(check([huge], CueOptions(max_line_chars=20)))


def test_check_errors_on_empty_cue() -> None:
    assert errors(check([Cue(start=0.0, end=2.0, lines=[""])]))


def test_check_errors_when_cue_count_changed() -> None:
    problems = check([cue("Один.", 0.0, 4.0)], source=SAMPLE)
    assert any("таймкоди прив'язані" in p.message for p in errors(problems))


def test_check_reports_glossary_drift_as_warning() -> None:
    glossary = Glossary((GlossaryEntry("VideoWorks", ""),))
    translated = [cue("Hello there.", 0.0, 4.0), cue("The tool reads it.", 4.5, 8.0)]
    problems = check(translated, source=SAMPLE, glossary=glossary)
    assert warnings(problems)
    assert not errors(problems)


# --------------------------------------------------------------------------- #
# Круговий обмін через файл
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", ["srt", "ass", "vtt"])
def test_cues_survive_a_save_load_round_trip(tmp_path, suffix: str) -> None:
    path = save(SAMPLE, tmp_path / f"uk.{suffix}")
    restored = load_cues(path)
    assert [c.text for c in restored] == [c.text for c in SAMPLE]
    for original, back in zip(SAMPLE, restored, strict=True):
        assert back.start == pytest.approx(original.start, abs=0.01)
        assert back.end == pytest.approx(original.end, abs=0.01)
