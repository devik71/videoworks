from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
from scipy import signal

from videoworks.edl import from_spans
from videoworks.models import AudioSettings, BackendInfo, Segment, Transcript, Word
from videoworks.render import build_filtergraph, cut
from videoworks.soundtrack.trial import (
    STRIKE_RAMP,
    SYNC_RATE,
    coarse_offset,
    duck_curve,
    gains,
    load,
    local_spans,
    no_voice_message,
    quiet_windows,
    refine,
    run_trial,
    strike_curve,
    uncovered_strikes,
    voice_spans,
    write_wav,
)


def test_sync_recovers_partial_track_with_room_echo_and_clock_drift():
    rate = SYNC_RATE
    rng = np.random.default_rng(71)
    track = rng.normal(0, 0.1, 75 * rate)
    # Неперіодичний ритм і зміни гучності; запис почався посеред треку.
    envelope = np.repeat(rng.uniform(0.1, 1, 150), rate // 2)
    track *= envelope
    true_offset, true_rate = -5.037, 1.00003
    coordinates = (np.arange(60 * rate) / rate - true_offset) / true_rate * rate
    room = np.interp(coordinates, np.arange(len(track)), track)
    room = signal.lfilter([1, 0.2], [1, -0.3], room)
    room += 0.2 * np.roll(room, round(0.027 * rate))
    room += rng.normal(0, 0.002, len(room))
    offset, peak, _ = coarse_offset(room, track)
    assert abs(offset - true_offset) < 0.03
    assert peak > 0.1
    refined, speed, windows = refine(room, track, offset)
    assert abs(refined - true_offset) < 0.001
    assert abs(speed - true_rate) < 5e-6
    assert sum(w["used"] for w in windows) >= 4


def test_unrelated_audio_does_not_pass_coarse_threshold():
    rng = np.random.default_rng(13)
    _, correlation, _ = coarse_offset(
        rng.normal(size=30 * SYNC_RATE), rng.normal(size=40 * SYNC_RATE)
    )
    assert correlation < 0.08


def test_duck_is_ready_before_first_word_and_stays_down_between_nearby_phrases():
    curve = duck_curve(5000, 1000, [(1.0, 1.5), (2.0, 2.5)])
    assert curve[720] == pytest.approx(0)
    assert curve[800] == pytest.approx(1)
    assert np.min(curve[800:3100]) == pytest.approx(1)
    assert curve[3700] == pytest.approx(0)


def _transcript(words):
    return Transcript(
        source="test.wav",
        duration=10,
        language="uk",
        backend=BackendInfo(name="test", model="test"),
        segments=[Segment(id=0, start=0, end=10, text="", words=words)],
    )


def test_lyrics_need_both_text_and_time_match_to_be_removed():
    room = _transcript(
        [
            Word(w="Love!", start=1, end=1.3),
            Word(w="Вгору", start=2, end=2.3),
            Word(w="love", start=5, end=5.3),
        ]
    )
    lyrics = _transcript([Word(w="love", start=1.1, end=1.4), Word(w="music", start=2, end=2.3)])
    spans, dropped = voice_spans(room, lyrics)
    assert dropped == 1
    assert spans == [(2, 2.3), (5, 5.3)]


def test_override_graph_keeps_video_coordinates_but_uses_local_audio():
    plan = from_spans("raw.mp4", [(10, 11)], AudioSettings(normalize=None))
    graph = build_filtergraph(plan, audio_input=1, audio_origin=10)
    assert "[0:v]trim=start=10.000000:end=11.000000" in graph
    assert "[1:a]atrim=start=0.000000:end=1.000000" in graph
    assert "[0:a]" not in graph


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg відсутній")
def test_render_uses_replacement_audio_and_hashes_both_inputs(tmp_path):
    source, replacement = tmp_path / "source.mp4", tmp_path / "replacement.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000",
            "-t",
            "12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    t = np.arange(48000) / 48000
    write_wav(replacement, (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 48000)
    plan = from_spans(str(source), [(10, 11)], AudioSettings(normalize=None))
    dest, receipt = cut(
        source,
        plan,
        tmp_path / "out.mp4",
        preset="ultrafast",
        audio_override=replacement,
        audio_origin=10,
    )
    samples = load(dest, 8000)
    peak = np.argmax(np.abs(np.fft.rfft(samples))) * 8000 / len(samples)
    assert peak == pytest.approx(220, abs=2)
    assert set(receipt.inputs) == {str(source), str(replacement)}
    assert receipt.audio["origin"] == 10
    too_long = from_spans(str(source), [(10, 12)], AudioSettings(normalize=None))
    with pytest.raises(ValueError, match="не покриває"):
        cut(source, too_long, tmp_path / "invalid.mp4", audio_override=replacement, audio_origin=10)
    assert not (tmp_path / "invalid.mp4").exists()


def test_strike_curve_is_full_inside_and_ramps_stay_within_the_span():
    curve = strike_curve(48000 * 3, 48000, [(1.0, 2.0)])
    assert curve.dtype == np.float32
    assert curve[int(0.999 * 48000)] == 0 and curve[int(2.001 * 48000)] == 0
    assert curve[int(1.5 * 48000)] == 1
    assert 0 < curve[int(1.02 * 48000)] < 1
    assert curve[int(1.045 * 48000)] == pytest.approx(1, abs=1e-3)
    assert STRIKE_RAMP == 0.04


def test_very_short_strike_still_reaches_silence():
    assert strike_curve(48000, 48000, [(0.5, 0.56)]).max() > 0.99


def test_overlapping_strikes_combine_by_maximum_and_clip_to_the_signal():
    curve = strike_curve(1000, 1000, [(0.1, 0.5), (0.3, 0.9), (0.95, 5.0)])
    assert len(curve) == 1000 and curve[400] == 1 and curve[200] == 1


def test_gains_without_strikes_match_the_current_mix():
    duck = duck_curve(48000 * 4, 48000, [(1.0, 2.0)])
    covered = np.ones(len(duck), bool)
    room, music = gains(duck, covered, np.zeros(len(duck), np.float32))
    np.testing.assert_array_equal(music, 10 ** ((-12 * duck) / 20))
    np.testing.assert_array_equal(room, 10 ** ((-24 * (1 - duck)) / 20))


def test_gains_without_strikes_keep_the_uncovered_rule_and_do_not_mutate_inputs():
    duck = duck_curve(4000, 1000, [(1.0, 1.5)])
    covered = np.arange(4000) < 2500
    before = duck.copy(), covered.copy()
    room, music = gains(duck, covered, np.zeros(4000, np.float32))
    assert (room[~covered] == 1).all() and (music[~covered] == 0).all()
    np.testing.assert_array_equal(duck, before[0])
    np.testing.assert_array_equal(covered, before[1])


def test_struck_stretch_is_clean_music_and_no_room_even_under_a_duck():
    duck = np.ones(48000, np.float32)
    covered = np.ones(48000, bool)
    room, music = gains(duck, covered, np.ones(48000, np.float32))
    assert room.max() == 0 and music.min() == pytest.approx(1)


def test_struck_stretch_without_a_track_is_silence():
    room, music = gains(np.zeros(100, np.float32), np.zeros(100, bool), np.ones(100, np.float32))
    assert room.max() == 0 and music.max() == 0


def test_local_spans_shift_and_clip_to_the_window():
    spans = [(50, 61), (119, 130), (300, 301)]
    assert local_spans(spans, start=60, seconds=60) == [(0.0, 1.0), (59.0, 60.0)]


def test_struck_words_do_not_duck_the_music():
    room = _transcript([Word(w="слово", start=10, end=10.4), Word(w="інше", start=20, end=20.4)])
    lyrics = _transcript([])
    spans, _ = voice_spans(room, lyrics, struck=[(9.8, 10.6)])
    assert spans == [(20, 20.4)]


def test_broken_strikes_file_stops_the_trial_before_sync(tmp_path, monkeypatch):
    class Project:
        strikes = tmp_path / "strikes.json"

    Project.strikes.write_text("{не json", encoding="utf-8")

    def sync_must_not_run(*args, **kwargs):
        raise AssertionError("sync_trial не мав викликатися")

    monkeypatch.setattr("videoworks.soundtrack.trial.sync_trial", sync_must_not_run)
    with pytest.raises(ValueError, match="strikes.json"):
        run_trial(Project())


def test_uncovered_strikes_are_merged_window_local_intervals():
    covered = np.ones(10000, bool)
    covered[2000:3000] = False
    covered[3000:3500] = False
    covered[9000:] = False
    got = uncovered_strikes(covered, 1000, [(1.0, 4.0), (8.0, 10.0)])
    assert got == [[2.0, 3.5], [9.0, 10.0]]
    assert uncovered_strikes(np.ones(100, bool), 100, [(0.0, 1.0)]) == []


def test_quiet_windows_skip_struck_stretches_and_match_the_old_rule_without_strikes():
    rate = 1000
    duck = duck_curve(8000, rate, [(1.0, 1.5)])
    covered = np.ones(8000, bool)
    covered[5000:6000] = False
    zero = np.zeros(8000, np.float32)
    old = [
        slice(i, i + rate)
        for i in range(0, 8000 - rate, rate)
        if np.max(duck[i : i + rate]) < 0.01 and np.all(covered[i : i + rate])
    ]
    assert quiet_windows(duck, covered, zero, rate) == old
    strike = strike_curve(8000, rate, [(3.2, 3.6)])
    got = quiet_windows(duck, covered, strike, rate)
    assert slice(3000, 4000) in old and slice(3000, 4000) not in got
    assert got == [w for w in old if w != slice(3000, 4000)]


def test_no_voice_says_when_strikes_removed_all_of_it():
    assert "викреслен" not in no_voice_message([])
    told = no_voice_message([(1.0, 2.0), (5.0, 6.5)])
    assert told.startswith("Голос не знайдено")
    assert "викреслен" in told and "2" in told
