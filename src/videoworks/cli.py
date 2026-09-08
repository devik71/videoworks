"""CLI."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from videoworks import benchmark as benchmark_mod
from videoworks import brief as brief_mod
from videoworks import burn as burn_mod
from videoworks import diagnostics, editor, ingest, metrics, paths, render, subtitles, translate
from videoworks import edl as edl_mod
from videoworks import fcpxml as fcpxml_mod
from videoworks import filmstrip as filmstrip_mod
from videoworks import scenes as scenes_mod
from videoworks.asr import registry, repair
from videoworks.models import Edl, Scenes, Transcript
from videoworks.text import plural

# Коли вивід перенаправлено, Python бере не UTF-8, а ANSI-кодування системи —
# під Windows це cp1251. Символів «×» і «→», які трапляються в повідомленнях,
# там немає, і команда падала на UnicodeEncodeError рівно тоді, коли мала
# показати знайдену проблему. Кодування лишаємо, псуємо тільки такий символ.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError, OSError):
        _stream.reconfigure(errors="replace")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Агентна обробка відео: транскрибація, монтаж, субтитри, переклад.",
)
console = Console()


@app.command()
def doctor() -> None:
    """Перевіряє оточення: ffmpeg, CUDA, моделі."""
    table = Table("компонент", "стан", box=None)

    for binary in ("ffmpeg", "ffprobe"):
        found = shutil.which(binary)
        table.add_row(binary, f"[green]{found}[/]" if found else "[red]не знайдено[/]")

    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        if count:
            table.add_row("CUDA", f"[green]{count} пристрій(-ів)[/]")
        else:
            table.add_row("CUDA", "[yellow]немає — працюватиме на CPU[/]")
    except Exception as exc:  # noqa: BLE001
        table.add_row("CUDA", f"[red]{exc}[/]")

    try:
        from videoworks.asr.faster_whisper_backend import pick_device

        device, compute = pick_device()
        table.add_row("пристрій", f"[green]{device} · {compute}[/]")
    except Exception as exc:  # noqa: BLE001
        table.add_row("пристрій", f"[red]{exc}[/]")

    for name, present in registry.availability().items():
        table.add_row(
            f"бекенд {name}",
            "[green]є[/]" if present else f"[dim]немає — uv sync --extra {name.replace('-', '')}[/]",
        )

    console.print(table)


@app.command()
def init(name: Annotated[str, typer.Argument(help="Назва проєкту")]) -> None:
    """Створює папку проєкту."""
    project = paths.resolve(name).create()
    console.print(f"[green]Створено[/] {project.root}")
    console.print(f"Поклади відео у [cyan]{project.media}[/] і запусти "
                  f"[cyan]videoworks transcribe {name}[/]")


@app.command(name="ingest")
def ingest_cmd(project_name: Annotated[str, typer.Argument(metavar="PROJECT")]) -> None:
    """Витягує метадані й аудіодоріжку 16 кГц."""
    project = paths.resolve(project_name)
    video = project.source_video()

    probe_data = ingest.probe(video)
    project.probe.write_text(
        json.dumps(probe_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not ingest.has_audio(probe_data):
        raise typer.BadParameter(f"У {video.name} немає аудіодоріжки.")

    duration = ingest.duration_of(probe_data)
    console.print(f"{video.name}: [cyan]{duration:.1f} с[/]")

    with console.status("Витягую аудіо…"):
        ingest.extract_audio(video, project.audio)
    console.print(f"[green]Аудіо[/] {project.audio}")


@app.command()
def transcribe(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    lang: Annotated[str | None, typer.Option(help="Код мови, напр. uk. Без нього — автовизначення")] = None,
    model: Annotated[str, typer.Option(help="Модель Whisper")] = "large-v3",
    device: Annotated[str | None, typer.Option(help="cuda або cpu")] = None,
    backend_name: Annotated[
        str, typer.Option("--backend", help="faster-whisper або whisperx")
    ] = "faster-whisper",
    out: Annotated[str | None, typer.Option("--out", help="Куди писати; типово transcript.raw.json")] = None,
) -> None:
    """Транскрибує проєкт у transcript.raw.json."""
    project = paths.resolve(project_name)
    video = project.source_video()

    if not project.audio.exists():
        console.print("Аудіо ще немає — витягую.")
        ingest.extract_audio(video, project.audio)

    backend = registry.build(backend_name, model=model, device=device, language=lang)
    console.print(f"[cyan]{backend.name}[/] · {model} · {getattr(backend, 'device', '?')}")

    def show(segment) -> None:
        console.print(f"[dim]{segment.start:7.1f}[/] {segment.text}")

    # Потоковий вивід уміє лише faster-whisper; решта віддає результат цілком.
    extra = {"on_segment": show} if backend_name == "faster-whisper" else {}
    transcript = backend.transcribe(
        project.audio, language=lang, source=str(video), **extra
    )

    transcript, repaired = repair.spread_degenerate(transcript)
    if repaired:
        console.print(
            f"[yellow]{plural(repaired, 'слово', 'слова', 'слів')} без тривалості "
            f"рознесено по проміжку між сусідами[/]"
        )

    transcript.save(Path(out) if out else project.transcript_raw)

    target = Path(out) if out else project.transcript_raw
    console.print(
        f"\n[green]Готово[/] {target.name} · мова {transcript.language} · "
        f"{plural(len(transcript.segments), 'сегмент', 'сегменти', 'сегментів')} · "
        f"{plural(len(transcript.words()), 'слово', 'слова', 'слів')}"
    )


def _default_lang(project: paths.Project) -> str:
    """Мова оригіналу береться з транскрипту, а не вгадується з імен файлів."""
    for path in (project.transcript_cut, project.transcript_raw):
        if path.exists():
            return Transcript.load(path).language
    raise typer.BadParameter("Не вдалось визначити мову — вкажи явно.")


def _probe_of(project: paths.Project, video: Path) -> dict:
    if project.probe.exists():
        return json.loads(project.probe.read_text(encoding="utf-8"))
    return ingest.probe(video)


@app.command(name="scenes")
def scenes_cmd(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    threshold: Annotated[float, typer.Option(help="Поріг ContentDetector")] = 27.0,
    adaptive: Annotated[bool, typer.Option("--adaptive", help="Стійкіший до руху камери")] = False,
) -> None:
    """Знаходить склейки → scenes.json. Візуальна сітка на додачу до текстової."""
    project = paths.resolve(project_name)
    video = project.source_video()

    duration = ingest.duration_of(_probe_of(project, video))
    with console.status("Шукаю склейки…"):
        found = scenes_mod.detect_scenes(
            video, threshold=threshold, adaptive=adaptive, duration=duration
        )
    found.save(project.scenes)

    console.print(
        f"[green]{plural(len(found.scenes), 'сцена', 'сцени', 'сцен')}[/] · {found.detector}"
    )
    for scene in found.scenes[:20]:
        console.print(f"  {scene.id + 1:>3}  {scene.start:8.2f}–{scene.end:8.2f}")
    if len(found.scenes) > 20:
        console.print(f"  [dim]…ще {len(found.scenes) - 20}[/]")


@app.command()
def brief(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    out: Annotated[bool, typer.Option("--save", help="Записати у brief.md")] = False,
) -> None:
    """Стисле зведення для агента: метадані, сцени, транскрипт на рівні фраз."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.transcript_raw.exists():
        raise typer.BadParameter(f"Немає {project.transcript_raw.name}. Спочатку transcribe.")

    text = brief_mod.build(
        project.root.name,
        Transcript.load(project.transcript_raw),
        probe_data=_probe_of(project, video),
        scenes=Scenes.load(project.scenes) if project.scenes.exists() else None,
    )
    console.print(text, highlight=False, markup=False)
    if out:
        target = project.root / "brief.md"
        target.write_text(text, encoding="utf-8")
        console.print(f"[green]Записано[/] {target}")


@app.command()
def review(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
) -> None:
    """Показує, що саме зникне за поточним edl.json — словами, а не числами."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.edl.exists():
        raise typer.BadParameter(f"Немає {project.edl.name}. Спочатку trim або план від агента.")
    if not project.transcript_raw.exists():
        raise typer.BadParameter(f"Немає {project.transcript_raw.name}. Спочатку transcribe.")

    duration = ingest.duration_of(_probe_of(project, video))
    text = brief_mod.review(
        Edl.load(project.edl), Transcript.load(project.transcript_raw), duration
    )
    console.print(text, highlight=False, markup=False)


@app.command()
def filmstrip(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    start: Annotated[float, typer.Option(help="Початок діапазону, с")] = 0.0,
    end: Annotated[float | None, typer.Option(help="Кінець діапазону, с")] = None,
    columns: Annotated[int, typer.Option(help="Колонок у сітці")] = 4,
    rows: Annotated[int, typer.Option(help="Рядків у сітці")] = 3,
) -> None:
    """Збирає контактний аркуш кадрів — картинка на вимогу, коли тексту мало."""
    project = paths.resolve(project_name)
    video = project.source_video()
    duration = ingest.duration_of(_probe_of(project, video))
    finish = end if end is not None else duration

    dest = project.frames / f"{start:.0f}-{finish:.0f}.png"
    times = filmstrip_mod.contact_sheet(
        video, dest, start=start, end=finish, columns=columns, rows=rows
    )

    console.print(f"[green]{len(times)} кадрів[/] {dest}")
    for index, moment in enumerate(times):
        console.print(f"  клітинка {index + 1:>2} (ряд {index // columns + 1}): {moment:.2f} с")


@app.command()
def trim(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    max_pause: Annotated[float, typer.Option(help="Паузи довші за це вирізаються, с")] = 0.6,
    pad: Annotated[float, typer.Option(help="Запас навколо мовлення, с")] = 0.08,
) -> None:
    """Будує edl.json, викидаючи довгі паузи. Швидкий чорновий монтаж без агента."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.transcript_raw.exists():
        raise typer.BadParameter(f"Немає {project.transcript_raw.name}. Спочатку transcribe.")

    transcript = Transcript.load(project.transcript_raw)
    duration = ingest.duration_of(_probe_of(project, video))

    spans = edl_mod.spans_from_speech(
        transcript, max_pause=max_pause, pad=pad, duration=duration
    )
    plan = edl_mod.from_spans(str(video), spans)
    plan.save(project.edl)

    saved = duration - plan.duration
    console.print(
        f"[green]{len(plan.segments)} сегментів[/] · {plan.duration:.1f} с із {duration:.1f} с"
        f" · вирізано {saved:.1f} с ({saved / duration * 100:.0f}%)"
    )
    console.print(f"  {project.edl}")


@app.command()
def cut(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    encoder: Annotated[str, typer.Option(help="libx264 або nvenc")] = "libx264",
    quality: Annotated[int, typer.Option(help="crf для libx264, cq для nvenc")] = 18,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Лише preflight, без рендеру")] = False,
) -> None:
    """Рендерить edl.json і перемаплює транскрипт під змонтоване відео."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.edl.exists():
        raise typer.BadParameter(f"Немає {project.edl.name}. Спочатку trim або план від агента.")

    plan = Edl.load(project.edl)
    probe_data = _probe_of(project, video)
    duration = ingest.duration_of(probe_data)
    with_audio = ingest.has_audio(probe_data)

    # Притягуємо план до сітки кадрів ДО рендеру й перемапування: інакше ffmpeg
    # округлює кожен сегмент сам, похибки накопичуються, і субтитри поїдуть.
    rate = fcpxml_mod.parse_rate(probe_data)
    snapped = edl_mod.quantize(plan, rate)
    drift = abs(snapped.duration - plan.duration)
    if drift > 0.001:
        console.print(
            f"[dim]план притягнуто до сітки {float(rate):g} к/с "
            f"({drift * 1000:.0f} мс різниці)[/]"
        )
    plan = snapped

    problems = edl_mod.preflight(plan, source_duration=duration)
    for problem in problems:
        colour = "red" if problem.level == "error" else "yellow"
        console.print(f"[{colour}]{problem}[/]")

    failures = edl_mod.errors(problems)
    if failures:
        raise typer.Exit(code=1)
    if not problems:
        console.print("[green]preflight чистий[/]")
    if dry_run:
        return

    dest = project.out / "cut.mp4"
    with console.status(f"Рендерю {len(plan.segments)} сегментів ({encoder})…"):
        dest, receipt = render.cut(
            video,
            plan,
            dest,
            with_audio=with_audio,
            encoder=encoder,
            quality=quality,
            source_duration=duration,
        )
    receipt.save(project.out / "cut.receipt.json")
    console.print(f"[green]Змонтовано[/] {dest}")

    if project.transcript_raw.exists():
        remapped = edl_mod.remap_transcript(Transcript.load(project.transcript_raw), plan)
        remapped.save(project.transcript_cut)
        console.print(
            f"[green]Транскрипт перемаплено[/] {project.transcript_cut.name} · "
            f"{len(remapped.words())} слів лишилось"
        )


@app.command()
def fcpxml(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
) -> None:
    """Експортує edl.json у FCPXML для Resolve / Premiere / Final Cut."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.edl.exists():
        raise typer.BadParameter(f"Немає {project.edl.name}. Спочатку trim.")

    plan = Edl.load(project.edl)
    probe_data = _probe_of(project, video)
    path = fcpxml_mod.save(
        plan,
        project.out / f"{project.root.name}.fcpxml",
        video=video,
        resolution=ingest.resolution_of(probe_data),
        rate=fcpxml_mod.parse_rate(probe_data),
        source_duration=ingest.duration_of(probe_data),
        name=project.root.name,
    )
    console.print(f"[green]FCPXML[/] {path}")


@app.command()
def subtitle(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    formats: Annotated[str, typer.Option("--format", help="Через кому: srt, vtt, ass")] = "srt,ass",
    lang: Annotated[str | None, typer.Option(help="Код мови для назви файлів")] = None,
    max_line: Annotated[int, typer.Option(help="Максимум символів у рядку")] = 42,
    max_lines: Annotated[int, typer.Option(help="Максимум рядків у репліці")] = 2,
    cps: Annotated[float, typer.Option(help="Стеля швидкості читання, символів/с")] = 17.0,
    font: Annotated[str, typer.Option(help="Шрифт для ASS і вжигання")] = "Arial",
    burn_in: Annotated[bool, typer.Option("--burn", help="Вжити субтитри в кадр")] = False,
    soft: Annotated[bool, typer.Option("--soft", help="Покласти субтитри треком у MKV")] = False,
    encoder: Annotated[str, typer.Option(help="libx264 або nvenc")] = "libx264",
) -> None:
    """Транскрипт → репліки → SRT/VTT/ASS, за потреби вжиті в кадр."""
    project = paths.resolve(project_name)

    # Транскрипт і відео мають бути з однієї стадії: перемаплені таймкоди
    # поверх сирого відео дали б розсинхрон, тим більший, чим більше вирізано.
    if project.transcript_cut.exists() and project.cut_video.exists():
        source, video = project.transcript_cut, project.cut_video
    else:
        if project.transcript_cut.exists():
            console.print(
                f"[yellow]Є {project.transcript_cut.name}, але немає {project.cut_video.name} — "
                f"беру сиру версію.[/]"
            )
        source, video = project.transcript_raw, project.source_video()

    if not source.exists():
        raise typer.BadParameter(f"Немає {source.name}. Спочатку transcribe.")
    transcript = Transcript.load(source)
    console.print(f"[dim]джерело: {source.name} + {video.name}[/]")

    # Кеш probe.json описує сире відео — для змонтованого питаємо ffprobe заново.
    probe_data = _probe_of(project, video) if video == project.source_video() else ingest.probe(video)
    resolution = ingest.resolution_of(probe_data)

    options = subtitles.CueOptions(max_line_chars=max_line, max_lines=max_lines, max_cps=cps)
    cues = subtitles.build_cues(transcript, options)
    if not cues:
        raise typer.BadParameter("У транскрипті немає слів — нема з чого робити субтитри.")

    style = subtitles.SubtitleStyle(fontname=font)
    code = lang or transcript.language
    written: list[Path] = []
    for suffix in (f.strip().lstrip(".") for f in formats.split(",") if f.strip()):
        written.append(
            subtitles.save(
                cues, project.subs / f"{code}.{suffix}", style=style, resolution=resolution
            )
        )

    longest = max(cues, key=lambda c: c.cps)
    console.print(
        f"[green]{len(cues)} реплік[/] · середня {sum(c.duration for c in cues) / len(cues):.1f} с"
        f" · пік {longest.cps:.1f} симв/с"
    )
    for path in written:
        console.print(f"  {path}")

    ass = next((p for p in written if p.suffix == ".ass"), None)
    carrier = ass or written[0]

    if soft:
        dest = burn_mod.mux_soft(video, carrier, project.out / "subtitled.mkv", language=code)
        console.print(f"[green]Мʼякий трек[/] {dest}")

    if burn_in:
        with console.status(f"Вжигаю субтитри ({encoder})…"):
            dest = burn_mod.burn(
                video, carrier, project.out / "subtitled.mp4", encoder=encoder
            )
        console.print(f"[green]Вижжено[/] {dest}")


def _report(problems: list, ok: str) -> int:
    for problem in problems:
        colour = "red" if problem.level == "error" else "yellow"
        console.print(f"[{colour}]{problem}[/]")
    failures = len(diagnostics.errors(problems))
    if not problems:
        console.print(f"[green]{ok}[/]")
    return failures


@app.command()
def benchmark(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    reference: Annotated[str | None, typer.Option(help="Еталон: .json транскрипт або .txt")] = None,
    models: Annotated[str, typer.Option(help="Моделі faster-whisper через кому")] = "small,large-v3",
    backends: Annotated[str, typer.Option(help="Бекенди через кому")] = "faster-whisper",
    lang: Annotated[str | None, typer.Option(help="Код мови")] = None,
    diarize: Annotated[bool, typer.Option("--diarize", help="Вмикати діаризацію де є")] = False,
) -> None:
    """Міряє ASR-бекенди на одному матеріалі: швидкість, WER, точність меж слів."""
    project = paths.resolve(project_name)
    video = project.source_video()
    if not project.audio.exists():
        ingest.extract_audio(video, project.audio)

    audio_seconds = ingest.duration_of(_probe_of(project, video))

    reference_tokens = None
    if reference:
        path = Path(reference)
        if path.suffix == ".json":
            reference_tokens = metrics.tokens_of(Transcript.load(path))
        else:
            reference_tokens = metrics.tokens_from_text(path.read_text(encoding="utf-8-sig"))
        console.print(f"[dim]еталон: {path.name} · {len(reference_tokens)} слів[/]")
    else:
        console.print("[yellow]Без еталона — буде лише швидкість.[/]")

    results: list[benchmark_mod.Comparison] = []
    for backend_name in (b.strip() for b in backends.split(",") if b.strip()):
        if not registry.available(backend_name):
            console.print(f"[yellow]{backend_name}: не встановлено, пропускаю[/]")
            continue

        # Whisper-моделі приймають обидва бекенди; у MOSS модель одна своя.
        variants = models.split(",") if backend_name in ("faster-whisper", "whisperx") else [None]
        for model in variants:
            label = f"{backend_name}/{model.strip()}" if model else backend_name
            console.print(f"[cyan]{label}[/]…")
            backend = registry.build(
                backend_name,
                model=model.strip() if model else None,
                diarize=diarize,
                language=lang,
            )
            run = benchmark_mod.measure(
                backend,
                project.audio,
                label=label,
                audio_seconds=audio_seconds,
                language=lang,
                source=str(video),
            )
            results.append(benchmark_mod.compare(run, reference_tokens))

    if not results:
        raise typer.BadParameter("Жоден бекенд не доступний.")

    console.print("")
    console.print(benchmark_mod.table(results), highlight=False, markup=False)


@app.command()
def worksheet(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    to: Annotated[str, typer.Option("--to", help="Цільова мова, напр. en")],
    lang: Annotated[str | None, typer.Option(help="Мова оригіналу субтитрів")] = None,
    max_line: Annotated[int, typer.Option(help="Максимум символів у рядку")] = 42,
    max_lines: Annotated[int, typer.Option(help="Максимум рядків у репліці")] = 2,
    cps: Annotated[float, typer.Option(help="Стеля швидкості читання")] = 17.0,
    save: Annotated[bool, typer.Option("--save", help="Записати у subs/<to>.draft.md")] = False,
) -> None:
    """Готує аркуш для перекладу: правила, глосарій, репліки з бюджетом символів."""
    project = paths.resolve(project_name)
    code = lang or _default_lang(project)
    source_path = project.subs / f"{code}.srt"
    if not source_path.exists():
        raise typer.BadParameter(f"Немає {source_path}. Спочатку subtitle.")

    opts = subtitles.CueOptions(max_line_chars=max_line, max_lines=max_lines, max_cps=cps)
    text = translate.worksheet(
        subtitles.load_cues(source_path),
        source_lang=code,
        target_lang=to,
        opts=opts,
        glossary=translate.Glossary.load(project.glossary),
    )

    if save:
        target = project.subs / f"{to}.draft.md"
        target.write_text(text, encoding="utf-8")
        console.print(f"[green]Записано[/] {target}")
    else:
        console.print(text, highlight=False, markup=False)


@app.command(name="apply")
def apply_cmd(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    lang: Annotated[str, typer.Option(help="Мова перекладу, напр. en")],
    source: Annotated[str | None, typer.Option("--from", help="Мова оригіналу субтитрів")] = None,
    draft: Annotated[str | None, typer.Option(help="Файл аркуша; типово subs/<lang>.draft.md")] = None,
    formats: Annotated[str, typer.Option("--format", help="Через кому: srt, vtt, ass")] = "srt,ass",
    max_line: Annotated[int, typer.Option(help="Максимум символів у рядку")] = 42,
    max_lines: Annotated[int, typer.Option(help="Максимум рядків у репліці")] = 2,
    cps: Annotated[float, typer.Option(help="Стеля швидкості читання")] = 17.0,
    force: Annotated[bool, typer.Option("--force", help="Записати навіть із помилками")] = False,
) -> None:
    """Підставляє переклад у таймкоди оригіналу й перевіряє, чи він влазить."""
    project = paths.resolve(project_name)
    code = source or _default_lang(project)
    original_path = project.subs / f"{code}.srt"
    draft_path = Path(draft) if draft else project.subs / f"{lang}.draft.md"

    if not original_path.exists():
        raise typer.BadParameter(f"Немає {original_path}. Спочатку subtitle.")
    if not draft_path.exists():
        raise typer.BadParameter(f"Немає {draft_path}. Спочатку worksheet --save і переклад.")

    opts = subtitles.CueOptions(max_line_chars=max_line, max_lines=max_lines, max_cps=cps)
    original = subtitles.load_cues(original_path)
    translations = translate.parse_draft(draft_path.read_text(encoding="utf-8-sig"))

    missing = [i for i in range(1, len(original) + 1) if i not in translations]
    if missing:
        console.print(f"[yellow]Без перекладу: {len(missing)} реплік — лишаю оригінал[/]")

    translated = translate.apply_draft(original, translations, opts)
    problems = translate.check(
        translated,
        opts,
        source=original,
        glossary=translate.Glossary.load(project.glossary),
    )
    failures = _report(problems, "усе влазить у тайминг")

    # Записуємо тільки придатний результат: субтитри, які не розкладаються,
    # мовчки затерли б робочу версію, а помітили б це вже на екрані.
    if failures and not force:
        counted = plural(failures, "помилка", "помилки", "помилок")
        console.print(f"[red]Не записано: {counted}. --force щоб усе одно.[/]")
        raise typer.Exit(code=1)

    for suffix in (f.strip().lstrip(".") for f in formats.split(",") if f.strip()):
        path = subtitles.save(translated, project.subs / f"{lang}.{suffix}")
        console.print(f"  {path}")


@app.command()
def check(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    lang: Annotated[str | None, typer.Option(help="Яку мову перевіряти")] = None,
    max_line: Annotated[int, typer.Option(help="Максимум символів у рядку")] = 42,
    max_lines: Annotated[int, typer.Option(help="Максимум рядків у репліці")] = 2,
    cps: Annotated[float, typer.Option(help="Стеля швидкості читання")] = 17.0,
) -> None:
    """Перевіряє готові субтитри: розкладка, швидкість читання, порожні репліки."""
    project = paths.resolve(project_name)
    code = lang or _default_lang(project)
    path = project.subs / f"{code}.srt"
    if not path.exists():
        raise typer.BadParameter(f"Немає {path}.")

    opts = subtitles.CueOptions(max_line_chars=max_line, max_lines=max_lines, max_cps=cps)
    cues = subtitles.load_cues(path)
    failures = _report(translate.check(cues, opts), f"{path.name}: усе гаразд")
    counted = plural(len(cues), "репліка", "репліки", "реплік")
    console.print(f"[dim]{counted} перевірено[/]")
    if failures:
        raise typer.Exit(code=1)


@app.command()
def edit(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    lang: Annotated[str | None, typer.Option(help="Мова субтитрів")] = None,
    max_line: Annotated[int, typer.Option(help="Максимум символів у рядку")] = 42,
    max_lines: Annotated[int, typer.Option(help="Максимум рядків у репліці")] = 2,
    cps: Annotated[float, typer.Option(help="Стеля швидкості читання")] = 17.0,
    trim: Annotated[int | None, typer.Option("--trim", help="Скоротити репліки до N символів")] = None,
    only_problems: Annotated[bool, typer.Option("--problems", help="Лише проблемні репліки")] = False,
    write: Annotated[bool, typer.Option("--write", help="Записати правку назад у .srt")] = False,
    force: Annotated[bool, typer.Option("--force", help="Записати навіть із помилками")] = False,
) -> None:
    """Показує кожну репліку з метриками; --trim скорочує задовгі."""
    project = paths.resolve(project_name)
    code = lang or _default_lang(project)
    path = project.subs / f"{code}.srt"
    if not path.exists():
        raise typer.BadParameter(f"Немає {path}. Спочатку subtitle.")

    opts = subtitles.CueOptions(max_line_chars=max_line, max_lines=max_lines, max_cps=cps)
    cues = subtitles.load_cues(path)

    if trim is not None:
        shortened = editor.trim_cues(cues, trim, opts)
        changed = sum(1 for old, new in zip(cues, shortened, strict=True) if old.text != new.text)
        cues = shortened
        counted = plural(changed, "репліку", "репліки", "реплік")
        console.print(f"[yellow]Скорочено до {trim} символів: {counted}[/]")

    reports = editor.analyze_cues(cues, opts)
    if only_problems:
        reports = [report for report in reports if report.problems]
    console.print(editor.format_report(reports), highlight=False, markup=False)

    failures = _report(translate.check(cues, opts), f"{path.name}: усе гаразд")

    # Без --write команда лишається оглядовою: правку видно, файл цілий.
    if not write:
        if trim is not None:
            console.print("[dim]Нічого не записано — додай --write.[/]")
        raise typer.Exit(code=1 if failures else 0)

    if failures and not force:
        counted = plural(failures, "помилка", "помилки", "помилок")
        console.print(f"[red]Не записано: {counted}. --force щоб усе одно.[/]")
        raise typer.Exit(code=1)

    subtitles.save(cues, path)
    console.print(f"[green]Записано[/] {path}")

    # Перезаписуємо лише .srt: у .ass лежить оформлення, зібране в subtitle
    # разом зі шрифтом і роздільністю, і сліпий перезапис його б знеособив.
    for suffix in ("ass", "vtt"):
        sibling = project.subs / f"{code}.{suffix}"
        if sibling.exists():
            console.print(f"[yellow]{sibling.name} лишився старим — перезбери subtitle.[/]")


@app.command()
def show(
    project_name: Annotated[str, typer.Argument(metavar="PROJECT")],
    words: Annotated[bool, typer.Option("--words", help="Показати таймкоди слів")] = False,
) -> None:
    """Друкує наявний транскрипт."""
    project = paths.resolve(project_name)
    if not project.transcript_raw.exists():
        raise typer.BadParameter(f"Немає {project.transcript_raw}. Спочатку transcribe.")

    transcript = Transcript.load(project.transcript_raw)
    console.print(f"[cyan]{transcript.source}[/] · {transcript.language} · "
                  f"{transcript.duration:.1f} с · {transcript.backend.model}")

    for segment in transcript.segments:
        console.print(f"[dim]{segment.start:7.1f}–{segment.end:.1f}[/] {segment.text}")
        if words:
            line = "  ".join(f"{w.w}[{w.start:.2f}]" for w in segment.words)
            console.print(f"    [dim]{line}[/]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
