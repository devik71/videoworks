"""Реєстр ASR-бекендів.

Важкі залежності (torch, transformers) тягнуться лише тим, кому вони потрібні,
тому наявність бекенда перевіряється, а не припускається.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec

from videoworks.asr.base import ASRBackend

BACKENDS = ("faster-whisper", "whisperx", "moss", "mlx")

_REQUIRES = {
    "faster-whisper": "faster_whisper",
    "whisperx": "whisperx",
    # Саме сам пакет MOSS, а не transformers: transformers прилітає як
    # залежність інших речей, і перевірка по ньому давала б хибне «є».
    "moss": "moss_transcribe_diarize",
    "mlx": "mlx_whisper",
}


def available(name: str) -> bool:
    module = _REQUIRES.get(name)
    return bool(module) and find_spec(module) is not None


def availability() -> dict[str, bool]:
    return {name: available(name) for name in BACKENDS}


def default() -> str:
    """Бекенд, коли його не назвали явно.

    На Mac з Apple Silicon і встановленим mlx-whisper — він: faster-whisper там
    рахує лише процесором, у рази повільніше. Скрізь інде — faster-whisper.
    """
    from videoworks.asr.mlx_backend import supported

    return "mlx" if supported() and available("mlx") else "faster-whisper"


def build(name: str, model: str | None = None, device: str | None = None, **kwargs) -> ASRBackend:
    if name not in BACKENDS:
        raise ValueError(f"невідомий бекенд {name!r}; є: {', '.join(BACKENDS)}")
    if not available(name):
        raise RuntimeError(f"{name} не встановлено: uv sync --extra {name.replace('-', '')}")

    factories: dict[str, Callable[[], ASRBackend]] = {
        "faster-whisper": lambda: _faster_whisper(model, device, **kwargs),
        "whisperx": lambda: _whisperx(model, device, **kwargs),
        "moss": lambda: _moss(model, device, **kwargs),
        "mlx": lambda: _mlx(model, device, **kwargs),
    }
    return factories[name]()


def _mlx(model: str | None, device: str | None, **kwargs) -> ASRBackend:
    from videoworks.asr.mlx_backend import DEFAULT_MODEL, MlxBackend

    kwargs.pop("diarize", None)  # цей бекенд діаризації не вміє
    kwargs.pop("language", None)  # мова передається в transcribe()
    return MlxBackend(model=model or DEFAULT_MODEL, device=device, **kwargs)


def _faster_whisper(model: str | None, device: str | None, **kwargs) -> ASRBackend:
    from videoworks.asr.faster_whisper_backend import DEFAULT_MODEL, FasterWhisperBackend

    kwargs.pop("diarize", None)  # цей бекенд діаризації не вміє
    kwargs.pop("language", None)  # мова передається в transcribe()
    return FasterWhisperBackend(model=model or DEFAULT_MODEL, device=device, **kwargs)


def _whisperx(model: str | None, device: str | None, **kwargs) -> ASRBackend:
    from videoworks.asr.whisperx_backend import DEFAULT_MODEL, WhisperXBackend

    return WhisperXBackend(model=model or DEFAULT_MODEL, device=device, **kwargs)


def _moss(model: str | None, device: str | None, **kwargs) -> ASRBackend:
    from videoworks.asr.moss_backend import DEFAULT_MODEL, MossBackend

    kwargs.pop("diarize", None)  # діаризація тут вбудована, вимкнути не можна
    kwargs.pop("language", None)  # мова передається в transcribe()
    return MossBackend(model=model or DEFAULT_MODEL, device=device, **kwargs)
