from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from videoworks import ingest, proxy, strike_server
from videoworks.cli import app
from videoworks.models import BackendInfo, Segment, Transcript, Word

SMOKE = Path(__file__).resolve().parents[1] / "projects" / "smoke" / "media" / "raw.mp4"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not SMOKE.exists(),
    reason="без ffmpeg і smoke-відео копію не зібрати",
)


def test_command_keeps_the_timing_and_drops_phone_data_streams(tmp_path):
    args = proxy.command(tmp_path / "in.mov", tmp_path / "out.mp4")
    joined = " ".join(args)
    assert "+faststart" in args
    assert "scale=-2:960" in joined
    assert "-map 0:v:0" in joined
    assert "-map 0:a:0" in joined
    assert "libx264" in args
    assert args[-1] == str(tmp_path / "out.mp4")


def test_stale_says_whether_the_copy_must_be_rebuilt(tmp_path):
    video, target = tmp_path / "raw.mp4", tmp_path / "proxy.mp4"
    video.write_bytes(b"v")
    assert proxy.stale(video, target)
    target.write_bytes(b"p")
    os.utime(video, (1_000, 1_000))
    os.utime(target, (2_000, 2_000))
    assert not proxy.stale(video, target)
    os.utime(video, (3_000, 3_000))
    assert proxy.stale(video, target)


@needs_ffmpeg
def test_build_keeps_duration_and_starts_both_streams_at_zero(tmp_path):
    video = tmp_path / "raw.mp4"
    shutil.copyfile(SMOKE, video)
    built = proxy.build(video, tmp_path / "out" / "proxy.mp4")
    original, copy = ingest.probe(video), ingest.probe(built)
    assert abs(ingest.duration_of(copy) - ingest.duration_of(original)) <= 0.05
    kinds = {s["codec_type"]: s for s in copy["streams"]}
    assert set(kinds) == {"video", "audio"}
    assert all(float(s["start_time"]) == 0 for s in copy["streams"])
    assert kinds["video"]["codec_name"] == "h264"
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["proxy.mp4"]  # без тимчасових


class Stub:
    def __init__(self):
        self.kwargs: dict = {}

    server_port = 8772

    def build(self, **kwargs):
        self.kwargs = kwargs
        return self

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


@needs_ffmpeg
def test_strike_proxy_builds_once_reuses_and_rebuilds_when_the_original_is_newer(
    tmp_path, monkeypatch
):
    root = tmp_path / "demo"
    (root / "media").mkdir(parents=True)
    shutil.copyfile(SMOKE, root / "media" / "raw.mp4")
    Transcript(
        source="s",
        duration=15.2,
        language="uk",
        backend=BackendInfo(name="t", model="m"),
        segments=[
            Segment(id=0, start=1.0, end=1.3, text="раз", words=[Word(w="раз", start=1.0, end=1.3)])
        ],
    ).save(root / "transcript.raw.json")
    stub = Stub()
    monkeypatch.setattr(strike_server, "build_server", stub.build)
    target = root / "out" / "strike-proxy.mp4"

    def strike():
        result = CliRunner().invoke(app, ["strike", str(root), "--no-open", "--proxy"])
        assert result.exit_code == 0, result.output
        assert stub.kwargs["video"] == target
        return result

    strike()
    assert target.exists()
    # Час у минулому: свіжозібрана копія має бути новішою за обидві позначки.
    os.utime(target, (1_500_000_000, 1_500_000_000))  # копія новіша за оригінал
    os.utime(root / "media" / "raw.mp4", (1_000_000_000, 1_000_000_000))
    strike()
    assert target.stat().st_mtime == 1_500_000_000  # не перезбирали

    os.utime(root / "media" / "raw.mp4", (1_600_000_000, 1_600_000_000))
    strike()
    assert target.stat().st_mtime > 1_600_000_000  # оригінал новіший — перезібрано
    assert not proxy.stale(root / "media" / "raw.mp4", target)
