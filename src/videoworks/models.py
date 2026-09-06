"""Контракти даних. Ці схеми — інтерфейс між ASR, агентом і рендером.

Ламати їх дорого: на них зав'язані і transcript.json на диску, і EDL, який пише
агент. Розширювати можна, перейменовувати поля — ні.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator


class Word(BaseModel):
    """Одне слово з таймкодами. Основна одиниця, по якій ріже агент."""

    w: str
    start: float
    end: float
    prob: float | None = None

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"слово {self.w!r}: end {self.end} < start {self.start}")
        return self


class Speaker(BaseModel):
    id: str
    label: str | None = None


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)


class BackendInfo(BaseModel):
    name: str
    model: str
    version: str | None = None
    device: str | None = None
    compute_type: str | None = None


class Transcript(BaseModel):
    source: str
    duration: float
    language: str
    backend: BackendInfo
    speakers: list[Speaker] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)

    def words(self) -> list[Word]:
        return [w for segment in self.segments for w in segment.words]

    @property
    def has_word_timings(self) -> bool:
        """Не всі бекенди дають рівень слова — MOSS, наприклад, лише репліки."""
        return any(segment.words for segment in self.segments)

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments).strip()

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> Transcript:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class Scene(BaseModel):
    id: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class Scenes(BaseModel):
    """Межі склейок. Візуальна сітка на додачу до текстової.

    Агент звіряється з нею, щоб не ставити шов посеред плану.
    """

    source: str
    detector: str
    scenes: list[Scene] = Field(default_factory=list)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Scenes:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class EdlSegment(BaseModel):
    """Шматок вихідного відео у фіналі.

    ``src_in``/``src_out`` — координати в оригіналі, ``dst_out`` — де цей шматок
    починається у змонтованому файлі. Саме ця трійка дозволяє перемапити
    таймкоди транскрипту після різки, не запускаючи ASR удруге.
    """

    src_in: float
    src_out: float
    dst_out: float

    @property
    def duration(self) -> float:
        return self.src_out - self.src_in

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.src_out <= self.src_in:
            raise ValueError(f"порожній сегмент: src_in={self.src_in} src_out={self.src_out}")
        return self


class AudioSettings(BaseModel):
    normalize: str | None = "ebur128"
    target_lufs: float = -16.0


class Edl(BaseModel):
    """План монтажу. Агент пише саме це, а не команди ffmpeg."""

    source: str
    segments: list[EdlSegment] = Field(default_factory=list)
    audio: AudioSettings = Field(default_factory=AudioSettings)

    @property
    def duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Edl:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class Receipt(BaseModel):
    """Провенанс вихідного файлу: з чого й чим зроблено."""

    output: str
    inputs: dict[str, str] = Field(default_factory=dict)  # шлях -> sha256
    edl: dict | None = None
    tools: dict[str, str] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    created_at: str

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), ensure_ascii=False, indent=2), "utf-8")
        return path
