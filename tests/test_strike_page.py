from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import videoworks
from videoworks.strike_server import STATIC

WEB = Path(videoworks.__file__).parent / "web" / "strike"
TESTS = Path(__file__).parent
SCRIPTS = ["app.js", "logic.js", "brush.js", "player.js", "sheet.js", "sound.js"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="немає node")


def run_node(*args: str) -> None:
    done = subprocess.run(
        ["node", *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@needs_node
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses(name):
    # Порожні заглушки теж мають проходити: пізніші завдання наповнюють їх по черзі.
    run_node("--check", str(WEB / name))


@needs_node
def test_logic_behaves():
    run_node("--test", str(TESTS / "strike_logic.test.mjs"))


ELEMENTS = ("sheet", "video", "map", "bar", "count", "saved", "banner", "mode", "review", "help")


def test_page_declares_the_elements_the_script_needs():
    html = (WEB / "index.html").read_text("utf-8")
    for element in ELEMENTS:
        assert f'id="{element}"' in html


def test_page_loads_scripts_as_modules_from_whitelisted_routes():
    # Сервер віддає лише маршрути зі STATIC: усе інше, на що послалась би сторінка,
    # у браузері обернеться на 404 і мовчазно зламаний аркуш.
    html = (WEB / "index.html").read_text("utf-8")
    linked = re.findall(r'(?:src|href)="([^"]*)"', html)
    assert linked, "сторінка мусить підключати стиль і скрипт"
    for route in linked:
        # data: — це вміст у самій сторінці, запиту до сервера він не робить.
        assert route in STATIC or route.startswith("data:"), route
    tags = re.findall(r"<script\b[^>]*>", html)
    assert tags
    for tag in tags:
        assert 'type="module"' in tag and "src=" in tag, tag


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_import_only_whitelisted_modules(name):
    source = (WEB / name).read_text("utf-8")
    for target in re.findall(r'\bfrom\s+"([^"]+)"', source):
        assert target.startswith("./"), target
        assert target[1:] in STATIC, target


def test_page_makes_no_requests_outside_the_server():
    # Сторінка працює без мережі: жодних шрифтів, скриптів чи картинок іззовні.
    for name in ("index.html", "style.css", *SCRIPTS):
        text = (WEB / name).read_text("utf-8")
        for address in re.findall(r"""(?:https?:)?//[\w.-]+\.\w+[^\s"')]*""", text):
            # Простори назв SVG — це імена, а не адреси: браузер їх не завантажує.
            assert address.startswith("http://www.w3.org/"), f"{name}: {address}"


def test_every_colour_goes_through_tokens():
    css = (WEB / "style.css").read_text("utf-8")
    assert ":root" in css
    rest = re.sub(r":root\s*\{[^}]*\}", "", css)
    colours = r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl|hwb|lab|lch|oklab|oklch)a?\("
    found = re.findall(colours, rest)
    assert not found, f"колір поза :root: {found}"
    for token in ("--paper", "--ink", "--brush", "--shadow"):
        assert f"{token}:" in css


def test_page_respects_reduced_motion():
    assert "prefers-reduced-motion" in (WEB / "style.css").read_text("utf-8")
    # Наростання мазка й бризки робить скрипт, тож і він мусить питати про рух.
    assert "prefers-reduced-motion" in (WEB / "brush.js").read_text("utf-8")


def test_page_text_is_ukrainian_and_has_no_placeholder_copy():
    html = (WEB / "index.html").read_text("utf-8")
    assert '<html lang="uk">' in html
    for name in ("index.html", "style.css", *SCRIPTS):
        text = (WEB / name).read_text("utf-8")
        for junk in ("Lorem", "TODO", "FIXME"):
            assert junk not in text, f"{name}: {junk}"
