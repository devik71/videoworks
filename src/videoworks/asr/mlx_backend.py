"""ASR на mlx-whisper: той самий Whisper, але на графічному чипі Apple Silicon.

faster-whisper (CTranslate2) на Mac рахує лише процесором: CUDA там немає, а
Metal CTranslate2 не вміє. Для 40-хвилинного заняття це різниця між кількома
хвилинами й годиною, тому на Mac є окремий бекенд.

Модель та сама — large-v3, лише сконвертована під MLX. Мовлення так само
вирізається Silero VAD до розпізнавання: без VAD Whisper вигадує текст на
музиці й тиші, а на заняттях аеробіки музика грає весь час. VAD береться з
faster-whisper, який стоїть і на Mac, — щоб відсікання мовлення було тим самим,
що й в основного бекенда, і транскрипти двох бекендів можна було порівнювати.

**Перевірено лише тестами з підміненим mlx_whisper**: на Windows MLX не
ставиться. Перший справжній прогін — на Mac, за планом у AGENTS.md.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import numpy as np

from videoworks.models import BackendInfo, Segment, Transcript, Word

SAMPLE_RATE = 16_000
DEFAULT_MODEL = "large-v3"

# Короткі назви, як у faster-whisper, → репозиторії сконвертованих моделей.
# Більшість лежить за шаблоном `mlx-community/whisper-<назва>-mlx`; винятки —
# тут. Повну назву репозиторію (з «/») можна передати й напряму.
REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def supported() -> bool:
    """MLX існує лише на Mac з Apple Silicon."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def repo_for(model: str) -> str:
    if "/" in model:
        return model
    return REPOS.get(model, f"mlx-community/whisper-{model}-mlx")


def speech_chunks(audio: np.ndarray) -> list[dict]:
    """Ділянки мовлення в семплах — тим самим VAD і з тими самими порогами."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    return get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500))


def restore(segments: list[dict], chunks: list[dict]) -> list[dict]:
    """Переносить час зі склеєного мовлення назад у час запису.

    Розпізнається лише мовлення, склеєне без пауз, тож таймкоди моделі — у
    координатах цієї склейки. Слово привʼязується до ділянки за своєю
    серединою, і початок, і кінець переносяться з одним зсувом: інакше слово
    на стику двох ділянок розтягнулось би на всю паузу між ними. Те саме
    правило, що в faster-whisper (`restore_speech_timestamps`).
    """
    from faster_whisper.vad import SpeechTimestampsMap

    ts_map = SpeechTimestampsMap(chunks, SAMPLE_RATE, time_precision=3)
    restored = []
    for segment in segments:
        words = []
        for word in segment.get("words") or []:
            index = ts_map.get_chunk_index((word["start"] + word["end"]) / 2)
            words.append(
                {
                    **word,
                    "start": ts_map.get_original_time(word["start"], index),
                    "end": ts_map.get_original_time(word["end"], index),
                }
            )
        if words:
            start, end = words[0]["start"], words[-1]["end"]
        else:
            start = ts_map.get_original_time(segment["start"])
            end = ts_map.get_original_time(segment["end"], is_end=True)
        restored.append({**segment, "start": start, "end": end, "words": words})
    return restored


def to_segments(segments: list[dict]) -> list[Segment]:
    """Відповідь mlx-whisper (як у openai-whisper) → наші Segment і Word."""
    result = []
    for index, segment in enumerate(segments):
        words = [
            Word(
                w=word["word"].strip(),
                start=round(word["start"], 3),
                end=round(word["end"], 3),
                prob=round(word["probability"], 4)
                if word.get("probability") is not None
                else None,
            )
            for word in segment.get("words") or []
            # Whisper зрідка віддає слово з end < start — воно зламає remap.
            if word["end"] >= word["start"]
        ]
        result.append(
            Segment(
                id=index,
                start=round(segment["start"], 3),
                end=round(segment["end"], 3),
                text=segment["text"].strip(),
                words=words,
            )
        )
    return result


class MlxBackend:
    name = "mlx"

    def __init__(self, model: str = DEFAULT_MODEL, device: str | None = None) -> None:
        self.model_name = model
        self.repo = repo_for(model)
        # Пристрій MLX обирає сам — це завжди графічний чип; поле лишаємо для
        # однакового рядка в CLI та квитанції.
        self.device = device or "apple-gpu"

    def prepare(self) -> None:
        """Завантажує ваги заздалегідь, щоб не міряти їх разом із роботою.

        Саме в той кеш і з тим dtype (float16), що бере `transcribe`: окремий
        `load_model` завантажив би модель удруге.
        """
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.get_model(self.repo, mx.float16)

    def transcribe(
        self,
        audio: Path,
        *,
        language: str | None = None,
        source: str | None = None,
        on_segment=None,
    ) -> Transcript:
        from faster_whisper.audio import decode_audio

        samples = decode_audio(str(audio), sampling_rate=SAMPLE_RATE)
        duration = len(samples) / SAMPLE_RATE
        chunks = speech_chunks(samples)

        detected = language
        segments: list[dict] = []
        if chunks:
            import mlx_whisper

            speech = np.concatenate([samples[c["start"] : c["end"]] for c in chunks])
            result = mlx_whisper.transcribe(
                speech,
                path_or_hf_repo=self.repo,
                language=language,
                word_timestamps=True,
            )
            detected = result.get("language") or language
            segments = restore(result.get("segments") or [], chunks)

        collected = to_segments(segments)
        if on_segment is not None:
            for segment in collected:
                on_segment(segment)

        return Transcript(
            source=source or str(audio),
            duration=round(duration, 3),
            language=detected or "",
            backend=BackendInfo(
                name=self.name,
                model=self.repo,
                version=_version(),
                device=self.device,
            ),
            segments=collected,
        )


def _version() -> str | None:
    try:
        from importlib.metadata import version

        return version("mlx-whisper")
    except Exception:  # noqa: BLE001 — версія лише для квитанції
        return None
