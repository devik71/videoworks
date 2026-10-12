"""Рендер EDL у відео + receipt на кожен вихідний файл."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from videoworks import __version__, audio
from videoworks.audio import AudioPlan
from videoworks.edl import PreflightFailed, errors, preflight
from videoworks.ingest import _binary, _run, ffmpeg_version
from videoworks.models import Edl, Receipt


def build_filtergraph(
    edl: Edl, *, with_audio: bool = True, audio_plan: AudioPlan | None = None
) -> str:
    """trim/atrim по кожному інтервалу + concat, далі ланцюг обробки звуку.

    Саме trim, а не seek: сегменти ріжуться покадрово від декодованого потоку,
    тож межі точні й не з'їжджають до найближчого ключового кадру. Без
    ``audio_plan`` звук іде як є.
    """
    parts: list[str] = []
    labels: list[str] = []

    for index, segment in enumerate(edl.segments):
        window = f"start={segment.src_in:.6f}:end={segment.src_out:.6f}"
        parts.append(f"[0:v]trim={window},setpts=PTS-STARTPTS[v{index}]")
        labels.append(f"[v{index}]")
        if with_audio:
            parts.append(f"[0:a]atrim={window},asetpts=PTS-STARTPTS[a{index}]")
            labels.append(f"[a{index}]")

    count = len(edl.segments)
    if with_audio:
        order = "".join(f"[v{i}][a{i}]" for i in range(count))
        parts.append(f"{order}concat=n={count}:v=1:a=1[cv][ca]")
        parts.append("[cv]null[outv]")
        if audio_plan:
            parts.extend(audio_plan.graph("[ca]", "[outa]"))
        else:
            parts.append("[ca]anull[outa]")
    else:
        order = "".join(f"[v{i}]" for i in range(count))
        parts.append(f"{order}concat=n={count}:v=1:a=0[outv]")

    return ";\n".join(parts)


def cut(
    video: Path,
    edl: Edl,
    dest: Path,
    *,
    with_audio: bool = True,
    encoder: str = "libx264",
    quality: int = 18,
    preset: str | None = None,
    source_duration: float | None = None,
) -> tuple[Path, Receipt]:
    """Рендерить EDL. Кидає PreflightFailed, якщо план не пройшов перевірку."""
    problems = preflight(edl, source_duration=source_duration)
    if errors(problems):
        raise PreflightFailed(problems)

    audio_plan = audio.plan(video, edl) if with_audio and edl.audio.normalize else None

    dest.parent.mkdir(parents=True, exist_ok=True)
    graph_path = dest.with_suffix(".filtergraph.txt")
    graph = build_filtergraph(edl, with_audio=with_audio, audio_plan=audio_plan)
    graph_path.write_text(graph, encoding="utf-8")

    args = [
        _binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(video.resolve()),
        # Граф із сотень сегментів не влазить у командний рядок Windows,
        # тому передаємо його файлом. Стара опція -filter_complex_script
        # прибрана у ffmpeg 9, робоча форма саме така.
        "-/filter_complex", str(graph_path.resolve()),
        "-map", "[outv]",
    ]
    if with_audio:
        args += ["-map", "[outa]"]
    if encoder == "nvenc":
        args += ["-c:v", "h264_nvenc", "-preset", preset or "p5", "-cq", str(quality)]
    else:
        args += ["-c:v", encoder, "-preset", preset or "medium", "-crf", str(quality)]
    args += ["-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", str(audio.SAMPLE_RATE)]
    args.append(str(dest.resolve()))

    _run(args)

    loudness = None
    if audio_plan:
        loudness = audio_plan.as_dict() | {"output": asdict(audio.measure_file(dest))}

    receipt = Receipt(
        output=str(dest),
        inputs={str(video): sha256(video)},
        edl=edl.model_dump(),
        audio=loudness,
        tools={
            "videoworks": __version__,
            "ffmpeg": ffmpeg_version(),
        },
        command=args,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return dest, receipt


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()
