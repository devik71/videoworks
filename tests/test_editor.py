from __future__ import annotations

from videoworks.editor import analyze_cues, format_report, trim_cues
from videoworks.subtitles import Cue, CueOptions


def cue(text: str, start: float = 0.0, end: float = 3.0) -> Cue:
    return Cue(start=start, end=end, lines=[text])


# --------------------------------------------------------------------------- #
# Аналіз
# --------------------------------------------------------------------------- #


def test_analyze_counts_characters_and_speed() -> None:
    report = analyze_cues([cue("Це тестовий текст для перевірки", 0.0, 1.0)])[0]
    assert report.index == 1
    assert report.char_count == len("Це тестовий текст для перевірки")
    assert report.line_count == 1
    assert report.cps > 0
    assert report.fits


def test_analyze_detects_empty_cue() -> None:
    report = analyze_cues([cue("")])[0]
    assert any("порожня" in p.message for p in report.problems)
    assert all(p.level == "error" for p in report.problems)


def test_analyze_flags_fast_reading() -> None:
    report = analyze_cues([cue("Довга репліка, яку ніхто не встигне прочитати", 0.0, 1.0)])[0]
    assert any(p.level == "warning" and "симв/с" in p.message for p in report.problems)


def test_analyze_flags_zero_duration() -> None:
    report = analyze_cues([cue("Текст", 5.0, 5.0)])[0]
    assert any("нульова тривалість" in p.message for p in report.problems)
    # Нескінченний CPS не має додавати другого попередження про швидкість.
    assert not any("симв/с" in p.message for p in report.problems)


def test_analyze_flags_text_that_does_not_fit() -> None:
    long_text = " ".join(["слово"] * 30)
    report = analyze_cues([cue(long_text, 0.0, 30.0)])[0]
    assert not report.fits
    assert any("не розкладаються" in p.message for p in report.problems)


def test_analyze_accepts_normal_cue() -> None:
    assert analyze_cues([cue("Звичайна репліка на екрані.", 0.0, 3.0)])[0].problems == []


def test_analyze_respects_options() -> None:
    strict = CueOptions(max_cps=1.0)
    assert analyze_cues([cue("Нормальна репліка", 0.0, 3.0)], strict)[0].problems


# --------------------------------------------------------------------------- #
# Скорочення
# --------------------------------------------------------------------------- #


def test_trim_shortens_long_cue() -> None:
    long_text = "Це дуже довгий текст який потрібно скоротити бо він занадто довгий"
    trimmed = trim_cues([cue(long_text)], 42)
    assert len(trimmed[0].text) <= 42
    assert long_text.startswith(trimmed[0].text)


def test_trim_keeps_whole_words() -> None:
    trimmed = trim_cues([cue("абвгд ежзик лмноп")], 8)
    assert trimmed[0].text == "абвгд"


def test_trim_preserves_short_cues() -> None:
    short = ["Короткий", "Текст"]
    trimmed = trim_cues([cue(text) for text in short], 42)
    assert [c.text for c in trimmed] == short


def test_trim_keeps_timing_and_speaker() -> None:
    original = Cue(start=1.5, end=4.25, lines=["Дуже довга репліка про монтаж"], speaker="A")
    trimmed = trim_cues([original], 10)[0]
    assert (trimmed.start, trimmed.end, trimmed.speaker) == (1.5, 4.25, "A")


def test_trim_leaves_one_word_when_nothing_fits() -> None:
    trimmed = trim_cues([cue("нескорочуванослово далі")], 5)
    assert trimmed[0].text == "нескорочуванослово"


def test_trim_handles_empty_cue() -> None:
    assert trim_cues([cue("")], 42)[0].text == ""


# --------------------------------------------------------------------------- #
# Звіт
# --------------------------------------------------------------------------- #


def test_format_report_contains_stats() -> None:
    output = format_report(analyze_cues([cue("Тест"), cue("")]))
    assert "Реплік: 2" in output
    assert "помилок: 1" in output
    assert "попереджень: 0" in output


def test_format_report_shows_text_and_problems() -> None:
    output = format_report(analyze_cues([cue("Привіт, світе.", 0.0, 3.0)]))
    assert "Привіт, світе." in output
    assert output.splitlines()[2].startswith("·")


def test_format_report_marks_severity() -> None:
    fast = cue("Репліка, яку ніхто не встигне прочитати за секунду", 0.0, 1.0)
    output = format_report(analyze_cues([cue(""), fast, cue("Норм.")]))
    marks = [line[0] for line in output.splitlines() if line[:1] and line[0] in "x!·"]
    assert marks == ["x", "!", "·"]


def test_format_report_counts_whole_file_when_filtering() -> None:
    fast = cue("Репліка, яку ніхто не встигне прочитати за секунду", 0.0, 1.0)
    reports = analyze_cues([cue("Норм."), fast, cue("Теж норм.")])
    output = format_report(reports, only_problems=True)
    assert "Реплік: 3" in output
    assert "попереджень: 1" in output
    assert output.count("симв/с") == 2  # рядок репліки + її попередження
    assert "Норм." not in output


def test_format_report_survives_empty_input() -> None:
    assert "Реплік: 0" in format_report([])
