from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from videoworks.edl import from_spans
from videoworks.fcpxml import build, parse_rate


def parsed(**kwargs) -> ET.Element:
    plan = kwargs.pop("plan", from_spans("raw.mp4", [(0.0, 3.37), (4.02, 7.85), (8.71, 11.75)]))
    xml = build(plan, video=Path("raw.mp4"), rate=Fraction(25, 1), **kwargs)
    return ET.fromstring(xml.split("\n", 2)[2])


def frames(value: str, rate: Fraction = Fraction(25, 1)) -> int:
    numerator, denominator = value.rstrip("s").split("/")
    return int(numerator) * rate.numerator // (int(denominator) * rate.denominator)


def test_clip_durations_sum_to_sequence_duration() -> None:
    root = parsed()
    sequence = root.find(".//sequence")
    clips = root.findall(".//asset-clip")
    assert len(clips) == 3
    total = sum(frames(clip.get("duration")) for clip in clips)
    assert total == frames(sequence.get("duration"))


def test_clips_are_laid_end_to_end() -> None:
    clips = parsed().findall(".//asset-clip")
    clock = 0
    for clip in clips:
        assert frames(clip.get("offset")) == clock
        clock += frames(clip.get("duration"))


def test_times_are_rational_not_decimal() -> None:
    root = parsed()
    for clip in root.findall(".//asset-clip"):
        for attribute in ("offset", "start", "duration"):
            value = clip.get(attribute)
            assert value.endswith("s")
            assert "/" in value
            assert "." not in value


def test_media_path_is_a_file_uri() -> None:
    root = parsed()
    src = root.find(".//media-rep").get("src")
    assert src.startswith("file:///")


def test_parse_rate_reads_ntsc_fraction() -> None:
    probe = {"streams": [{"codec_type": "video", "r_frame_rate": "30000/1001"}]}
    assert parse_rate(probe) == Fraction(30000, 1001)


def test_parse_rate_falls_back_when_missing() -> None:
    assert parse_rate({"streams": [{"codec_type": "audio"}]}) == Fraction(25, 1)


def test_ntsc_rate_keeps_timeline_consistent() -> None:
    plan = from_spans("raw.mp4", [(0.0, 3.37), (4.02, 7.85)])
    xml = build(plan, video=Path("raw.mp4"), rate=Fraction(30000, 1001))
    root = ET.fromstring(xml.split("\n", 2)[2])
    rate = Fraction(30000, 1001)
    clips = root.findall(".//asset-clip")
    total = sum(frames(clip.get("duration"), rate) for clip in clips)
    assert total == frames(root.find(".//sequence").get("duration"), rate)


def test_rejects_nothing_but_produces_valid_xml_for_single_segment() -> None:
    plan = from_spans("raw.mp4", [(1.0, 2.0)])
    root = parsed(plan=plan)
    assert len(root.findall(".//asset-clip")) == 1


@pytest.mark.parametrize("seconds", [0.0, 0.02, 1.999])
def test_source_start_rounds_to_whole_frames(seconds: float) -> None:
    plan = from_spans("raw.mp4", [(seconds, seconds + 2.0)])
    clip = parsed(plan=plan).find(".//asset-clip")
    assert frames(clip.get("start")) == round(seconds * 25)
