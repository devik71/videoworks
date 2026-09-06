"""Інтерфейс ASR-бекенда.

Бекендів буде щонайменше три (faster-whisper, whisperX, MOSS), вибір робимо
після бенчмарку на українському матеріалі. Тому все, що вище цього шару, знає
лише про Transcript і нічого про конкретну модель.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from videoworks.models import Transcript


@runtime_checkable
class ASRBackend(Protocol):
    name: str

    def transcribe(
        self,
        audio: Path,
        *,
        language: str | None = None,
        source: str | None = None,
    ) -> Transcript:
        """Аудіо → транскрипт зі словами й таймкодами."""
        ...
