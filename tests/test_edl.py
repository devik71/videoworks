from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from videoworks.edl import (
    PreflightFailed,
    errors,
    from_spans,
    locate,
    preflight,
    quantize,
    remap_transcript,
    spans_from_speech,
)
from videoworks.models import AudioSettings, BackendInfo, Edl, EdlSegment, Segment, Transcript, Word


def transcript_of(pairs: list[tuple[str, float, float]], *, duration: float = 100.0) -> Transcript:
    words = [Word(w=w, start=s, end=e) for w, s, e in pairs]
    return Transcript(
        source="media/raw.mp4",
        duration=duration,
        language="uk",
        backend=BackendInfo(name="test", model="test"),
        segments=[
            Segment(
                id=0,
                start=words[0].start,
                end=words[-1].end,
                text=" ".join(w.w for w in words),
                words=words,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Побудова EDL
# --------------------------------------------------------------------------- #


def test_from_spans_lays_segments_end_to_end() -> None:
    plan = from_spans("raw.mp4", [(10.0, 15.0), (30.0, 32.5)])
    assert [s.dst_out for s in plan.segments] == [0.0, 5.0]
    assert plan.duration == pytest.approx(7.5)


def test_from_spans_drops_empty_spans() -> None:
    plan = from_spans("raw.mp4", [(10.0, 15.0), (20.0, 20.0)])
    assert len(plan.segments) == 1


def test_spans_cut_only_long_pauses() -> None:
    transcript = transcript_of([
        ("один", 1.0, 1.4),
        ("два", 1.5, 1.9),
        # пауза 3 с
        ("три", 4.9, 5.3),
    ])
    spans = spans_from_speech(transcript, max_pause=0.6, pad=0.0)
    assert spans == [(1.0, 1.9), (4.9, 5.3)]


def test_spans_pad_but_stay_inside_source() -> None:
    transcript = transcript_of([("слово", 0.02, 9.98)], duration=10.0)
    spans = spans_from_speech(transcript, pad=0.5, duration=10.0)
    assert spans == [(0.0, 10.0)]


def test_spans_merge_after_padding() -> None:
    # Паузи 0.7 с достатньо для розриву, але padding 0.5 з обох боків їх стуляє.
    transcript = transcript_of([("один", 1.0, 1.4), ("два", 2.1, 2.5)])
    spans = spans_from_speech(transcript, max_pause=0.6, pad=0.5)
    assert len(spans) == 1


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #


def test_quantize_expands_outward_never_clipping() -> None:
    # 1.013 → 1.00, 4.987 → 5.00: межі розсуваються назовні, щоб квантування
    # не зрізало атаку приголосного на початку слова.
    plan = quantize(from_spans("raw.mp4", [(1.013, 4.987)]), 25)
    segment = plan.segments[0]
    assert segment.src_in == pytest.approx(1.0)
    assert segment.src_out == pytest.approx(5.0)
    assert segment.src_in <= 1.013
    assert segment.src_out >= 4.987


def test_quantized_segments_last_whole_frames() -> None:
    spans = [(i * 3.017, i * 3.017 + 1.993) for i in range(20)]
    plan = quantize(from_spans("raw.mp4", spans), 25)
    for segment in plan.segments:
        assert (segment.duration * 25) == pytest.approx(round(segment.duration * 25))


def test_quantized_timeline_stays_seamless() -> None:
    spans = [(i * 3.017, i * 3.017 + 1.993) for i in range(20)]
    plan = quantize(from_spans("raw.mp4", spans), 25)
    # Найважливіше: після притягування dst_out усе ще суцільний, інакше
    # preflight відхилив би власний результат.
    assert preflight(plan) == []


def test_quantize_promotes_subframe_segment_to_one_frame() -> None:
    # Розширення назовні означає, що 10 мс стають кадром, а не зникають:
    # краще один кадр, ніж утрачений шматок.
    plan = quantize(from_spans("raw.mp4", [(0.0, 5.0), (10.0, 10.01)]), 25)
    assert len(plan.segments) == 2
    assert plan.segments[1].duration == pytest.approx(0.04)


def test_quantize_handles_ntsc_rate() -> None:
    from fractions import Fraction

    rate = Fraction(30000, 1001)
    plan = quantize(from_spans("raw.mp4", [(1.0, 4.0), (10.0, 12.0)]), rate)
    for segment in plan.segments:
        frames = segment.duration * float(rate)
        assert frames == pytest.approx(round(frames), abs=1e-3)


def test_quantize_is_idempotent() -> None:
    once = quantize(from_spans("raw.mp4", [(1.013, 4.987), (9.001, 12.999)]), 25)
    twice = quantize(once, 25)
    assert [(s.src_in, s.src_out, s.dst_out) for s in once.segments] == [
        (s.src_in, s.src_out, s.dst_out) for s in twice.segments
    ]


def test_preflight_accepts_sane_plan() -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0), (10.0, 12.0)])
    assert preflight(plan, source_duration=30.0) == []


def test_preflight_rejects_empty_plan() -> None:
    assert errors(preflight(Edl(source="raw.mp4")))


def test_preflight_rejects_segment_past_source_end() -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0), (28.0, 40.0)])
    assert any("за межами джерела" in p.message for p in errors(preflight(plan, source_duration=30.0)))


def test_preflight_catches_broken_destination_timeline() -> None:
    # dst_out другого сегмента має бути 5.0 — розрив зсунув би субтитри.
    plan = Edl(
        source="raw.mp4",
        segments=[
            EdlSegment(src_in=0.0, src_out=5.0, dst_out=0.0),
            EdlSegment(src_in=10.0, src_out=12.0, dst_out=9.0),
        ],
    )
    assert any("таймлайні" in p.message for p in errors(preflight(plan)))


def test_preflight_rejects_absurd_loudness() -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0)], AudioSettings(normalize="ebur128", target_lufs=12.0))
    assert any("target_lufs" in p.message for p in errors(preflight(plan)))


def test_preflight_warns_on_subframe_segment() -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0), (10.0, 10.01)])
    problems = preflight(plan, source_duration=30.0)
    assert not errors(problems)
    assert any(p.level == "warning" for p in problems)


def test_preflight_failure_blocks_render(tmp_path: Path) -> None:
    from videoworks import render

    plan = from_spans("raw.mp4", [(0.0, 5.0)])
    with pytest.raises(PreflightFailed):
        render.cut(Path("missing.mp4"), plan, tmp_path / "cut.mp4", source_duration=2.0)


# --------------------------------------------------------------------------- #
# Перемапування таймкодів — серце пайплайну
# --------------------------------------------------------------------------- #


def test_locate_finds_owning_segment() -> None:
    segments = from_spans("raw.mp4", [(10.0, 15.0), (30.0, 32.0)]).segments
    assert locate(12.0, segments) == 0
    assert locate(31.0, segments) == 1
    assert locate(20.0, segments) is None


def test_words_inside_first_segment_keep_their_offset() -> None:
    transcript = transcript_of([("один", 10.5, 11.0), ("два", 11.2, 11.8)])
    plan = from_spans("raw.mp4", [(10.0, 15.0)])
    result = remap_transcript(transcript, plan)
    assert [(w.w, w.start) for w in result.words()] == [("один", 0.5), ("два", 1.2)]


def test_words_after_a_cut_shift_by_removed_time() -> None:
    transcript = transcript_of([
        ("перед", 10.5, 11.0),
        ("вирізане", 20.0, 21.0),
        ("після", 30.5, 31.0),
    ])
    plan = from_spans("raw.mp4", [(10.0, 15.0), (30.0, 32.0)])
    result = remap_transcript(transcript, plan)
    assert [w.w for w in result.words()] == ["перед", "після"]
    # 30.5 лежить на 0.5 с усередині другого шматка, який починається на 5.0.
    assert result.words()[1].start == pytest.approx(5.5)


def test_removed_words_disappear() -> None:
    transcript = transcript_of([("лишити", 1.0, 2.0), ("викинути", 50.0, 51.0)])
    plan = from_spans("raw.mp4", [(0.0, 5.0)])
    assert [w.w for w in remap_transcript(transcript, plan).words()] == ["лишити"]


def test_word_on_the_seam_follows_its_midpoint() -> None:
    # Слово 4.8–5.2 переважно всередині шматка, що закінчується на 5.0.
    transcript = transcript_of([("шов", 4.8, 5.2)])
    plan = from_spans("raw.mp4", [(0.0, 5.0)])
    kept = remap_transcript(transcript, plan).words()
    assert len(kept) == 1
    assert kept[0].end <= 5.0 + 1e-6


def test_remapped_words_stay_ordered_and_bounded() -> None:
    pairs = [(f"слово{i}", i * 1.0, i * 1.0 + 0.8) for i in range(40)]
    plan = from_spans("raw.mp4", [(2.0, 9.0), (15.0, 22.0), (30.0, 36.0)])
    result = remap_transcript(transcript_of(pairs), plan)

    words = result.words()
    assert words
    for earlier, later in pairwise(words):
        assert earlier.start <= later.start
    assert all(0.0 <= w.start <= result.duration + 1e-6 for w in words)
    assert result.duration == pytest.approx(plan.duration)


def test_remap_splits_segment_across_two_cuts() -> None:
    transcript = transcript_of([
        ("а", 1.0, 1.5),
        ("б", 2.0, 2.5),
        ("в", 20.0, 20.5),
        ("г", 21.0, 21.5),
    ])
    plan = from_spans("raw.mp4", [(0.0, 5.0), (19.0, 25.0)])
    result = remap_transcript(transcript, plan)
    # Один вихідний сегмент, але два шматки EDL — має стати двома сегментами,
    # інакше склеїлися б репліки з різних місць запису.
    assert len(result.segments) == 2
    assert [s.text for s in result.segments] == ["а б", "в г"]


def test_remap_of_untouched_plan_is_identity() -> None:
    pairs = [("один", 1.0, 1.5), ("два", 2.0, 2.5)]
    plan = from_spans("raw.mp4", [(0.0, 100.0)])
    result = remap_transcript(transcript_of(pairs), plan)
    assert [(w.w, w.start, w.end) for w in result.words()] == pairs
