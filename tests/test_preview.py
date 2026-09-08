from __future__ import annotations

import json

import pytest

from videoworks.editor import analyze_cues
from videoworks.preview import page, parse_range, payload
from videoworks.subtitles import Cue


def cue(text: str, start: float = 0.0, end: float = 3.0) -> Cue:
    return Cue(start=start, end=end, lines=[text])


# --------------------------------------------------------------------------- #
# Діапазони байтів
# --------------------------------------------------------------------------- #


def test_range_absent_means_whole_file() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("", 100) is None


def test_range_explicit_bounds() -> None:
    assert parse_range("bytes=10-20", 100) == (10, 20)


def test_range_open_end_runs_to_last_byte() -> None:
    assert parse_range("bytes=90-", 100) == (90, 99)


def test_range_suffix_takes_tail() -> None:
    assert parse_range("bytes=-10", 100) == (90, 99)


def test_range_suffix_longer_than_file_clamps_to_start() -> None:
    assert parse_range("bytes=-500", 100) == (0, 99)


def test_range_end_past_file_is_clamped() -> None:
    # Плеєр часто просить із запасом — це не помилка, віддаємо скільки є.
    assert parse_range("bytes=50-9999", 100) == (50, 99)


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=200-300", "bytes=30-10", "bytes=-0"])
def test_range_outside_file_raises(header: str) -> None:
    with pytest.raises(ValueError):
        parse_range(header, 100)


@pytest.mark.parametrize("header", ["items=0-10", "bytes=0-10,20-30", "bytes=abc"])
def test_range_unsupported_falls_back_to_whole_file(header: str) -> None:
    assert parse_range(header, 100) is None


# --------------------------------------------------------------------------- #
# Дані сторінки
# --------------------------------------------------------------------------- #


def test_payload_is_json_serialisable() -> None:
    data = payload(analyze_cues([cue("Привіт."), cue("")]))
    assert json.loads(json.dumps(data, ensure_ascii=False))["cues"][0]["lines"] == ["Привіт."]


def test_payload_counts_levels() -> None:
    fast = cue("Репліка, яку ніхто не встигне прочитати за секунду", 0.0, 1.0)
    data = payload(analyze_cues([cue(""), fast, cue("Норм.")]))
    assert (data["errors"], data["warnings"]) == (1, 1)
    assert [c["level"] for c in data["cues"]] == ["error", "warning", "ok"]


def test_payload_replaces_infinite_speed_with_none() -> None:
    # json.dumps віддав би Infinity, на якому JSON.parse у браузері падає.
    data = payload(analyze_cues([cue("Текст", 5.0, 5.0)]))
    assert data["cues"][0]["cps"] is None
    assert "Infinity" not in json.dumps(data)


# --------------------------------------------------------------------------- #
# Сторінка
# --------------------------------------------------------------------------- #


def test_page_carries_title_and_endpoints() -> None:
    html = page("smoke · uk.srt")
    assert "smoke · uk.srt" in html
    assert "/cues.json" in html
    assert 'src="/video"' in html


def test_page_keeps_javascript_intact() -> None:
    """Перенос усередині літерала JS — SyntaxError, і сторінка порожня."""
    script = page("t").partition("<script>")[2].partition("</script>")[0]
    for number, line in enumerate(script.splitlines(), 1):
        assert line.count('"') % 2 == 0, f"розірваний літерал у рядку {number}"
        assert line.count("`") % 2 == 0, f"розірваний шаблон у рядку {number}"


def test_page_escapes_title() -> None:
    # Заголовок — це ім'я папки проєкту, і воно потрапляє і в <title>, і в <h1>.
    assert "&lt;b&gt;назва&lt;/b&gt;" in page("<b>назва</b>")
