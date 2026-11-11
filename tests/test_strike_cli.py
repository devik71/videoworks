from __future__ import annotations

import json
import webbrowser

import pytest
from typer.testing import CliRunner

from videoworks import strike_server, strikes
from videoworks.cli import app
from videoworks.models import BackendInfo, Segment, Strikes, Transcript, Word

runner = CliRunner()

WORDS = [("раз", 1.0, 1.3), ("два", 1.4, 1.7), ("три", 1.8, 2.1)]


def make_transcript(words=WORDS) -> Transcript:
    segment = Segment(
        id=0,
        start=words[0][1] if words else 0.0,
        end=words[-1][2] if words else 0.0,
        text=" ".join(w for w, _, _ in words),
        words=[Word(w=w, start=a, end=b) for w, a, b in words],
    )
    return Transcript(
        source="s",
        duration=60.0,
        language="uk",
        backend=BackendInfo(name="t", model="m"),
        segments=[segment],
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "demo"
    (root / "media").mkdir(parents=True)
    (root / "media" / "raw.mp4").write_bytes(b"\x00\x01\x02")
    make_transcript().save(root / "transcript.raw.json")
    return root


class Stub:
    """Заміна build_server: збирає, що їй дали, і одразу «натискає Ctrl+C»."""

    def __init__(self):
        self.kwargs: dict = {}
        self.closed = False

    def build(self, **kwargs):
        self.kwargs = kwargs
        return self

    server_port = 8772

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


@pytest.fixture
def stub(monkeypatch):
    fake = Stub()
    monkeypatch.setattr(strike_server, "build_server", fake.build)
    return fake


@pytest.fixture
def browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    return opened


def run(project, *args):
    return runner.invoke(app, ["strike", str(project), *args])


def test_strike_refuses_without_a_transcript(project, stub):
    (project / "transcript.raw.json").unlink()
    result = run(project, "--no-open")
    assert result.exit_code == 1
    assert "transcribe" in result.output
    assert stub.kwargs == {}


def test_strike_refuses_a_transcript_without_word_timings(project, stub):
    bare = make_transcript()
    bare.segments[0].words = []
    bare.save(project / "transcript.raw.json")
    result = run(project, "--no-open")
    assert result.exit_code == 1
    assert "transcribe" in result.output


def test_strike_refuses_a_broken_strikes_file_and_leaves_it_alone(project, stub):
    path = project / "strikes.json"
    path.write_bytes(b'{"source": "x", broken')
    result = run(project, "--no-open")
    assert result.exit_code == 1
    assert "strikes.json" in result.output
    assert path.read_bytes() == b'{"source": "x", broken'
    assert stub.kwargs == {}


def old_strikes(project, words, sha="stale") -> None:
    Strikes(
        source="media/raw.mp4",
        transcript_sha256=sha,
        words=[{"w": w, "start": a, "end": b} for w, a, b in words],
    ).save(project / "strikes.json")


def test_strike_announces_unglued_words_and_saves_the_rematch(project, stub):
    old_strikes(project, [("зникло", 5.0, 5.4), ("два", 1.4, 1.7)])
    result = run(project, "--no-open")
    assert result.exit_code == 0
    assert "1 слово не знайшлись у новому транскрипті" in " ".join(result.output.split())
    saved = Strikes.model_validate_json((project / "strikes.json").read_text(encoding="utf-8"))
    assert saved.transcript_sha256 == strikes.sha256(project / "transcript.raw.json")
    assert [w.w for w in saved.words] == ["два"]
    assert [(m.start, m.end) for m in saved.manual] == [(5.0, 5.4)]
    assert saved.spans
    first, second = stub.kwargs["load"](), stub.kwargs["load"]()
    assert first["unglued"] == 1
    assert second["unglued"] == 0
    assert second["struck"] == [1]


def test_strike_rematches_silently_when_nothing_is_unglued(project, stub):
    old_strikes(project, [("два", 1.4, 1.7)])
    result = run(project, "--no-open")
    assert result.exit_code == 0
    assert "не знайшл" not in result.output
    saved = json.loads((project / "strikes.json").read_text(encoding="utf-8"))
    assert saved["transcript_sha256"] == strikes.sha256(project / "transcript.raw.json")
    assert stub.kwargs["load"]()["unglued"] == 0


def test_strike_prints_the_summary_on_exit(project, stub):
    old_strikes(project, [("два", 1.4, 1.7)], sha=strikes.sha256(project / "transcript.raw.json"))
    result = run(project, "--no-open")
    assert result.exit_code == 0
    assert "Викреслено: 1 слово" in result.output
    assert "0 без слів" in result.output
    assert "strikes.json" in result.output
    assert stub.closed


def test_strike_says_nothing_was_struck_when_there_is_no_file(project, stub):
    result = run(project, "--no-open")
    assert "Нічого не викреслено" in result.output
    assert not (project / "strikes.json").exists()


def test_strike_hands_the_server_working_callables(project, stub):
    result = run(project, "--no-open", "--port", "0")
    assert result.exit_code == 0
    kwargs = stub.kwargs
    assert kwargs["port"] == 0
    assert kwargs["video"].name == "raw.mp4"

    state = kwargs["load"]()
    assert state["title"] == "demo"
    assert state["struck"] == []

    before = kwargs["stamp"]()
    assert before == ""
    done = kwargs["store"]([0, 2], [(10.0, 11.0)])
    assert done["struck"] == [0, 2]
    assert done["manual"] == [[10.0, 11.0]]
    assert done["spans"] and done["counts"]["words"] == 2 and done["counts"]["manual"] == 1

    saved = Strikes.model_validate_json((project / "strikes.json").read_text(encoding="utf-8"))
    assert [w.w for w in saved.words] == ["раз", "три"]
    assert saved.source == "media/raw.mp4"
    assert [list(s) for s in saved.spans] == done["spans"]
    assert kwargs["stamp"]() == strikes.sha256(project / "strikes.json") != before


def test_strike_load_sees_a_change_made_on_disk(project, stub):
    run(project, "--no-open")
    load = stub.kwargs["load"]
    assert load()["struck"] == []
    old_strikes(project, [("три", 1.8, 2.1)], sha=strikes.sha256(project / "transcript.raw.json"))
    assert load()["struck"] == [2]


def test_strike_open_flag_controls_the_browser(project, stub, browser):
    run(project, "--no-open")
    assert browser == []
    run(project)
    assert browser == ["http://127.0.0.1:8772/"]


def test_strike_reports_a_busy_port(project, monkeypatch):
    def busy(**kwargs):
        raise OSError("зайнято")

    monkeypatch.setattr(strike_server, "build_server", busy)
    result = run(project, "--no-open")
    assert result.exit_code != 0
    assert "зайнятий" in result.output


def test_strike_complains_about_a_missing_video(project, stub):
    (project / "media" / "raw.mp4").unlink()
    result = run(project, "--no-open")
    assert result.exit_code == 1
    assert stub.kwargs == {}
