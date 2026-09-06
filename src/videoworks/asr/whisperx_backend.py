"""ASR на whisperX: faster-whisper + forced alignment + діаризація.

Для української тут є штатна фонемна модель —
`Yehor/wav2vec2-xls-r-300m-uk-with-small-lm` у `DEFAULT_ALIGN_MODELS_HF`,
тож вирівнювання не деградує до рівня сегмента, як це буває з мовами без моделі.

Ваги pyannote для діаризації закриті: потрібен токен HuggingFace і згода з
умовами на сторінці моделі. Без токена працює все, крім розмітки спікерів.
"""

from __future__ import annotations

import os
from pathlib import Path

import videoworks  # noqa: F401  — підключає CUDA-DLL до імпорту ctranslate2
from videoworks.models import BackendInfo, Segment, Transcript, Word

DEFAULT_MODEL = "large-v3"


def available() -> bool:
    from importlib.util import find_spec

    return find_spec("whisperx") is not None


class WhisperXBackend:
    name = "whisperx"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str | None = None,
        compute_type: str | None = None,
        *,
        diarize: bool = False,
        hf_token: str | None = None,
        language: str | None = None,
    ) -> None:
        from videoworks.asr.faster_whisper_backend import pick_device

        auto_device, auto_compute = pick_device()
        self.model_name = model
        self.device = device or auto_device
        self.compute_type = compute_type or auto_compute
        self.diarize = diarize
        self.hf_token = hf_token or os.environ.get("HF_TOKEN")
        self.language = language
        self._model = None
        self._aligners: dict[str, tuple] = {}

    def prepare(self) -> None:
        """Підвантажує ваги заздалегідь — інакше вони потрапляють у замір часу."""
        self._load_model()
        if self.language:
            self._load_aligner(self.language)

    def _load_model(self):
        if self._model is None:
            import whisperx

            # Мову даємо вже при завантаженні: інакше whisperX визначає її
            # для кожного файла окремо й марно витрачає час.
            self._model = whisperx.load_model(
                self.model_name,
                self.device,
                compute_type=self.compute_type,
                language=self.language,
            )
        return self._model

    def _load_aligner(self, language: str) -> tuple:
        if language not in self._aligners:
            import whisperx

            self._aligners[language] = whisperx.load_align_model(
                language_code=language, device=self.device
            )
        return self._aligners[language]

    def transcribe(
        self,
        audio: Path,
        *,
        language: str | None = None,
        source: str | None = None,
    ) -> Transcript:
        if not available():
            raise RuntimeError("whisperx не встановлено: uv sync --extra whisperx")

        import whisperx

        model = self._load_model()
        loaded = whisperx.load_audio(str(audio))
        result = model.transcribe(loaded, language=language or self.language)
        detected = result.get("language", language or self.language or "")

        align_model, metadata = self._load_aligner(detected)
        result = whisperx.align(
            result["segments"], align_model, metadata, loaded, self.device
        )

        speakers: dict[int, str] = {}
        if self.diarize:
            if not self.hf_token:
                raise RuntimeError(
                    "Діаризація потребує HF_TOKEN і згоди з умовами pyannote "
                    "на сторінці моделі."
                )
            pipeline = whisperx.diarize.DiarizationPipeline(
                use_auth_token=self.hf_token, device=self.device
            )
            result = whisperx.assign_word_speakers(pipeline(loaded), result)

        return Transcript(
            source=source or str(audio),
            duration=round(len(loaded) / 16_000, 3),
            language=detected,
            backend=BackendInfo(
                name=self.name,
                model=self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            ),
            segments=self._segments(result, speakers),
        )

    @staticmethod
    def _segments(result: dict, _speakers: dict) -> list[Segment]:
        segments: list[Segment] = []
        for index, segment in enumerate(result.get("segments", [])):
            words = [
                Word(
                    w=str(word["word"]).strip(),
                    start=round(float(word["start"]), 3),
                    end=round(float(word["end"]), 3),
                    prob=round(float(word["score"]), 4) if "score" in word else None,
                )
                for word in segment.get("words", [])
                # Вирівнювання зрідка не знаходить слово в аудіо й лишає його
                # без таймкодів — такі слова зламали б перемапування.
                if word.get("start") is not None and word.get("end") is not None
            ]
            if not words:
                continue
            segments.append(
                Segment(
                    id=index,
                    start=words[0].start,
                    end=words[-1].end,
                    text=str(segment.get("text", "")).strip(),
                    speaker=segment.get("speaker"),
                    words=words,
                )
            )
        return segments
