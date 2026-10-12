from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from videoworks.burn import mux_soft

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="без ffmpeg нема чого муксити"
)


def _streams(path) -> list[str]:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [s["codec_type"] for s in json.loads(done.stdout)["streams"]]


def test_mux_soft_survives_data_streams(tmp_path) -> None:
    """Телефон пише в MP4 data-потоки, яких Matroska не приймає.

    Таймкод-трек MP4 — такий самий data-потік, і з ним `-map 0` валив мукс
    ще на заголовку, лишаючи порожній контейнер на кількасот байтів.
    """
    video = tmp_path / "phone.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x36:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-timecode",
            "00:00:00:00",
            str(video),
        ],
        check=True,
    )
    assert "data" in _streams(video), "стенд мав би дати data-потік"

    subs = tmp_path / "uk.srt"
    subs.write_text("1\n00:00:00,000 --> 00:00:00,800\nПривіт\n", "utf-8")

    dest = mux_soft(video, subs, tmp_path / "out.mkv")
    assert sorted(_streams(dest)) == ["audio", "subtitle", "video"]
