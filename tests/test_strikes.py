import json
import os
from itertools import pairwise

import pytest

from videoworks.models import (
    BackendInfo,
    ManualSpan,
    Segment,
    Strikes,
    StruckWord,
    Transcript,
    Word,
)
from videoworks.paths import Project
from videoworks.strikes import (
    from_indices,
    indices,
    load,
    music_gaps,
    phrases,
    rematch,
    spans,
    state,
    store,
)

W = StruckWord


def segment(i, *words):
    """Репліка зі слів (w, start, end[, prob])."""
    made = [Word(w=w, start=s, end=e, prob=rest[0] if rest else None) for w, s, e, *rest in words]
    text = " ".join(w for w, *_ in words)
    return Segment(id=i, start=made[0].start, end=made[-1].end, text=text, words=made)


def transcript_of(*segments, duration=100.0):
    backend = BackendInfo(name="t", model="m")
    return Transcript(
        source="s", duration=duration, language="uk", backend=backend, segments=list(segments)
    )


def transcript(*words, duration=100.0):
    """Один Segment із цими словами."""
    return transcript_of(segment(0, *words), duration=duration)


T = transcript(("раз", 1.0, 1.3), ("два", 1.4, 1.7), ("три", 1.8, 2.1))
EMPTY = Strikes(source="s", transcript_sha256="x")


def test_span_is_word_plus_pad_on_both_sides():
    assert spans([W(w="блін", start=10.0, end=10.3)], [], duration=100) == [(9.85, 10.45)]


def test_neighbours_closer_than_merge_gap_become_one_span():
    got = spans([W(w="а", start=10.0, end=10.3), W(w="б", start=10.8, end=11.0)], [], duration=100)
    assert got == [(9.85, 11.15)]  # проміжок 10.45→10.65 = 0.2 < 0.25


def test_distant_words_stay_apart():
    got = spans([W(w="а", start=10.0, end=10.3), W(w="б", start=12.0, end=12.2)], [], duration=100)
    assert len(got) == 2


def test_spans_are_clamped_to_the_recording():
    got = spans([W(w="а", start=0.05, end=0.2)], [ManualSpan(start=99.9, end=100.0)], duration=100)
    assert got == [(0.0, 0.35), (99.75, 100.0)]


def test_zero_length_word_still_gets_two_pads():
    ((start, end),) = spans([W(w="е", start=5.0, end=5.0)], [], duration=100)
    assert end - start == pytest.approx(0.30)


def test_unordered_and_overlapping_input_gives_sorted_disjoint_spans():
    got = spans(
        [W(w="б", start=20, end=21), W(w="а", start=5, end=6), W(w="в", start=20.5, end=22)],
        [ManualSpan(start=5.5, end=7)],
        duration=100,
    )
    assert got == sorted(got) and all(a[1] < b[0] for a, b in pairwise(got))
    assert got == [(4.85, 7.15), (19.85, 22.15)]


def test_values_are_rounded_to_milliseconds():
    ((start, end),) = spans([W(w="а", start=1 / 3, end=2 / 3)], [], duration=100)
    assert (start, end) == (round(start, 3), round(end, 3))


def test_store_recomputes_spans_and_round_trips(tmp_path):
    path = tmp_path / "strikes.json"
    store(
        path,
        Strikes(
            source="media/a.mov",
            transcript_sha256="x",
            words=[W(w="а", start=10.0, end=10.3)],
            spans=[(0.0, 1.0)],
        ),
        duration=100,
    )
    assert Strikes.load(path).spans == [(9.85, 10.45)]
    assert not list(tmp_path.glob("*.tmp"))


def test_store_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    # os.replace кидає OSError → старий файл цілий, виняток піднімається
    path = tmp_path / "strikes.json"
    store(path, Strikes(source="a.mov", transcript_sha256="x"), duration=100)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("диск зайнятий")

    monkeypatch.setattr(os, "replace", boom)
    new = Strikes(source="a.mov", transcript_sha256="x", words=[W(w="а", start=1, end=2)])
    with pytest.raises(OSError):
        store(path, new, duration=100)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "bad",
    [
        {"w": "а", "start": 2, "end": 1},
        {"w": "а", "start": -1, "end": 1},
        {"w": "а", "start": float("nan"), "end": 1},
        {"w": "а", "start": 0, "end": float("inf")},
    ],
)
def test_struck_word_refuses_broken_times(bad):
    with pytest.raises(ValueError):
        StruckWord(**bad)


def test_manual_span_must_have_positive_length():
    with pytest.raises(ValueError):
        ManualSpan(start=3, end=3)


def test_project_points_at_strikes_json(tmp_path):
    assert Project(tmp_path).strikes == tmp_path / "strikes.json"


def test_phrase_breaks_on_a_pause_longer_than_0_6():
    t = transcript(("раз", 1.0, 1.3), ("два", 1.4, 1.7), ("три", 2.4, 2.7))
    assert [(p.first, p.count) for p in phrases(t)] == [(0, 2), (2, 1)]


def test_phrase_keeps_a_pause_of_exactly_0_6():
    t = transcript(("раз", 1.0, 1.3), ("два", 1.9, 2.2))
    assert [(p.first, p.count) for p in phrases(t)] == [(0, 2)]


def test_phrase_is_capped_at_14_words():
    t = transcript(*[(f"с{i}", i * 0.3, i * 0.3 + 0.2) for i in range(30)])
    assert [p.count for p in phrases(t)] == [14, 14, 2]


def test_phrase_breaks_at_segment_boundaries():
    t = transcript_of(segment(0, ("раз", 1.0, 1.3)), segment(1, ("два", 1.4, 1.7)))
    assert [(p.first, p.count) for p in phrases(t)] == [(0, 1), (1, 1)]


def test_phrase_start_and_end_are_first_and_last_word():
    (p,) = phrases(transcript(("раз", 1.0, 1.3), ("два", 1.4, 1.7)))
    assert (p.start, p.end) == (1.0, 1.7)


def test_blank_words_do_not_shift_global_indices():
    t = transcript(("раз", 1.0, 1.3), ("  ", 1.35, 1.38), ("два", 1.4, 1.7))
    assert [(p.first, p.count) for p in phrases(t)] == [(0, 1), (2, 1)]


def test_music_gaps_include_lead_in_and_tail():
    t = transcript(("раз", 10.0, 10.3), ("два", 20.0, 20.3), duration=30.0)
    assert music_gaps(phrases(t), 30.0) == [(0.0, 10.0), (10.3, 20.0), (20.3, 30.0)]


def test_short_gaps_are_not_music_rows():
    t = transcript(("раз", 0.0, 1.0), ("два", 2.5, 3.0), duration=3.0)
    assert music_gaps(phrases(t), 3.0) == []


def test_rematch_follows_a_word_that_moved_slightly():
    old = Strikes(source="s", transcript_sha256="old", words=[W(w="Блін,", start=10.0, end=10.3)])
    new, unglued = rematch(old, transcript(("блін", 10.1, 10.45)), "new")
    assert (new.words[0].start, unglued, new.transcript_sha256) == (10.1, 0, "new")


def test_rematch_turns_a_lost_word_into_a_manual_span():
    old = Strikes(source="s", transcript_sha256="old", words=[W(w="блін", start=10.0, end=10.3)])
    new, unglued = rematch(old, transcript(("привіт", 50.0, 50.4)), "new")
    assert new.words == [] and unglued == 1
    assert (new.manual[0].start, new.manual[0].end) == (10.0, 10.3)


def test_rematch_widens_a_zero_length_lost_word():
    old = Strikes(source="s", transcript_sha256="old", words=[W(w="е", start=10.0, end=10.0)])
    new, _ = rematch(old, transcript(("привіт", 50.0, 50.4)), "new")
    assert (new.manual[0].start, new.manual[0].end) == (10.0, 10.05)


def test_rematch_needs_text_and_time():
    old = Strikes(source="s", transcript_sha256="old", words=[W(w="блін", start=10.0, end=10.3)])
    new, unglued = rematch(old, transcript(("блін", 40.0, 40.3)), "new")
    assert new.words == [] and unglued == 1


def test_rematch_picks_the_nearest_candidate():
    old = Strikes(source="s", transcript_sha256="old", words=[W(w="так", start=10.0, end=10.3)])
    new, _ = rematch(old, transcript(("так", 9.75, 10.0), ("так", 10.1, 10.4)), "new")
    assert new.words[0].start == 10.1


def test_rematch_keeps_manual_spans_and_never_loses_intent():
    old = Strikes(
        source="s",
        transcript_sha256="old",
        words=[W(w=f"с{i}", start=i * 5.0, end=i * 5.0 + 0.3) for i in range(5)],
        manual=[ManualSpan(start=60, end=61), ManualSpan(start=70, end=71)],
    )
    for new_t in (
        transcript(("с0", 0.0, 0.3), ("с1", 5.1, 5.4)),
        transcript(("інше", 3.0, 3.3)),
        transcript(("с0", 0.0, 0.3), ("с0", 0.0, 0.3), ("с0", 0.0, 0.3)),
    ):
        new, _ = rematch(old, new_t, "new")
        assert len(new.words) + len(new.manual) == 7


def test_load_without_file_is_empty(tmp_path):
    got, unglued = load(tmp_path / "strikes.json", transcript=T, transcript_sha="x", source="s")
    assert (got.words, got.manual, got.transcript_sha256, got.source) == ([], [], "x", "s")
    assert unglued == 0


def test_load_with_matching_hash_returns_the_file_as_is(tmp_path):
    path = tmp_path / "strikes.json"
    saved = Strikes(source="s", transcript_sha256="x", words=[W(w="раз", start=1.0, end=1.3)])
    store(path, saved, duration=100)
    got, unglued = load(path, transcript=T, transcript_sha="x", source="s")
    assert (len(got.words), unglued) == (1, 0)


def test_load_with_stale_hash_rematches_without_writing(tmp_path):
    path = tmp_path / "strikes.json"
    saved = Strikes(source="s", transcript_sha256="old", words=[W(w="раз", start=1.05, end=1.3)])
    store(path, saved, duration=100)
    before = path.read_bytes()
    got, unglued = load(path, transcript=T, transcript_sha="new", source="s")
    assert (got.transcript_sha256, got.words[0].start, unglued) == ("new", 1.0, 0)
    assert path.read_bytes() == before


def test_load_tolerates_a_bom(tmp_path):
    path = tmp_path / "strikes.json"
    store(path, Strikes(source="s", transcript_sha256="x"), duration=100)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    got, _ = load(path, transcript=T, transcript_sha="x", source="s")
    assert got.transcript_sha256 == "x"


def test_load_reports_a_broken_file_by_name(tmp_path):
    (tmp_path / "strikes.json").write_text("{не json", encoding="utf-8")
    with pytest.raises(ValueError, match="strikes.json"):
        load(tmp_path / "strikes.json", transcript=T, transcript_sha="x", source="s")


def test_load_reports_an_invalid_shape_by_name(tmp_path):
    (tmp_path / "strikes.json").write_text('{"source": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="strikes.json"):
        load(tmp_path / "strikes.json", transcript=T, transcript_sha="x", source="s")


def test_indices_round_trip_through_from_indices():
    s = from_indices([0, 2], [(40.0, 41.0)], transcript=T, transcript_sha="x", source="s")
    assert indices(s, T) == [0, 2] and s.manual[0].end == 41.0


def test_from_indices_sorts_and_dedupes():
    s = from_indices([2, 0, 2], [], transcript=T, transcript_sha="x", source="s")
    assert [w.w for w in s.words] == ["раз", "три"]


@pytest.mark.parametrize("bad", [-1, 3, 99])
def test_from_indices_refuses_unknown_words(bad):
    with pytest.raises(ValueError):
        from_indices([bad], [], transcript=T, transcript_sha="x", source="s")


def test_from_indices_refuses_blank_words():
    t = transcript(("раз", 1.0, 1.3), ("", 1.4, 1.5))
    with pytest.raises(ValueError):
        from_indices([1], [], transcript=t, transcript_sha="x", source="s")


def test_state_marks_uncertain_words_dim_and_is_json_safe():
    data = state(transcript(("е", 1.0, 1.2, 0.3), ("так", 1.3, 1.6, 0.9)), EMPTY, title="x")
    assert [w["dim"] for w in data["words"]] == [True, False]
    assert "Infinity" not in json.dumps(data) and "NaN" not in json.dumps(data)


def test_state_describes_phrases_gaps_and_struck_words():
    t = transcript(("раз", 10.0, 10.3), ("два", 20.0, 20.3), duration=30.0)
    s = from_indices([1], [(25.0, 26.0)], transcript=t, transcript_sha="sha", source="s")
    data = state(t, s, title="aerobic", unglued=2)
    assert data["title"] == "aerobic" and data["transcript"] == "sha" and data["unglued"] == 2
    assert data["struck"] == [1] and data["manual"] == [[25.0, 26.0]]
    assert data["phrases"][1] == {"start": 20.0, "end": 20.3, "first": 1, "count": 1}
    assert data["gaps"] == [[0.0, 10.0], [10.3, 20.0], [20.3, 30.0]]
    assert data["counts"]["words"] == 1 and data["counts"]["manual"] == 1


def test_state_counts_seconds_from_spans_not_from_words():
    t = transcript(("раз", 10.0, 10.3), ("два", 10.4, 10.7))
    s = from_indices([0, 1], [], transcript=t, transcript_sha="x", source="s")
    data = state(t, s, title="x")
    assert data["spans"] == [[9.85, 10.85]]  # злиті: 1.0 с, а не 2 × 0.6
    assert data["counts"]["seconds"] == 1.0


def test_state_ignores_stale_spans_stored_in_the_file():
    t = transcript(("раз", 10.0, 10.3))
    s = from_indices([0], [], transcript=t, transcript_sha="x", source="s")
    s.spans = [(0.0, 50.0)]
    assert state(t, s, title="x")["spans"] == [[9.85, 10.45]]


def test_state_rounds_floats_to_three_decimals():
    t = transcript(("раз", 1 / 3, 2 / 3))
    assert state(t, EMPTY, title="x")["words"][0]["start"] == 0.333
