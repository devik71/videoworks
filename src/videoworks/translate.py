"""Переклад і правка субтитрів.

Перекладає агент — він і є мовною моделлю, окремий MT-рушій тут зайвий. Задача
цього модуля інша: дати перекладачеві повний контекст, бюджет символів на кожну
репліку і перевірку, що результат ще влазить у тайминг.

Ключове обмеження: **перекладати треба цілим документом, не по репліці**.
Ізольована репліка втрачає рід, число, звертання і термінологію — займенники
поїдуть уже на другій фразі.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from videoworks.diagnostics import Problem
from videoworks.subtitles import Cue, CueOptions, fits_lines, wrap_lines
from videoworks.text import plural

_MARKER = re.compile(r"^\[(\d+)\][^\n]*$", re.MULTILINE)

# Груба поправка на приріст тексту при перекладі. Українська та англійська
# різняться приблизно на цю величину, тож попереджаємо заздалегідь.
GROWTH_HINT = {
    ("uk", "en"): "англійська зазвичай коротша на 10–15% — місця вистачить",
    ("en", "uk"): "українська зазвичай довша на 10–20% — стежте за бюджетом",
}


# --------------------------------------------------------------------------- #
# Глосарій
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GlossaryEntry:
    source: str
    target: str  # порожній рядок = лишити як є

    @property
    def expected(self) -> str:
        return self.target or self.source


@dataclass(frozen=True)
class Glossary:
    entries: tuple[GlossaryEntry, ...] = ()

    @classmethod
    def load(cls, path: Path) -> Glossary:
        """TSV: `термін<TAB>переклад`. Порожній переклад — не перекладати."""
        if not path.exists():
            return cls()

        # utf-8-sig, а не utf-8: файл, збережений блокнотом чи PowerShell,
        # починається з BOM, і перший термін тихо переставав збігатися.
        entries: list[GlossaryEntry] = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            source, _, target = line.partition("\t")
            if source.strip():
                entries.append(GlossaryEntry(source.strip(), target.strip()))
        return cls(tuple(entries))

    def render(self) -> str:
        if not self.entries:
            return ""
        lines = [
            f"- {entry.source} → {entry.target}"
            if entry.target
            else f"- {entry.source} → лишити як є"
            for entry in self.entries
        ]
        return "\n".join(lines)

    def violations(self, source: str, translated: str) -> list[str]:
        """Терміни, що були в оригіналі, але зникли з перекладу."""
        missed: list[str] = []
        low_source, low_translated = source.lower(), translated.lower()
        for entry in self.entries:
            if entry.source.lower() not in low_source:
                continue
            if entry.expected.lower() in low_translated:
                continue
            missed.append(
                f"«{entry.source}» мало стати «{entry.target}»"
                if entry.target
                else f"«{entry.source}» мало лишитись без перекладу"
            )
        return missed


# --------------------------------------------------------------------------- #
# Бюджет символів
# --------------------------------------------------------------------------- #


def budget(cue: Cue, opts: CueOptions) -> int:
    """Скільки символів можна витратити на репліку.

    Обмежують дві різні речі: фізичний розмір (рядки × символи) і швидкість
    читання. Діє менша з них.
    """
    by_lines = opts.max_line_chars * opts.max_lines
    by_speed = int(cue.duration * opts.max_cps)
    return max(1, min(by_lines, by_speed))


# --------------------------------------------------------------------------- #
# Аркуш для перекладу
# --------------------------------------------------------------------------- #


def worksheet(
    cues: list[Cue],
    *,
    source_lang: str,
    target_lang: str,
    opts: CueOptions | None = None,
    glossary: Glossary | None = None,
) -> str:
    """Документ для перекладу: правила, глосарій, усі репліки з бюджетами."""
    opts = opts or CueOptions()
    glossary = glossary or Glossary()

    summary = (
        f"{plural(len(cues), 'репліка', 'репліки', 'реплік')} · "
        f"ліміт {opts.max_line_chars}×{opts.max_lines} символів · "
        f"до {opts.max_cps:.0f} символів за секунду."
    )
    header = [
        f"# Переклад {source_lang} → {target_lang}",
        "",
        summary,
        "",
        "## Правила",
        "",
        "- Перекладай **увесь документ разом**, а не репліку за реплікою:",
        "  ізольована фраза втрачає рід, число й звертання.",
        "- Тримайся бюджету символів у дужках. Це не побажання: довший текст",
        "  або не влізе в рядки, або не встигне прочитатись.",
        "- Не змінюй номери реплік і не додавай нових: таймкоди прив'язані до них.",
        "- Не перенось рядки вручну — розкладка робиться автоматично.",
    ]

    hint = GROWTH_HINT.get((source_lang, target_lang))
    if hint:
        header.append(f"- Поправка на мову: {hint}.")

    if glossary.entries:
        header += ["", "## Глосарій", "", glossary.render()]

    body = ["", "## Репліки", ""]
    for index, cue in enumerate(cues, start=1):
        body += [
            f"[{index}] ≤{budget(cue, opts)} симв · {cue.duration:.1f} с",
            cue.text,
            "",
        ]

    return "\n".join([*header, *body]).rstrip() + "\n"


def parse_draft(text: str) -> dict[int, str]:
    """Розбирає заповнений аркуш: `[N]` і текст під ним до наступного маркера."""
    result: dict[int, str] = {}
    markers = list(_MARKER.finditer(text))
    for position, marker in enumerate(markers):
        start = marker.end()
        stop = markers[position + 1].start() if position + 1 < len(markers) else len(text)
        body = text[start:stop].strip()
        if body:
            result[int(marker.group(1))] = " ".join(body.split())
    return result


# --------------------------------------------------------------------------- #
# Застосування і перевірка
# --------------------------------------------------------------------------- #


def apply_draft(
    cues: list[Cue],
    translations: dict[int, str],
    opts: CueOptions | None = None,
) -> list[Cue]:
    """Підставляє переклад у наявні репліки, зберігаючи таймкоди."""
    opts = opts or CueOptions()
    result: list[Cue] = []
    for index, cue in enumerate(cues, start=1):
        text = translations.get(index)
        result.append(
            Cue(
                start=cue.start,
                end=cue.end,
                lines=wrap_lines(text, opts) if text else list(cue.lines),
                speaker=cue.speaker,
            )
        )
    return result


def check(
    cues: list[Cue],
    opts: CueOptions | None = None,
    *,
    source: list[Cue] | None = None,
    glossary: Glossary | None = None,
) -> list[Problem]:
    """Чи придатні репліки до показу: розкладка, швидкість читання, глосарій."""
    opts = opts or CueOptions()
    problems: list[Problem] = []

    for index, cue in enumerate(cues, start=1):
        if not cue.text.strip():
            problems.append(Problem("error", f"репліка {index}: порожня"))
            continue

        if not fits_lines(cue.text, opts):
            problems.append(
                Problem(
                    "error",
                    f"репліка {index}: {len(cue.text)} символів не розкладаються "
                    f"на {opts.max_lines}×{opts.max_line_chars}",
                )
            )
        elif len(cue.lines) > opts.max_lines:
            problems.append(
                Problem("error", f"репліка {index}: {len(cue.lines)} рядків замість {opts.max_lines}")
            )

        if cue.cps > opts.max_cps:
            problems.append(
                Problem(
                    "warning",
                    f"репліка {index}: {cue.cps:.1f} симв/с проти стелі {opts.max_cps:.0f} "
                    f"— читач не встигне",
                )
            )

    if source and len(source) != len(cues):
        problems.append(
            Problem(
                "error",
                f"реплік стало {len(cues)} замість {len(source)} — таймкоди прив'язані до номерів",
            )
        )
    elif source and glossary and glossary.entries:
        for index, (original, translated) in enumerate(zip(source, cues, strict=True), start=1):
            for missed in glossary.violations(original.text, translated.text):
                problems.append(Problem("warning", f"репліка {index}: {missed}"))

    return problems
