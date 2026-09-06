"""Детекція склейок через PySceneDetect."""

from __future__ import annotations

from pathlib import Path

from videoworks.models import Scene, Scenes


def detect_scenes(
    video: Path,
    *,
    threshold: float = 27.0,
    min_scene_len: int = 15,
    adaptive: bool = False,
    duration: float | None = None,
) -> Scenes:
    """Межі планів у секундах.

    `adaptive` вмикає двопрохідний детектор: він краще тримається на русі
    камери, де звичайний ContentDetector сипле хибними склейками.

    PySceneDetect на відео без склейок повертає порожній список — формально це
    «немає переходів», але читається як помилка. Знаючи тривалість, віддаємо
    натомість одну суцільну сцену: так це і є насправді.
    """
    from scenedetect import AdaptiveDetector, ContentDetector, detect

    detector = (
        AdaptiveDetector(min_scene_len=min_scene_len)
        if adaptive
        else ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
    )
    found = detect(str(video), detector)

    scenes = [
        Scene(id=index, start=round(start.get_seconds(), 3), end=round(end.get_seconds(), 3))
        for index, (start, end) in enumerate(found)
    ]
    if not scenes and duration:
        scenes = [Scene(id=0, start=0.0, end=round(duration, 3))]

    return Scenes(
        source=str(video),
        detector="adaptive" if adaptive else f"content:{threshold}",
        scenes=scenes,
    )


def nearest_boundary(time: float, scenes: Scenes, *, window: float = 0.5) -> float | None:
    """Найближча склейка в межах вікна — щоб не різати посеред плану."""
    boundaries = [scene.start for scene in scenes.scenes] + [
        scene.end for scene in scenes.scenes
    ]
    candidates = [b for b in boundaries if abs(b - time) <= window]
    return min(candidates, key=lambda b: abs(b - time)) if candidates else None
