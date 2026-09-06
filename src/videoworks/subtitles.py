"""Транскрипт → репліки субтитрів → SRT / VTT / ASS.

Це та частина, що відрізняє нормальні субтитри від автогенерованих на вигляд.
ASR дає потік слів; читабельність робиться тут: де різати репліку, як розкласти
її на рядки, скільки тримати на екрані.

Дефолти взяті з практики стрімінгових платформ: 42 символи в рядку, максимум два
рядки, мінімум 5/6 секунди на репліку, до 17 символів за секунду читання.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pysubs2

from videoworks.models import Transcript, Word
from videoworks.text import join_words

SENTENCE_END = ".!?…"
CLAUSE_END = ",;:—–"

# Слова, якими не можна закінчувати рядок: очі читача перестрибують на наступний
# рядок, втративши звʼязок. Прийменники, сполучники, частки — українські та
# англійські, бо матеріал часто змішаний.
ORPHANS = frozenset(
    """
    в у з із зі на до за під над перед при про для від без по біля крізь через між
    серед після коло щодо задля попри всупереч
    і й та а але або чи що щоб як бо ніж якщо коли хоч хоча ані ні не аби доки поки
    the a an of in on at to for from with by into onto over under and or but if as
    that which while when than about
    """.split()  # noqa: SIM905 — суцільний список читається краще за 76 літералів
)

_ORPHAN_PENALTY = 400.0  # ≈ 20 символів дисбалансу — дорожче за будь-яку кривизну
_PUNCT_BONUS = 120.0
_LINE_PENALTY = 60.0  # за інших рівних краще менше рядків


@dataclass(frozen=True)
class CueOptions:
    max_line_chars: int = 42
    max_lines: int = 2
    min_duration: float = 5 / 6
    max_duration: float = 7.0
    max_cps: float = 17.0
    min_gap: float = 0.08
    pause_break: float = 0.7
    min_cue_chars: int = 16


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str] = field(default_factory=list)
    speaker: str | None = None

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def cps(self) -> float:
        return len(self.text) / self.duration if self.duration > 0 else float("inf")


# --------------------------------------------------------------------------- #
# Складання реплік
# --------------------------------------------------------------------------- #


def build_cues(transcript: Transcript, opts: CueOptions | None = None) -> list[Cue]:
    opts = opts or CueOptions()
    words = [w for w in transcript.words() if w.w.strip()]
    if not words:
        return []

    groups = _group(words, opts)
    groups = _merge_short(groups, opts)
    groups = _rebalance(groups, opts)
    cues = [
        Cue(
            start=round(group[0].start, 3),
            end=round(group[-1].end, 3),
            lines=wrap_lines(join_words(group), opts),
        )
        for group in groups
    ]
    _fix_timing(cues, opts)
    return cues


def _group(words: list[Word], opts: CueOptions) -> list[list[Word]]:
    """Ріже потік слів на репліки по паузах, реченнях і лімітах."""
    groups: list[list[Word]] = []
    current: list[Word] = []

    for word in words:
        if current:
            gap = word.start - current[-1].end
            candidate = current + [word]
            if (
                gap >= opts.pause_break
                or not fits_lines(join_words(candidate), opts)
                or word.end - current[0].start > opts.max_duration
            ):
                groups.append(current)
                current = []

        current.append(word)

        # Кінець речення — природний шов, але не заради двох слів: короткі
        # уривки потім однаково злипнуться в _merge_short.
        if word.w.rstrip()[-1:] in SENTENCE_END and len(join_words(current)) >= opts.min_cue_chars:
            groups.append(current)
            current = []

    if current:
        groups.append(current)
    return groups


def _merge_short(groups: list[list[Word]], opts: CueOptions) -> list[list[Word]]:
    """Приліплює надто короткі репліки до сусідів, якщо ліміти дозволяють."""
    merged: list[list[Word]] = []
    for group in groups:
        if not merged:
            merged.append(group)
            continue

        previous = merged[-1]
        too_short = len(join_words(previous)) < opts.min_cue_chars or len(join_words(group)) < opts.min_cue_chars
        if too_short and _can_merge(previous, group, opts):
            merged[-1] = previous + group
        else:
            merged.append(group)
    return merged


def _rebalance(groups: list[list[Word]], opts: CueOptions) -> list[list[Word]]:
    """Пересуває шов, коли репліка лишилася сиротою на одне-два слова.

    Злиття тут не допомагає — саме через те, що разом вони не розкладаються, шов
    і виник. Натомість забираємо кілька слів у попередньої репліки: текст той
    самий, але жодна не виглядає обрубком.
    """
    for index in range(1, len(groups)):
        current = groups[index]
        if len(join_words(current)) >= opts.min_cue_chars:
            continue

        previous = groups[index - 1]
        # Шов на паузі відповідає реальному розриву в мовленні — його не рухаємо.
        if current[0].start - previous[-1].end >= opts.pause_break:
            continue

        chosen: int | None = None
        for split in range(len(previous) - 1, 0, -1):
            head, tail = previous[:split], previous[split:] + current
            if tail[-1].end - tail[0].start > opts.max_duration:
                break
            if not fits_lines(join_words(tail), opts) or len(join_words(head)) < opts.min_cue_chars:
                break
            chosen = split
            if len(join_words(tail)) >= opts.min_cue_chars:
                break

        if chosen is not None:
            groups[index - 1] = previous[:chosen]
            groups[index] = previous[chosen:] + current
    return groups


def _can_merge(left: list[Word], right: list[Word], opts: CueOptions) -> bool:
    if right[0].start - left[-1].end >= opts.pause_break:
        return False
    if right[-1].end - left[0].start > opts.max_duration:
        return False
    return fits_lines(join_words(left + right), opts)


def _fix_timing(cues: list[Cue], opts: CueOptions) -> None:
    """Витягує репліки до мінімальної тривалості й швидкості читання.

    Розширювати можна тільки вправо і тільки до початку наступної репліки:
    зсув початку розсинхронізував би субтитр із мовленням.
    """
    for index, cue in enumerate(cues):
        following = cues[index + 1].start if index + 1 < len(cues) else float("inf")
        limit = following - opts.min_gap

        needed = max(opts.min_duration, len(cue.text) / opts.max_cps)
        if cue.duration < needed:
            cue.end = min(cue.start + needed, limit)

        if cue.duration > opts.max_duration:
            cue.end = cue.start + opts.max_duration

        if cue.end > limit:
            cue.end = max(cue.start + 0.04, limit)

        cue.end = round(cue.end, 3)


# --------------------------------------------------------------------------- #
# Текст і розкладка на рядки
# --------------------------------------------------------------------------- #


def fits_lines(text: str, opts: CueOptions) -> bool:
    """Чи можна розкласти текст у дозволену кількість рядків.

    Бюджету символів (`max_line_chars × max_lines`) недостатньо: 63 символи
    влазять у 2×32, але якщо жоден проміжок між словами не дає двох рядків по 32,
    репліку доведеться рвати на три. Тому межу репліки визначає саме здійсненність
    розкладки, а не сумарна довжина.
    """
    words = text.split()
    if not words:
        return True
    if _width(words, 0, len(words)) <= opts.max_line_chars:
        return True
    return any(_split_exact(words, k, opts) is not None for k in range(2, opts.max_lines + 1))


def wrap_lines(text: str, opts: CueOptions | None = None) -> list[str]:
    """Розкладає репліку на рядки: баланс довжин, шви на пунктуації, без сиріт."""
    opts = opts or CueOptions()
    words = text.split()
    if not words:
        return [""]
    if _width(words, 0, len(words)) <= opts.max_line_chars:
        return [text]

    best: tuple[float, list[str]] | None = None
    for lines in range(2, opts.max_lines + 1):
        found = _split_exact(words, lines, opts)
        if found is None:
            continue
        cost = found[0] + (lines - 1) * _LINE_PENALTY
        if best is None or cost < best[0]:
            best = (cost, found[1])

    if best is not None:
        return best[1]

    # Жоден розклад не влазить у ліміт (довге слово або надто щільна репліка) —
    # віддаємо жадібний варіант, краще переповнений рядок, ніж втрачений текст.
    return _greedy(words, opts)


def _width(words: list[str], start: int, stop: int) -> int:
    return sum(len(w) for w in words[start:stop]) + max(0, stop - start - 1)


def _line_cost(words: list[str], start: int, stop: int, mean: float, opts: CueOptions) -> float:
    width = _width(words, start, stop)
    if width > opts.max_line_chars:
        return float("inf")

    cost = (width - mean) ** 2
    if stop < len(words):
        last = words[stop - 1]
        if last.strip(".,;:!?…»)").lower() in ORPHANS:
            cost += _ORPHAN_PENALTY
        elif last[-1:] in CLAUSE_END or last[-1:] in SENTENCE_END:
            cost -= _PUNCT_BONUS
    return cost


def _split_exact(words: list[str], lines: int, opts: CueOptions) -> tuple[float, list[str]] | None:
    """DP: розбити на рівно `lines` рядків із мінімальною вартістю."""
    count = len(words)
    if lines > count:
        return None
    mean = _width(words, 0, count) / lines

    infinity = float("inf")
    best = [[infinity] * (count + 1) for _ in range(lines + 1)]
    back = [[0] * (count + 1) for _ in range(lines + 1)]
    best[0][0] = 0.0

    for line in range(1, lines + 1):
        for end in range(line, count + 1):
            for start in range(line - 1, end):
                if best[line - 1][start] == infinity:
                    continue
                cost = best[line - 1][start] + _line_cost(words, start, end, mean, opts)
                if cost < best[line][end]:
                    best[line][end] = cost
                    back[line][end] = start

    if best[lines][count] == infinity:
        return None

    result: list[str] = []
    end = count
    for line in range(lines, 0, -1):
        start = back[line][end]
        result.append(" ".join(words[start:end]))
        end = start
    result.reverse()
    return best[lines][count], result


def _greedy(words: list[str], opts: CueOptions) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = current + [word]
        if current and _width(candidate, 0, len(candidate)) > opts.max_line_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current = candidate
    if current:
        lines.append(" ".join(current))
    return lines


# --------------------------------------------------------------------------- #
# Експорт
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SubtitleStyle:
    """Оформлення для ASS і вжигання. Основа бренд-профілю."""

    fontname: str = "Arial"
    fontsize: int | None = None  # None → 5% висоти кадру
    bold: bool = True
    primary: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: float = 2.4
    shadow: float = 0.6
    margin_v: int | None = None  # None → 6% висоти кадру
    alignment: int = 2  # знизу по центру


def _color(value: str) -> pysubs2.Color:
    raw = value.lstrip("#")
    return pysubs2.Color(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def build_ssa(
    cues: list[Cue],
    *,
    style: SubtitleStyle | None = None,
    resolution: tuple[int, int] | None = None,
) -> pysubs2.SSAFile:
    style = style or SubtitleStyle()
    subs = pysubs2.SSAFile()

    height = resolution[1] if resolution else 1080
    if resolution:
        subs.info["PlayResX"] = str(resolution[0])
        subs.info["PlayResY"] = str(resolution[1])
    # Інакше libass масштабує обведення окремо від шрифту і воно «пливе».
    subs.info["ScaledBorderAndShadow"] = "yes"

    subs.styles["Default"] = pysubs2.SSAStyle(
        fontname=style.fontname,
        fontsize=style.fontsize or max(16, round(height * 0.05)),
        bold=style.bold,
        primarycolor=_color(style.primary),
        outlinecolor=_color(style.outline_color),
        backcolor=_color(style.outline_color),
        outline=style.outline,
        shadow=style.shadow,
        alignment=pysubs2.Alignment(style.alignment),
        marginv=style.margin_v or round(height * 0.06),
    )

    for cue in cues:
        subs.append(
            pysubs2.SSAEvent(
                start=pysubs2.make_time(s=cue.start),
                end=pysubs2.make_time(s=cue.end),
                text=r"\N".join(cue.lines),
            )
        )
    return subs


def load_cues(path: Path) -> list[Cue]:
    """Читає SRT/VTT/ASS назад у репліки — щоб перевірити правку ззовні."""
    # utf-8-sig знімає BOM, який лишають вінддовські редактори; без нього
    # перший рядок першої репліки приходить із невидимим символом.
    subs = pysubs2.load(str(path), encoding="utf-8-sig")
    return [
        Cue(
            start=event.start / 1000,
            end=event.end / 1000,
            lines=event.plaintext.splitlines() or [""],
        )
        for event in subs
        if not event.is_comment
    ]


def save(
    cues: list[Cue],
    path: Path,
    *,
    style: SubtitleStyle | None = None,
    resolution: tuple[int, int] | None = None,
) -> Path:
    """Записує субтитри. Формат визначається розширенням: .srt / .vtt / .ass."""
    path.parent.mkdir(parents=True, exist_ok=True)
    build_ssa(cues, style=style, resolution=resolution).save(str(path), encoding="utf-8")
    return path
