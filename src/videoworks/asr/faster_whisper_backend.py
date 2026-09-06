"""ASR на faster-whisper (CTranslate2). Дефолтний бекенд."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import videoworks  # noqa: F401  — підключає CUDA-DLL до імпорту ctranslate2
from videoworks.models import BackendInfo, Segment, Transcript, Word

DEFAULT_MODEL = "large-v3"


def pick_device() -> tuple[str, str]:
    """(device, compute_type). float16 на GPU, int8 на CPU."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except (ImportError, OSError, RuntimeError):
        # Немає CUDA-рантайму або драйвера — це не помилка, просто працюємо на CPU.
        pass
    return "cpu", "int8"


class FasterWhisperBackend:
    name = "faster-whisper"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        auto_device, auto_compute = pick_device()
        self.model_name = model
        self.device = device or auto_device
        self.compute_type = compute_type or (auto_compute if device is None else "default")
        self._model = None

    def prepare(self) -> None:
        """Завантажує модель заздалегідь, щоб не міряти її разом із роботою."""
        self._load()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(
        self,
        audio: Path,
        *,
        language: str | None = None,
        source: str | None = None,
        on_segment=None,
    ) -> Transcript:
        model = self._load()
        segments, info = model.transcribe(
            str(audio),
            language=language,
            word_timestamps=True,
            # Без VAD Whisper вигадує текст на музиці й тиші — це не опція.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        collected = list(self._collect(segments, on_segment))
        import faster_whisper

        return Transcript(
            source=source or str(audio),
            duration=float(info.duration),
            language=info.language,
            backend=BackendInfo(
                name=self.name,
                model=self.model_name,
                version=getattr(faster_whisper, "__version__", None),
                device=self.device,
                compute_type=self.compute_type,
            ),
            segments=collected,
        )

    @staticmethod
    def _collect(segments, on_segment) -> Iterator[Segment]:
        for index, segment in enumerate(segments):
            words = [
                Word(
                    w=word.word.strip(),
                    start=round(word.start, 3),
                    end=round(word.end, 3),
                    prob=round(word.probability, 4) if word.probability is not None else None,
                )
                for word in (segment.words or [])
                # Whisper зрідка віддає слово з end < start — воно зламає remap.
                if word.end >= word.start
            ]
            result = Segment(
                id=index,
                start=round(segment.start, 3),
                end=round(segment.end, 3),
                text=segment.text.strip(),
                words=words,
            )
            if on_segment is not None:
                on_segment(result)
            yield result
