from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from videoworks import audio
from videoworks.diagnostics import errors
from videoworks.edl import from_spans, preflight
from videoworks.models import AudioSettings, Compressor, Edl, Limiter
from videoworks.render import build_filtergraph

EBUR128_SUMMARY = """
[Parsed_ebur128_0 @ 000001] t: 4.9  TARGET:-23 LUFS    M: -21.0 S: -22.4     I: -24.0 LUFS
[Parsed_ebur128_0 @ 000001] Summary:

  Integrated loudness:
    I:         -23.4 LUFS
    Threshold: -33.8 LUFS

  Loudness range:
    LRA:         6.3 LU
    Threshold: -43.9 LUFS
    LRA low:   -27.0 LUFS
    LRA high:  -20.7 LUFS

  True peak:
    Peak:       -4.0 dBFS
"""


def test_chain_runs_stages_in_order() -> None:
    graph = ";".join(audio.chain(AudioSettings(), 3.5, 7.25))
    stages = ["volume=3.50dB", "acompressor", "acrossover=split=5000", "volume=7.25dB", "alimiter"]
    positions = [graph.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert graph.startswith("[ca]aresample=48000")
    assert graph.endswith("anull[outa]")


def test_chain_thresholds_are_linear() -> None:
    graph = ";".join(audio.chain(AudioSettings(compressor=Compressor(threshold_db=-20.0)), 0, 0))
    assert "threshold=0.100000" in graph


def test_limiter_is_oversampled_and_does_not_autolevel() -> None:
    graph = ";".join(audio.chain(AudioSettings(limiter=Limiter(ceiling_db=-1.0)), 0, 0))
    # Без level=0 alimiter сам підтягує гучність до стелі й ламає мейкап.
    assert "alimiter=limit=0.891251" in graph
    assert ":level=0" in graph
    assert "aresample=192000,alimiter" in graph


def test_dynamics_only_stops_before_makeup() -> None:
    graph = ";".join(audio.chain(AudioSettings(), 2.0, 9.0, dynamics_only=True))
    assert "9.00dB" not in graph
    assert "alimiter" not in graph
    assert "acrossover" in graph


def test_disabled_stages_drop_out() -> None:
    settings = AudioSettings(compressor=None, deesser=None, limiter=None)
    graph = ";".join(audio.chain(settings, 1.0, 2.0))
    assert "acompressor" not in graph
    assert "acrossover" not in graph
    assert "alimiter" not in graph
    assert "volume=1.00dB" in graph and "volume=2.00dB" in graph


def test_filtergraph_without_plan_passes_audio_through() -> None:
    edl = from_spans("raw.mp4", [(0.0, 5.0)])
    assert "[ca]anull[outa]" in build_filtergraph(edl)
    assert "loudnorm" not in build_filtergraph(edl)


def test_audio_graph_selects_segments_in_one_branch() -> None:
    edl = from_spans("raw.mp4", [(1.0, 2.0), (3.0, 4.5), (6.0, 7.0)])
    graph = audio.audio_graph(edl, ["[ca]anull[meter]"])
    assert graph.count("[0:a]") == 1
    assert "atrim" not in graph
    assert r"between(t\,1.000000\,2.000000)" in graph
    assert r"between(t\,6.000000\,7.000000)" in graph
    # Дерево, а не плаский ланцюжок: інакше ffmpeg не розбирає сотні термів.
    assert "(between" in graph


def test_parse_ebur128_takes_summary_not_running_values() -> None:
    loudness = audio.parse_ebur128("лог ffmpeg\n" + EBUR128_SUMMARY)
    assert loudness == audio.Loudness(integrated=-23.4, true_peak=-4.0, lra=6.3)


def test_parse_ebur128_treats_gate_floor_as_silence() -> None:
    text = "    I:         -70.0 LUFS\n    LRA:         0.0 LU\n    Peak:       -inf dBFS\n"
    loudness = audio.parse_ebur128(text)
    assert loudness.integrated is None
    assert loudness.true_peak is None
    assert loudness.silent


def test_plan_targets_stage_and_output_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([audio.Loudness(-31.0, -9.0, 5.0), audio.Loudness(-24.5, -6.0, 3.0)])
    monkeypatch.setattr(audio, "measure", lambda *args, **kwargs: next(readings))
    plan = audio.plan(Path("raw.mp4"), from_spans("raw.mp4", [(0.0, 5.0)]))
    assert plan.stage_gain_db == pytest.approx(11.0)  # -31 → -20
    assert plan.makeup_db == pytest.approx(8.5)  # -24.5 → -16


def test_plan_does_not_boost_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "measure", lambda *a, **k: audio.Loudness(None, None, 0.0))
    plan = audio.plan(Path("raw.mp4"), from_spans("raw.mp4", [(0.0, 5.0)]))
    assert plan.stage_gain_db == 0.0
    assert plan.makeup_db == 0.0
    assert plan.notes


def test_plan_warns_on_huge_stage_gain(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([audio.Loudness(-50.0, -30.0, 5.0), audio.Loudness(-24.0, -6.0, 3.0)])
    monkeypatch.setattr(audio, "measure", lambda *a, **k: next(readings))
    plan = audio.plan(Path("raw.mp4"), from_spans("raw.mp4", [(0.0, 5.0)]))
    assert any("шум" in note for note in plan.notes)


def test_old_edl_without_chain_gets_default_chain() -> None:
    edl = Edl.model_validate_json(
        '{"source": "raw.mp4", "segments": [], "audio": {"normalize": "ebur128", "target_lufs": -14}}'
    )
    assert edl.audio.compressor and edl.audio.deesser and edl.audio.limiter
    assert edl.audio.stage_lufs == -20.0


@pytest.mark.parametrize(
    ("settings", "needle"),
    [
        (AudioSettings(normalize="peak"), "normalize"),
        (AudioSettings(stage_lufs=-3.0), "stage_lufs"),
        (AudioSettings(compressor=Compressor(ratio=0.5)), "compressor.ratio"),
        (AudioSettings(limiter=Limiter(ceiling_db=0.5)), "limiter.ceiling_db"),
    ],
)
def test_preflight_rejects_broken_chain(settings: AudioSettings, needle: str) -> None:
    plan = from_spans("raw.mp4", [(0.0, 5.0)], settings)
    assert any(needle in p.message for p in errors(preflight(plan)))


def test_preflight_ignores_chain_when_processing_is_off() -> None:
    settings = AudioSettings(normalize=None, compressor=Compressor(ratio=0.5))
    assert not preflight(from_spans("raw.mp4", [(0.0, 5.0)], settings))


# --------------------------------------------------------------------------- #
# Живий ffmpeg: два однакові сигнали різної гучності мають вийти однаковими.


def _speechlike(path: Path, gain_db: float) -> Path:
    # Рожевий шум, промодульований складами ~4 Гц, і шиплячі сплески — грубо, але
    # з динамікою й верхами, на яких є що робити і компресору, і де-есеру.
    source = (
        "anoisesrc=color=pink:duration=12:sample_rate=44100:seed=7,"
        "volume='0.15+0.6*abs(sin(2*PI*2*t))':eval=frame,"
        f"volume={gain_db}dB"
    )
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="без ffmpeg ланцюг не прогнати")
def test_chain_evens_out_loud_and_quiet_material(tmp_path: Path) -> None:
    edl = from_spans("x", [(0.5, 5.0), (6.0, 11.5)])
    results = []
    for name, gain in (("loud", -4.0), ("quiet", -22.0)):
        clip = _speechlike(tmp_path / f"{name}.wav", gain)
        plan = audio.plan(clip, edl)
        out = audio.measure(clip, edl, plan.graph(dest="[pre]"))
        results.append((plan, out))

    (loud_plan, loud), (quiet_plan, quiet) = results
    assert quiet_plan.stage_gain_db - loud_plan.stage_gain_db == pytest.approx(18.0, abs=0.3)
    for out in (loud, quiet):
        assert out.integrated == pytest.approx(-16.0, abs=1.0)
        assert out.true_peak <= -1.5 + 0.3
    assert loud.integrated == pytest.approx(quiet.integrated, abs=0.3)
