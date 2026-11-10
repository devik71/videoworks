import os
from itertools import pairwise

import pytest

from videoworks.models import ManualSpan, Strikes, StruckWord
from videoworks.paths import Project
from videoworks.strikes import spans, store

W = StruckWord


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
