"""Розкладка папки проєкту.

Один проєкт — одна папка. Усі проміжні артефакти лежать поруч із вихідним відео,
щоб агент міг їх прочитати без жодної бази даних.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIRNAME = "projects"
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def subs(self) -> Path:
        return self.root / "subs"

    @property
    def audio(self) -> Path:
        return self.media / "audio.wav"

    @property
    def probe(self) -> Path:
        return self.root / "probe.json"

    @property
    def transcript_raw(self) -> Path:
        return self.root / "transcript.raw.json"

    @property
    def transcript_cut(self) -> Path:
        return self.root / "transcript.cut.json"

    @property
    def edl(self) -> Path:
        return self.root / "edl.json"

    @property
    def cut_video(self) -> Path:
        return self.out / "cut.mp4"

    @property
    def scenes(self) -> Path:
        return self.root / "scenes.json"

    @property
    def glossary(self) -> Path:
        return self.root / "glossary.tsv"

    @property
    def frames(self) -> Path:
        return self.out / "frames"

    @property
    def notes(self) -> Path:
        return self.root / "project.md"

    def create(self) -> Project:
        for directory in (self.media, self.out, self.subs):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.notes.exists():
            self.notes.write_text(
                f"# {self.root.name}\n\nПамʼять сесії: що вже зроблено, що вирішено.\n",
                encoding="utf-8",
            )
        return self

    def source_video(self) -> Path:
        """Єдине відео в media/. Кидає, якщо їх нуль або більше одного."""
        found = sorted(p for p in self.media.glob("*") if p.suffix.lower() in VIDEO_SUFFIXES)
        if not found:
            raise FileNotFoundError(f"У {self.media} немає відео. Поклади файл туди.")
        if len(found) > 1:
            names = ", ".join(p.name for p in found)
            raise ValueError(f"У {self.media} кілька відео ({names}) — вкажи потрібне явно.")
        return found[0]


def resolve(name_or_path: str, base: Path | None = None) -> Project:
    """Приймає і назву проєкту, і шлях до його папки."""
    candidate = Path(name_or_path)
    if candidate.is_absolute() or candidate.exists():
        return Project(candidate.resolve())
    root = base or Path.cwd()
    return Project((root / PROJECTS_DIRNAME / name_or_path).resolve())
