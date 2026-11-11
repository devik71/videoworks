"""Бекенд mlx без самого MLX: на Windows його не поставити.

Справжній `mlx_whisper` підміняється заглушкою з тією самою формою відповіді,
що в mlx-whisper 0.4.3 (як в openai-whisper). Перевіряється все, крім самої
моделі: VAD, склейка мовлення, перенесення часу назад, перетворення у
Transcript, вибір бекенда.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from videoworks.asr import mlx_backend, registry
from videoworks.asr.mlx_backend import MlxBackend, repo_for, restore, to_segments

RATE = mlx_backend.SAMPLE_RATE


def word(w: str, start: float, end: float, prob: float | None = 0.9) -> dict:
    return {"word": f" {w}", "start": start, "end": end, "probability": prob}


# --------------------------------------------------------------------------- #
# Моделі
# --------------------------------------------------------------------------- #


def test_short_names_map_to_mlx_community_repos():
    assert repo_for("large-v3") == "mlx-community/whisper-large-v3-mlx"
    assert repo_for("small") == "mlx-community/whisper-small-mlx"
    # Turbo лежить не за шаблоном — без винятку повели б у неіснуючий репозиторій.
    assert repo_for("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"


def test_full_repo_name_passes_through():
    assert repo_for("someone/whisper-uk-mlx") == "someone/whisper-uk-mlx"


# --------------------------------------------------------------------------- #
# Перенесення часу зі склейки мовлення в час запису
# --------------------------------------------------------------------------- #

# Мовлення 1–3 с і 10–12 с запису; у склейці вони стоять поруч: 0–2 і 2–4 с.
CHUNKS = [{"start": 1 * RATE, "end": 3 * RATE}, {"start": 10 * RATE, "end": 12 * RATE}]


def test_words_return_to_their_place_in_the_recording():
    segments = [
        {"start": 0.0, "end": 3.5, "text": "раз два", "words": [
            word("раз", 0.2, 0.6),
            word("два", 2.5, 3.0),
        ]},
    ]
    (got,) = restore(segments, CHUNKS)
    assert [(w["start"], w["end"]) for w in got["words"]] == [(1.2, 1.6), (10.5, 11.0)]
    assert (got["start"], got["end"]) == (1.2, 11.0)


def test_word_on_the_seam_keeps_one_offset_and_does_not_span_the_pause():
    # Середина 1.95 с — ще перша ділянка: обидва краї з одним зсувом +1 с,
    # а не початок із першої ділянки, а кінець з другої (+8 с паузи).
    segments = [{"start": 1.8, "end": 2.1, "text": "шов", "words": [word("шов", 1.8, 2.1)]}]
    (got,) = restore(segments, CHUNKS)
    assert (got["words"][0]["start"], got["words"][0]["end"]) == (2.8, 3.1)


def test_segment_without_words_is_moved_too():
    (got,) = restore([{"start": 2.5, "end": 3.0, "text": "…", "words": []}], CHUNKS)
    assert (got["start"], got["end"]) == (10.5, 11.0)


# --------------------------------------------------------------------------- #
# Відповідь моделі → Transcript
# --------------------------------------------------------------------------- #


def test_to_segments_strips_words_and_drops_reversed_ones():
    (segment,) = to_segments([
        {"start": 1.0, "end": 2.0, "text": " Так. ", "words": [
            word("Так.", 1.0, 1.4),
            word("зле", 1.6, 1.5),  # end < start ламає remap
            word("ні", 1.6, 1.9, None),
        ]},
    ])
    assert segment.text == "Так."
    assert [w.w for w in segment.words] == ["Так.", "ні"]
    assert segment.words[1].prob is None


# --------------------------------------------------------------------------- #
# Повний прохід із заглушкою mlx_whisper
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_mlx(monkeypatch):
    calls: list[dict] = []

    def transcribe(audio, **kwargs):
        calls.append({"audio": audio, **kwargs})
        return {
            "language": "uk",
            "segments": [
                {"start": 0.0, "end": 3.5, "text": " раз два", "words": [
                    word("раз", 0.2, 0.6),
                    word("два", 2.5, 3.0),
                ]},
            ],
        }

    module = types.ModuleType("mlx_whisper")
    module.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return calls


@pytest.fixture
def recording(monkeypatch, tmp_path):
    """15 с запису, мовлення на 1–3 і 10–12 с — VAD підмінено, щоб не залежати від ваг."""
    samples = np.arange(15 * RATE, dtype=np.float32)
    monkeypatch.setattr(
        "faster_whisper.audio.decode_audio", lambda path, sampling_rate: samples
    )
    monkeypatch.setattr(mlx_backend, "speech_chunks", lambda audio: CHUNKS)
    return tmp_path / "audio.wav"


def test_transcribe_sends_only_speech_and_restores_times(fake_mlx, recording):
    seen = []
    transcript = MlxBackend().transcribe(
        recording, language="uk", source="media/raw.mov", on_segment=seen.append
    )

    (call,) = fake_mlx
    # Модель отримує лише мовлення, склеєне без пауз: 2 с + 2 с.
    assert len(call["audio"]) == 4 * RATE
    assert call["audio"][0] == 1 * RATE and call["audio"][2 * RATE] == 10 * RATE
    assert call["word_timestamps"] is True
    assert call["language"] == "uk"
    assert call["path_or_hf_repo"] == "mlx-community/whisper-large-v3-mlx"

    assert [(w.w, w.start, w.end) for w in transcript.words()] == [
        ("раз", 1.2, 1.6),
        ("два", 10.5, 11.0),
    ]
    assert transcript.duration == 15.0
    assert transcript.language == "uk"
    assert transcript.source == "media/raw.mov"
    assert transcript.backend.name == "mlx"
    assert len(seen) == 1


def test_no_speech_means_no_model_call(fake_mlx, recording, monkeypatch):
    monkeypatch.setattr(mlx_backend, "speech_chunks", lambda audio: [])
    transcript = MlxBackend().transcribe(recording, language="uk")
    assert fake_mlx == []
    assert transcript.segments == []
    assert transcript.language == "uk"


# --------------------------------------------------------------------------- #
# Вибір бекенда
# --------------------------------------------------------------------------- #


def test_mlx_is_known_to_the_registry():
    assert "mlx" in registry.BACKENDS
    assert "mlx" in registry.availability()


@pytest.mark.parametrize(
    ("platform", "machine", "installed", "expected"),
    [
        ("darwin", "arm64", True, "mlx"),
        ("darwin", "arm64", False, "faster-whisper"),  # не поставили extra — не ламаємось
        ("darwin", "x86_64", True, "faster-whisper"),  # Intel-Mac: MLX там немає
        ("win32", "AMD64", True, "faster-whisper"),
    ],
)
def test_default_backend_follows_the_machine(monkeypatch, platform, machine, installed, expected):
    monkeypatch.setattr(mlx_backend.sys, "platform", platform)
    monkeypatch.setattr(mlx_backend.platform, "machine", lambda: machine)
    monkeypatch.setattr(registry, "available", lambda name: installed or name != "mlx")
    assert registry.default() == expected


def test_registry_builds_mlx_backend_without_importing_mlx(monkeypatch):
    monkeypatch.setattr(registry, "available", lambda name: True)
    backend = registry.build("mlx", model="large-v3", language="uk", diarize=False)
    assert isinstance(backend, MlxBackend)
    assert backend.repo == "mlx-community/whisper-large-v3-mlx"
