"""MOSS-Transcribe-Diarize: транскрибація і діаризація одним проходом.

**Цей бекенд не дає таймкодів на рівні слова.** Ні `TranscriptSegment`, ні
`SubtitleSegment` у моделі MOSS не мають поля зі словами — усе на рівні репліки.
Для монтажу, де різка йде по межах слів, цього замало, тому основним бекендом
MOSS бути не може.

Його цінність в іншому: діаризація без закритих ваг. pyannote вимагає токена
HuggingFace і згоди з умовами, MOSS під Apache-2.0 — ні. Тому робочий зв'язок —
faster-whisper для слів плюс MOSS для спікерів, зшиті через
`videoworks.diarize.assign_speakers`.
"""

from __future__ import annotations

from pathlib import Path

from videoworks.models import BackendInfo, Segment, Transcript

DEFAULT_MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"


def available() -> bool:
    from importlib.util import find_spec

    return find_spec("moss_transcribe_diarize") is not None


class MossBackend:
    name = "moss"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str | None = None,
        max_new_tokens: int = 2048,
    ) -> None:
        self.model_name = model
        self.device = device
        self.max_new_tokens = max_new_tokens

    def transcribe(
        self,
        audio: Path,
        *,
        language: str | None = None,
        source: str | None = None,
    ) -> Transcript:
        if not available():
            raise RuntimeError("MOSS не встановлено: uv sync --extra moss")

        import torch
        from moss_transcribe_diarize import parse_transcript
        from moss_transcribe_diarize.inference_utils import (
            build_transcription_messages,
            generate_transcription,
            resolve_device,
        )
        from transformers import AutoModelForCausalLM, AutoProcessor

        device = resolve_device(self.device or "auto")
        dtype = torch.bfloat16 if device.type == "cuda" else torch.float32

        model = (
            AutoModelForCausalLM.from_pretrained(
                self.model_name,
                trust_remote_code=True,
                dtype="auto",
                # eager росте квадратично за довжиною аудіо і падає з OOM
                # на довгих записах — sdpa тут не оптимізація, а необхідність.
                attn_implementation="sdpa",
            )
            .to(dtype=dtype)
            .to(device)
            .eval()
        )
        processor = AutoProcessor.from_pretrained(self.model_name, trust_remote_code=True)

        result = generate_transcription(
            model,
            processor,
            build_transcription_messages(str(audio)),
            max_new_tokens=self.max_new_tokens,
            do_sample=False,
            device=device,
            dtype=dtype,
        )

        parsed = parse_transcript(result["text"])
        segments = [
            Segment(
                id=index,
                start=round(float(item.start), 3),
                end=round(float(item.end), 3),
                text=item.text.strip(),
                speaker=item.speaker,
                words=[],  # рівня слова тут немає — див. модуль-docstring
            )
            for index, item in enumerate(parsed)
        ]

        return Transcript(
            source=source or str(audio),
            duration=round(segments[-1].end, 3) if segments else 0.0,
            language=language or "",
            backend=BackendInfo(
                name=self.name,
                model=self.model_name,
                device=str(device),
            ),
            segments=segments,
        )
