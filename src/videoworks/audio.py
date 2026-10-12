"""Ланцюг обробки звуку: стейджгейн → компресор → де-есер → мейкап → лімітер.

Матеріал приходить різної гучності, а пороги динаміки задані в децибелах. Якби
ланцюг стояв одразу на вході, той самий компресор на тихому записі не робив би
нічого, а на гучному давив би на 10 дБ. Тому підсилення не фіксовані, а
заміряні:

1. перший прохід міряє інтегральну гучність саме того, що піде в монтаж, і
   стейджгейн підтягує її до робочого рівня ``stage_lufs``;
2. другий прохід міряє результат після динаміки — мейкап доводить його до
   ``target_lufs``;
3. лімітер тримає стелю, з 4× оверсемплом, щоб не проскакував true peak.

Обидва заміри — лише аудіо, без декодування відео й без віяла з сотень atrim,
тож коштують секунди навіть на довгому записі.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from videoworks.diagnostics import Problem
from videoworks.ingest import _binary, _run
from videoworks.models import AudioSettings, Edl

# Уся обробка йде на 48 кГц: джерела бувають і 22 кГц, і 44.1, а де-есеру
# потрібен запас над 5 кГц, та й AAC на виході однаково чекає 48.
SAMPLE_RATE = 48_000
OVERSAMPLE = 4

# Нижче цього — тиша або майже тиша: підсилювати там нічого, крім шуму.
SILENCE_LUFS = -70.0
# Більший стейджгейн уже помітно підніме шум запису — варто знати заздалегідь.
LOUD_GAIN_DB = 24.0

METER = "ebur128=peak=true:framelog=quiet"


@dataclass(frozen=True)
class Loudness:
    """Заміри EBU R128. ``integrated`` = None, якщо там тиша."""

    integrated: float | None
    true_peak: float | None
    lra: float

    @property
    def silent(self) -> bool:
        return self.integrated is None or self.integrated <= SILENCE_LUFS


@dataclass
class AudioPlan:
    """Що заміряно й яке підсилення з цього вийшло."""

    settings: AudioSettings
    source: Loudness
    stage_gain_db: float = 0.0
    dynamics: Loudness | None = None
    makeup_db: float = 0.0
    notes: list[str] = field(default_factory=list)

    def graph(self, source: str = "[ca]", dest: str = "[outa]") -> list[str]:
        return chain(self.settings, self.stage_gain_db, self.makeup_db, source=source, dest=dest)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["settings"] = self.settings.model_dump()
        return data


def _linear(db: float) -> str:
    return f"{10 ** (db / 20):.6f}"


def chain(
    settings: AudioSettings,
    stage_gain_db: float,
    makeup_db: float,
    *,
    source: str = "[ca]",
    dest: str = "[outa]",
    dynamics_only: bool = False,
) -> list[str]:
    """Твердження filter_complex від ``source`` до ``dest``.

    ``dynamics_only`` зупиняється перед мейкапом — саме цей шматок міряє другий
    прохід, щоб обчислити мейкап.
    """
    statements: list[str] = []
    current = source

    def step(filters: str) -> None:
        nonlocal current
        label = f"[au{len(statements)}]"
        statements.append(f"{current}{filters}{label}")
        current = label

    step(f"aresample={SAMPLE_RATE},volume={stage_gain_db:.2f}dB")

    if comp := settings.compressor:
        step(
            f"acompressor=threshold={_linear(comp.threshold_db)}:ratio={comp.ratio:g}"
            f":attack={comp.attack_ms:g}:release={comp.release_ms:g}:detection=rms"
        )

    if deess := settings.deesser:
        # Linkwitz-Riley 4-го порядку: смуги в сумі дають рівну АЧХ, тож поки
        # компресор верхньої смуги не спрацьовує, звук не змінюється.
        n = len(statements)
        low, high, pressed = f"[ds{n}lo]", f"[ds{n}hi]", f"[ds{n}hc]"
        statements.append(f"{current}acrossover=split={deess.frequency_hz:g}:order=4th{low}{high}")
        statements.append(
            f"{high}acompressor=threshold={_linear(deess.threshold_db)}:ratio={deess.ratio:g}"
            f":attack={deess.attack_ms:g}:release={deess.release_ms:g}:detection=peak{pressed}"
        )
        current = f"{low}{pressed}"
        step("amix=inputs=2:normalize=0")

    if not dynamics_only:
        step(f"volume={makeup_db:.2f}dB")
        if limit := settings.limiter:
            step(
                f"aresample={SAMPLE_RATE * OVERSAMPLE},"
                f"alimiter=limit={_linear(limit.ceiling_db)}:attack={limit.attack_ms:g}"
                f":release={limit.release_ms:g}:level=0,"
                f"aresample={SAMPLE_RATE}"
            )

    statements.append(f"{current}anull{dest}")
    return statements


def _selection(edl: Edl) -> str:
    """Вираз aselect, що пропускає кадри всередині сегментів EDL.

    Сума зібрана деревом: плаский ланцюжок із сотні between() ffmpeg не
    розбирає («Cannot allocate memory»).
    """
    terms = [rf"between(t\,{s.src_in:.6f}\,{s.src_out:.6f})" for s in edl.segments]

    def tree(items: list[str]) -> str:
        if len(items) == 1:
            return items[0]
        middle = len(items) // 2
        return f"({tree(items[:middle])}+{tree(items[middle:])})"

    return tree(terms) if terms else "0"


def audio_graph(edl: Edl, tail: list[str]) -> str:
    """Звук монтажу для заміру: одна гілка aselect замість сотень atrim.

    Рендер ріже точно до семпла через atrim, але віяло з сотень гілок
    проганяє весь звук стільки разів, скільки сегментів, — на чотирнадцяти
    хвилинах це хвилина на прохід. aselect бере цілі аудіокадри (десятки
    мілісекунд), а для інтегральної гучності такої точності досить.
    """
    head = f"[0:a]aselect={_selection(edl)},asetpts=N/SR/TB[ca]"
    return ";\n".join([head, *tail])


def parse_ebur128(stderr: str) -> Loudness:
    """Підсумок ebur128 у кінці stderr: I, LRA і true peak."""

    def last(label: str) -> float | None:
        found = re.findall(rf"\b{label}:\s+(-?[\d.]+|-inf)", stderr)
        if not found:
            raise RuntimeError("ebur128 не надрукував підсумку")
        return None if found[-1] == "-inf" else float(found[-1])

    # Тишу ebur128 показує як -70.0 LUFS — це нижня межа гейту, а не замір.
    integrated = last("I")
    return Loudness(
        integrated=None if integrated is None or integrated <= SILENCE_LUFS else integrated,
        true_peak=last("Peak"),
        lra=last("LRA") or 0.0,
    )


def _measure(args: list[str], graph: str) -> Loudness:
    # Граф із сотень сегментів не влазить у командний рядок Windows — файлом.
    with tempfile.TemporaryDirectory(prefix="videoworks-") as tmp:
        script = Path(tmp) / "measure.txt"
        script.write_text(graph, encoding="utf-8")
        result = _run(
            [
                _binary("ffmpeg"),
                "-nostdin",
                "-hide_banner",
                "-vn",
                *args,
                "-/filter_complex",
                str(script),
                "-map",
                "[meter]",
                "-f",
                "null",
                "-",
            ]
        )
    return parse_ebur128(result.stderr)


def measure(video: Path, edl: Edl, tail: list[str] | None = None) -> Loudness:
    """Гучність змонтованого звуку; ``tail`` — обробка між [ca] і [pre]."""
    tail = tail or ["[ca]anull[pre]"]
    graph = audio_graph(edl, [*tail, f"[pre]{METER}[meter]"])
    return _measure(["-i", str(video.resolve())], graph)


def measure_file(path: Path) -> Loudness:
    """Гучність готового файлу — для receipt і звіту."""
    return _measure(["-i", str(path.resolve())], f"[0:a]{METER}[meter]")


def plan(video: Path, edl: Edl) -> AudioPlan:
    """Два заміри → стейджгейн і мейкап."""
    settings = edl.audio
    source = measure(video, edl)
    result = AudioPlan(settings=settings, source=source)

    if source.silent:
        result.notes.append("звук майже тихий — підсилення не застосовано")
        return result

    result.stage_gain_db = round(settings.stage_lufs - source.integrated, 2)
    if result.stage_gain_db > LOUD_GAIN_DB:
        result.notes.append(
            f"стейджгейн +{result.stage_gain_db:.1f} дБ — разом із голосом підніметься й шум"
        )

    dynamics = chain(
        settings, result.stage_gain_db, 0.0, source="[ca]", dest="[pre]", dynamics_only=True
    )
    result.dynamics = measure(video, edl, dynamics)
    if result.dynamics.silent:
        return result
    result.makeup_db = round(settings.target_lufs - result.dynamics.integrated, 2)
    return result


def validate(settings: AudioSettings) -> list[Problem]:
    """Межі, поза якими параметр — помилка в плані, а не творчий вибір."""
    if not settings.normalize:
        return []

    problems: list[Problem] = []

    def check(name: str, value: float, low: float, high: float) -> None:
        if not low <= value <= high:
            problems.append(
                Problem("error", f"audio.{name} = {value:g} поза межами [{low:g}, {high:g}]")
            )

    if settings.normalize != "ebur128":
        problems.append(
            Problem("error", f"audio.normalize: невідомий режим {settings.normalize!r}")
        )
    check("target_lufs", settings.target_lufs, -70.0, -5.0)
    check("stage_lufs", settings.stage_lufs, -40.0, -10.0)

    if comp := settings.compressor:
        check("compressor.threshold_db", comp.threshold_db, -60.0, 0.0)
        check("compressor.ratio", comp.ratio, 1.0, 20.0)
        check("compressor.attack_ms", comp.attack_ms, 0.01, 2000.0)
        check("compressor.release_ms", comp.release_ms, 0.01, 9000.0)

    if deess := settings.deesser:
        check("deesser.frequency_hz", deess.frequency_hz, 2000.0, 12000.0)
        check("deesser.threshold_db", deess.threshold_db, -60.0, 0.0)
        check("deesser.ratio", deess.ratio, 1.0, 20.0)
        check("deesser.attack_ms", deess.attack_ms, 0.01, 2000.0)
        check("deesser.release_ms", deess.release_ms, 0.01, 9000.0)

    if limit := settings.limiter:
        # alimiter не опускає стелю нижче -24 дБ (limit ≥ 0.0625).
        check("limiter.ceiling_db", limit.ceiling_db, -24.0, 0.0)
        check("limiter.attack_ms", limit.attack_ms, 0.1, 80.0)
        check("limiter.release_ms", limit.release_ms, 1.0, 8000.0)
    elif settings.target_lufs > -14.0:
        problems.append(
            Problem(
                "warning", f"target_lufs {settings.target_lufs:g} без лімітера — піки підуть у кліп"
            )
        )

    return problems
