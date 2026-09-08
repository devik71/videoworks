from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from videoworks.editor import analyze_cues
from videoworks.preview import build_server, page, parse_range, payload
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


# --------------------------------------------------------------------------- #
# Живий сервер
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(tmp_path):
    """Піднімає справжній сервер на вільному порті."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 8)  # 2048 байтів

    running = build_server(
        video=video,
        load=lambda: payload(analyze_cues([cue("Привіт, світе.", 0.0, 3.0)])),
        title="тест",
        port=0,
    )
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{running.server_port}"
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)


def test_server_serves_page(server: str) -> None:
    with urllib.request.urlopen(f"{server}/") as response:
        body = response.read().decode("utf-8")
    assert response.headers["Content-Type"].startswith("text/html")
    assert "тест" in body


def test_server_serves_cues(server: str) -> None:
    with urllib.request.urlopen(f"{server}/cues.json") as response:
        data = json.loads(response.read())
    assert data["cues"][0]["lines"] == ["Привіт, світе."]


def test_server_answers_range_with_partial_content(server: str) -> None:
    request = urllib.request.Request(f"{server}/video", headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(request) as response:
        body = response.read()
    assert response.status == 206
    assert response.headers["Content-Range"] == "bytes 100-199/2048"
    assert body == (bytes(range(256)) * 8)[100:200]


def test_server_reports_size_without_range(server: str) -> None:
    with urllib.request.urlopen(f"{server}/video") as response:
        assert response.headers["Content-Length"] == "2048"
        assert response.headers["Accept-Ranges"] == "bytes"


def test_server_rejects_range_past_the_file(server: str) -> None:
    request = urllib.request.Request(f"{server}/video", headers={"Range": "bytes=9000-9100"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 416


def test_server_has_no_other_routes(server: str) -> None:
    # Роздачі каталогу немає — отже, і виходу за межі проєкту теж.
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{server}/../../etc/passwd")
    assert caught.value.code == 404


def test_dropped_connection_is_not_reported(tmp_path, capsys) -> None:
    """Обрив зʼєднання глушимо, справжню помилку — ні."""
    running = build_server(video=tmp_path, load=dict, title="t", port=0)
    try:
        try:
            raise ConnectionAbortedError(10053, "aborted")
        except ConnectionAbortedError:
            running.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""

        try:
            raise ValueError("справжня біда")
        except ValueError:
            running.handle_error(None, ("127.0.0.1", 1))
        assert "справжня біда" in capsys.readouterr().err
    finally:
        running.server_close()
