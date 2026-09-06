"""Компактні текстові зведення для агента.

Головне обмеження агентного монтажу — контекстне вікно. `transcript.raw.json`
із таймкодом кожного слова важить сотні кілобайт і читати його агенту нема сенсу:
рішення про різку приймаються на рівні фраз. Тут ми віддаємо стислу форму —
таку, що вміщується в контекст разом із рештою розмови.
"""

from __future__ import annotations

from videoworks import ingest
from videoworks.edl import EPSILON
from videoworks.models import Edl, Scenes, Transcript
from videoworks.text import join_words, plural


def _clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:06.3f}"




def build(
    name: str,
    transcript: Transcript,
    *,
    probe_data: dict | None = None,
    scenes: Scenes | None = None,
) -> str:
    """Стисле зведення проєкту: метадані, сцени, транскрипт на рівні фраз."""
    lines: list[str] = [f"# {name}", ""]

    if probe_data:
        resolution = ingest.resolution_of(probe_data)
        size = f"{resolution[0]}×{resolution[1]}" if resolution else "?"
        lines.append(
            f"- Джерело: {ingest.duration_of(probe_data):.1f} с · {size}"
            f" · аудіо: {'є' if ingest.has_audio(probe_data) else 'немає'}"
        )

    lines.append(
        f"- Транскрипт: {transcript.language} · {transcript.backend.name}"
        f" {transcript.backend.model} · {plural(len(transcript.segments), 'фраза', 'фрази', 'фраз')}"
        f" · {plural(len(transcript.words()), 'слово', 'слова', 'слів')}"
    )

    if scenes and scenes.scenes:
        lines += ["", f"## Сцени ({len(scenes.scenes)})", ""]
        lines += [
            f"{scene.id + 1:>3}  {_clock(scene.start)}–{_clock(scene.end)}"
            f"  ({scene.duration:.1f} с)"
            for scene in scenes.scenes
        ]

    lines += ["", "## Транскрипт", ""]
    lines += [
        f"[{_clock(segment.start)}→{_clock(segment.end)}] {segment.text}"
        for segment in transcript.segments
    ]

    body = "\n".join(lines)
    return f"{body}\n\n---\n{len(body)} символів.\n"


def removed_spans(edl: Edl, duration: float) -> list[tuple[float, float]]:
    """Доповнення до збережених інтервалів — те, що піде під ніж."""
    kept = sorted((segment.src_in, segment.src_out) for segment in edl.segments)
    gaps: list[tuple[float, float]] = []
    clock = 0.0
    for start, end in kept:
        if start - clock > EPSILON:
            gaps.append((round(clock, 3), round(start, 3)))
        clock = max(clock, end)
    if duration - clock > EPSILON:
        gaps.append((round(clock, 3), round(duration, 3)))
    return gaps


def review(edl: Edl, transcript: Transcript, duration: float) -> str:
    """Що саме зникне — у словах, а не в числах.

    Це артефакт для людини: план монтажу треба бачити текстом до того, як
    щось відрендериться.
    """
    rows: list[tuple[str, float, float]] = [
        ("keep", segment.src_in, segment.src_out) for segment in edl.segments
    ]
    rows += [("cut", start, end) for start, end in removed_spans(edl, duration)]
    rows.sort(key=lambda row: row[1])

    words = transcript.words()
    lines: list[str] = []
    for kind, start, end in rows:
        inside = [w for w in words if start <= (w.start + w.end) / 2 < end]
        text = join_words(inside) if inside else f"(без мовлення, {end - start:.1f} с)"
        marker = "keep" if kind == "keep" else "cut "
        lines.append(f"[{marker}] {_clock(start)}–{_clock(end)}  {text}")

    cut_time = sum(end - start for start, end in removed_spans(edl, duration))
    share = cut_time / duration * 100 if duration else 0.0
    summary = (
        f"Лишається {edl.duration:.1f} с із {duration:.1f} с · "
        f"вирізано {cut_time:.1f} с ({share:.0f}%) у {len(edl.segments)} сегментах."
    )
    return "\n".join([*lines, "", summary])
