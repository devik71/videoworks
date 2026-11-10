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
    SYNC_RATE,
    coarse_offset,
    duck_curve,
    load,
    refine,
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
