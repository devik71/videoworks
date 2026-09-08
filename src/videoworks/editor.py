"""Перегляд і масова правка готових субтитрів.

`check` відповідає «так» або «ні» на весь файл. Коли відповідь «ні», далі
потрібно бачити кожну репліку окремо: скільки в ній символів, чи встигає її
прочитати глядач, де саме розкладка не сходиться. Цей модуль дає такий зріз —
і одну масову операцію поверх нього, скорочення задовгих реплік.

Модуль нічого не читає з диска й нічого не пише: на вході репліки, на виході
репліки або текст звіту. Файлами займається CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

from videoworks.diagnostics import Problem
from videoworks.subtitles import Cue, CueOptions, fits_lines, wrap_lines


@dataclass(frozen=True)
class CueReport:
    """Репліка з порахованими метриками — рядок майбутнього звіту."""

    index: int
    cue: Cue
    char_count: int
    line_count: int
    cps: float
    fits: bool
    problems: list[Problem]


# --------------------------------------------------------------------------- #
# Аналіз
# --------------------------------------------------------------------------- #


def analyze_cues(cues: list[Cue], opts: CueOptions | None = None) -> list[CueReport]:
    """Рахує метрики кожної репліки й перелічує її проблеми.

    Перевірки ті самі, що в `translate.check`, але порізані по репліках: там
    потрібен присуд на весь файл, тут — рядок навпроти конкретної репліки.
    """
    opts = opts or CueOptions()
    reports: list[CueReport] = []

    for index, cue in enumerate(cues, start=1):
        text = cue.text
        fits = fits_lines(text, opts)
        problems: list[Problem] = []

        if not text.strip():
            problems.append(Problem("error", "порожня репліка"))
        elif not fits:
            problems.append(
                Problem(
                    "error",
                    f"{len(text)} символів не розкладаються "
                    f"на {opts.max_lines}×{opts.max_line_chars}",
                )
            )
        elif len(cue.lines) > opts.max_lines:
            problems.append(Problem("error", f"{len(cue.lines)} рядків замість {opts.max_lines}"))

        # Нульова тривалість робить безглуздими і швидкість читання (вона
        # нескінченна), і мінімум на екрані — тому це окрема гілка.
        if cue.duration <= 0:
            problems.append(Problem("error", "нульова тривалість — не зʼявиться на екрані"))
        else:
            if cue.cps > opts.max_cps:
                problems.append(
                    Problem(
                        "warning",
                        f"{cue.cps:.1f} симв/с проти стелі {opts.max_cps:.0f} — читач не встигне",
                    )
                )
            if cue.duration < opts.min_duration:
                problems.append(
                    Problem(
                        "warning",
                        f"{cue.duration:.2f} с на екрані — менше за {opts.min_duration:.2f}",
                    )
                )

        reports.append(
            CueReport(
                index=index,
                cue=cue,
                char_count=len(text),
                line_count=len(cue.lines),
                cps=cue.cps,
                fits=fits,
                problems=problems,
            )
        )

    return reports


# --------------------------------------------------------------------------- #
# Масова правка
# --------------------------------------------------------------------------- #


def trim_cues(cues: list[Cue], max_chars: int, opts: CueOptions | None = None) -> list[Cue]:
    """Скорочує задовгі репліки до `max_chars`, лишаючи цілі слова.

    Операція з втратою: хвіст репліки просто зникає. Вона має сенс там, де
    текст усе одно не буде прочитаний — переклад удвічі довший за бюджет,
    злиплі репліки — і не замінює переписування фрази вручну.

    Таймкоди не рухаються: скорочення не має роз'їхатися з мовленням.
    """
    opts = opts or CueOptions()
    result: list[Cue] = []

    for cue in cues:
        words = cue.text.split()
        if not words or len(cue.text) <= max_chars:
            result.append(cue)
            continue

        kept: list[str] = []
        width = 0
        for word in words:
            step = len(word) + (1 if kept else 0)
            if width + step > max_chars:
                break
            kept.append(word)
            width += step

        # Порожня репліка гірша за обрубану: перша — помилка, друга — правка.
        if not kept:
            kept = words[:1]

        result.append(
            Cue(
                start=cue.start,
                end=cue.end,
                lines=wrap_lines(" ".join(kept), opts),
                speaker=cue.speaker,
            )
        )

    return result


# --------------------------------------------------------------------------- #
# Звіт
# --------------------------------------------------------------------------- #


# Позначка на початку рядка. Ні «галочки», ні «×» тут бути не може: вінддовська
# консоль стартує в cp1251, і обидва символи валять друк на UnicodeEncodeError.
# Порядок важливий — репліка з помилкою і попередженням читається як помилка.
_STATUS = {"error": "x", "warning": "!", "ok": "·"}


def _severity(report: CueReport) -> str:
    levels = {problem.level for problem in report.problems}
    if "error" in levels:
        return "error"
    return "warning" if levels else "ok"


def format_report(reports: list[CueReport]) -> str:
    """Складає звіт для терміналу: підсумок, потім репліка за реплікою."""
    errors = sum(1 for r in reports if any(p.level == "error" for p in r.problems))
    warnings = sum(1 for r in reports if any(p.level == "warning" for p in r.problems))

    lines = [f"Реплік: {len(reports)} | помилок: {errors} | попереджень: {warnings}", ""]

    for report in reports:
        status = _STATUS[_severity(report)]
        lines.append(
            f"{status} [{report.index:>3}] {report.cue.start:7.2f}–{report.cue.end:.2f} "
            f"({report.cue.duration:.1f} с) {report.char_count:>3} симв "
            f"{report.cps:>5.1f} симв/с"
        )
        lines += [f"        {line}" for line in report.cue.lines]
        lines += [f"        [{p.level}] {p.message}" for p in report.problems]
        lines.append("")

    return "\n".join(lines)
