"""Експорт EDL у FCPXML — щоб монтаж можна було довести руками.

Resolve, Premiere і Final Cut читають цей формат. Агентний монтаж не мусить
бути останнім словом: чернетку віддаємо в NLE, а точні шви людина ставить сама.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from videoworks.models import Edl

DEFAULT_RATE = Fraction(25, 1)


def parse_rate(probe_data: dict) -> Fraction:
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        raw = stream.get("r_frame_rate") or stream.get("avg_frame_rate")
        if raw and "/" in raw:
            numerator, denominator = raw.split("/")
            if int(denominator) and int(numerator):
                return Fraction(int(numerator), int(denominator))
    return DEFAULT_RATE


def _frames(seconds: float, rate: Fraction) -> int:
    return round(seconds * rate)


def _at(frames: int, rate: Fraction) -> str:
    """FCPXML вимагає раціональних значень, кратних тривалості кадру.

    Десяткові секунди тут не приймаються, тому час записуємо як
    `кадри×знаменник / чисельник`.
    """
    return f"{frames * rate.denominator}/{rate.numerator}s"


def _time(seconds: float, rate: Fraction) -> str:
    return _at(_frames(seconds, rate), rate)


def build(
    edl: Edl,
    *,
    video: Path,
    resolution: tuple[int, int] | None = None,
    rate: Fraction | None = None,
    source_duration: float | None = None,
    name: str | None = None,
) -> str:
    rate = rate or DEFAULT_RATE
    width, height = resolution or (1920, 1080)
    title = name or Path(edl.source).stem

    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        name=f"FFVideoFormat{height}p{float(rate):g}",
        frameDuration=f"{rate.denominator}/{rate.numerator}s",
        width=str(width),
        height=str(height),
    )
    asset = ET.SubElement(
        resources,
        "asset",
        id="r2",
        name=video.stem,
        start="0s",
        duration=_time(source_duration or edl.duration, rate),
        hasVideo="1",
        hasAudio="1",
        format="r1",
    )
    ET.SubElement(asset, "media-rep", kind="original-media", src=video.resolve().as_uri())

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="videoworks")
    project = ET.SubElement(event, "project", name=title)
    total = _frames(edl.duration, rate)
    sequence = ET.SubElement(
        project,
        "sequence",
        format="r1",
        duration=_at(total, rate),
        tcStart="0s",
        tcFormat="NDF",
    )
    spine = ET.SubElement(sequence, "spine")

    # Тривалість кліпа — різниця сусідніх зміщень, а не власне округлення
    # сегмента. Інакше похибки кожного сегмента накопичуються і сума кліпів
    # розходиться з довжиною секвенції: останній кліп вилазить за межу.
    ordered = sorted(edl.segments, key=lambda s: s.dst_out)
    offsets = [_frames(segment.dst_out, rate) for segment in ordered]
    bounds = [*offsets[1:], total]

    for index, (segment, offset, stop) in enumerate(zip(ordered, offsets, bounds, strict=True)):
        length = stop - offset
        if length <= 0:
            continue
        ET.SubElement(
            spine,
            "asset-clip",
            ref="r2",
            name=f"{title}-{index + 1:03d}",
            offset=_at(offset, rate),
            start=_time(segment.src_in, rate),
            duration=_at(length, rate),
        )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'


def save(edl: Edl, path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(edl, **kwargs), encoding="utf-8")
    return path
