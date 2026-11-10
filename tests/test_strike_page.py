from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import videoworks

WEB = Path(videoworks.__file__).parent / "web" / "strike"
TESTS = Path(__file__).parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="немає node")


def run_node(*args: str) -> None:
    done = subprocess.run(
        ["node", *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@needs_node
@pytest.mark.parametrize("name", ["app.js", "logic.js", "brush.js", "sound.js"])
def test_script_parses(name):
    # Порожні заглушки теж мають проходити: пізніші завдання наповнюють їх по черзі.
    run_node("--check", str(WEB / name))


@needs_node
def test_logic_behaves():
    run_node("--test", str(TESTS / "strike_logic.test.mjs"))
