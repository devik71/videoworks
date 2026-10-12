from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from videoworks.studio import analysis, build_server, page, read_cues, rewrap
from videoworks.subtitles import Cue, CueOptions

OPTS = CueOptions()


def cue(text: str, start: float = 0.0, end: float = 3.0) -> Cue:
    return Cue(start=start, end=end, lines=text.split("\n"))


# --------------------------------------------------------------------------- #
# Розбір тіла запиту
# --------------------------------------------------------------------------- #


def test_read_cues_keeps_manual_line_breaks() -> None:
    cues = read_cues({"cues": [{"start": 0, "end": 2, "text": "Перший\nдругий"}]})
    assert cues[0].lines == ["Перший", "другий"]


def test_read_cues_accepts_numeric_strings() -> None:
    # Поля таймкодів на сторінці текстові, і "1.5" звідти цілком очікуване.
    assert read_cues({"cues": [{"start": "1.5", "end": "2", "text": "Так"}]})[0].start == 1.5


def test_read_cues_keeps_empty_cue_instead_of_dropping_it() -> None:
    # Викинута репліка зникла б і зі списку — разом із міткою про помилку.
    cues = read_cues({"cues": [{"start": 0, "end": 1, "text": ""}]})
    assert len(cues) == 1
    assert analysis(cues, OPTS)["errors"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"cues": "не список"},
        {"cues": ["не обʼєкт"]},
        {"cues": [{"start": 0, "end": 1}]},
        {"cues": [{"start": "пізніше", "end": 1, "text": "Так"}]},
        {"cues": [{"start": float("nan"), "end": 1, "text": "Так"}]},
        {"cues": [{"start": 0, "end": float("inf"), "text": "Так"}]},
    ],
)
def test_read_cues_refuses_broken_bodies(body: object) -> None:
    with pytest.raises(ValueError):
        read_cues(body)


def test_read_cues_refuses_infinity_so_the_page_can_parse_the_answer() -> None:
    """json.loads приймає Infinity, json.dumps віддає — а JSON.parse падає.

    Нескінченність, пропущена всередину, повертається сторінці в таймкоді,
    розбір відповіді валиться, і редактор мовчки застигає на старих даних.
    """
    with pytest.raises(ValueError):
        read_cues({"cues": [{"start": float("-inf"), "end": 1, "text": "Так"}]})


# --------------------------------------------------------------------------- #
# Дані сторінки
# --------------------------------------------------------------------------- #


def test_analysis_carries_limits_for_the_counters() -> None:
    data = analysis([cue("Привіт.")], CueOptions(max_line_chars=32, max_cps=12.0))
    assert data["limits"]["max_line_chars"] == 32
    assert data["limits"]["max_cps"] == 12.0


def test_analysis_is_json_serialisable() -> None:
    data = analysis([cue("Текст", 5.0, 5.0)], OPTS)
    assert "Infinity" not in json.dumps(data, ensure_ascii=False)


def test_rewrap_splits_a_long_cue_and_keeps_timing() -> None:
    long = cue("Це достатньо довга репліка, щоб вона не вмістилась у сорок два символи", 0.0, 6.0)
    [fixed] = rewrap([long], OPTS)
    assert len(fixed.lines) == 2
    assert all(len(line) <= OPTS.max_line_chars for line in fixed.lines)
    assert (fixed.start, fixed.end) == (0.0, 6.0)


def test_rewrap_drops_manual_breaks() -> None:
    [fixed] = rewrap([cue("Коротко\nі ще")], OPTS)
    assert fixed.lines == ["Коротко і ще"]


# --------------------------------------------------------------------------- #
# Сторінка
# --------------------------------------------------------------------------- #


def test_page_carries_title_and_endpoints() -> None:
    html = page("smoke · uk.srt")
    assert "smoke · uk.srt" in html
    for route in ("/cues.json", "/analyze", "/rewrap", "/save"):
        assert route in html
    assert 'src="/video"' in html


def test_page_keeps_javascript_intact() -> None:
    """Перенос усередині літерала JS — SyntaxError, і сторінка порожня."""
    script = page("t").partition("<script>")[2].partition("</script>")[0]
    for number, line in enumerate(script.splitlines(), 1):
        assert line.count('"') % 2 == 0, f"розірваний літерал у рядку {number}"
        assert line.count("`") % 2 == 0, f"розірваний шаблон у рядку {number}"


@pytest.mark.skipif(shutil.which("node") is None, reason="без node синтаксис не перевірити")
def test_page_javascript_parses(tmp_path) -> None:
    """Полічені лапки — здогадка; це справжній розбір тим самим двигуном.

    Сторінка приїжджає одним шматком усередині рядка Python, і будь-яка описка
    в ній не падає на тестах, а тихо лишає користувача з чорним екраном.
    """
    script = tmp_path / "page.js"
    script.write_text(page("t").partition("<script>")[2].partition("</script>")[0], "utf-8")
    done = subprocess.run(
        [shutil.which("node"), "--check", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="без node сторінку не виконати")
def test_page_timeline_maths_behaves(tmp_path) -> None:
    """Виконує скрипт сторінки й перевіряє арифметику стрічки.

    Розбір нічого не каже про те, чи репліка зберігає довжину, коли її тягнуть
    у сусіда, і чи прилипає край там, де треба. Стенд поруч виконує той самий
    скрипт поверх заглушки DOM — це єдиний спосіб побачити ці відповіді, не
    відкриваючи браузер.
    """
    script = tmp_path / "page.js"
    script.write_text(page("t").partition("<script>")[2].partition("</script>")[0], "utf-8")
    probe = Path(__file__).with_name("studio_page_probe.mjs")

    done = subprocess.run(
        [shutil.which("node"), str(probe), str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_page_carries_the_editing_controls() -> None:
    html = page("t")
    for control in ('id="magnet"', 'id="split"', 'id="rewrap"', 'id="magnetLine"'):
        assert control in html


def test_page_scales_ticks_and_labels_apart() -> None:
    """Підпису треба місце, щоб не злипнутись із сусіднім, рисці — ні.

    Один крок на обох дає або частокіл підписів, або шкалу без поділок.
    """
    script = page("t").partition("<script>")[2].partition("</script>")[0]
    assert "MIN_TICK_PX" in script and "MIN_LABEL_PX" in script
    tick = int(script.partition("const MIN_TICK_PX = ")[2].partition(";")[0])
    label = int(script.partition("const MIN_LABEL_PX = ")[2].partition(";")[0])
    assert label > tick


def test_page_defines_every_colour_through_tokens() -> None:
    """Колір, оголошений лише в media-блоці, не застосується в іншій темі."""
    style = page("t").partition("<style>")[2].partition("</style>")[0]
    light = style.partition("prefers-color-scheme: light")[2]
    for token in ("--ground", "--ink", "--blue", "--red"):
        assert style.count(token + ":") >= 3, f"{token} має бути в усіх трьох темах"
        assert token in light


def test_page_escapes_title() -> None:
    assert "&lt;b&gt;назва&lt;/b&gt;" in page("<b>назва</b>")


# --------------------------------------------------------------------------- #
# Живий сервер
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(tmp_path):
    """Піднімає справжній редактор на вільному порті; store пише у список."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 8)  # 2048 байтів
    written: list[list[Cue]] = []

    def store(cues: list[Cue], force: bool) -> dict:
        written.append(cues)
        return {"path": str(tmp_path / "uk.srt"), "stale": ["uk.ass"], "forced": force}

    running = build_server(
        video=video,
        load=lambda: [cue("Привіт, світе.", 0.0, 3.0)],
        store=store,
        opts=OPTS,
        title="тест",
        port=0,
    )
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{running.server_port}", written
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)


def call(base: str, route: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{base}{route}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def test_server_serves_page(server) -> None:
    base, _ = server
    with urllib.request.urlopen(f"{base}/") as response:
        body = response.read().decode("utf-8")
    assert response.headers["Content-Type"].startswith("text/html")
    assert "тест" in body


def test_server_serves_cues_with_limits(server) -> None:
    base, _ = server
    with urllib.request.urlopen(f"{base}/cues.json") as response:
        data = json.loads(response.read())
    assert data["cues"][0]["lines"] == ["Привіт, світе."]
    assert data["limits"]["max_lines"] == OPTS.max_lines


def test_analyze_returns_metrics_without_touching_disk(server) -> None:
    base, written = server
    data = call(
        base,
        "/analyze",
        {"cues": [{"start": 0, "end": 1, "text": "Задовга репліка на секунду часу"}]},
    )
    assert data["warnings"] == 1
    assert written == []


def test_rewrap_returns_relaid_lines(server) -> None:
    base, _ = server
    long = "Це достатньо довга репліка, щоб вона не вмістилась у сорок два символи"
    data = call(base, "/rewrap", {"cues": [{"start": 0, "end": 6, "text": long}]})
    assert len(data["cues"][0]["lines"]) == 2


def test_save_writes_when_there_are_no_errors(server) -> None:
    base, written = server
    data = call(base, "/save", {"cues": [{"start": 0, "end": 3, "text": "Норм."}]})
    assert data["saved"] is True
    assert data["stale"] == ["uk.ass"]
    assert written[0][0].text == "Норм."


def test_save_refuses_errors_until_forced(server) -> None:
    base, written = server
    broken = {"cues": [{"start": 0, "end": 1, "text": ""}]}

    blocked = call(base, "/save", broken)
    assert (blocked["saved"], blocked["blocked"], blocked["errors"]) == (False, True, 1)
    assert written == []

    forced = call(base, "/save", {**broken, "force": True})
    assert forced["saved"] is True
    assert len(written) == 1


def test_server_answers_broken_body_with_400(server) -> None:
    base, _ = server
    request = urllib.request.Request(
        f"{base}/analyze", data=b"{ not json", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 400
    assert "error" in json.loads(caught.value.read())


def test_server_answers_range_with_partial_content(server) -> None:
    base, _ = server
    request = urllib.request.Request(f"{base}/video", headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(request) as response:
        body = response.read()
    assert response.status == 206
    assert body == (bytes(range(256)) * 8)[100:200]


def test_server_has_no_other_routes(server) -> None:
    base, _ = server
    # Роздачі каталогу немає — отже, і виходу за межі проєкту теж.
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{base}/../../etc/passwd")
    assert caught.value.code == 404


def read_response(link: socket.socket) -> bytes:
    """Один HTTP-відгук: заголовки плюс стільки тіла, скільки в них обіцяно."""
    answer = b""
    while b"\r\n\r\n" not in answer:
        chunk = link.recv(4096)
        assert chunk, "сервер закрив зʼєднання, не відповівши"
        answer += chunk

    head, _, tail = answer.partition(b"\r\n\r\n")
    length = int(
        dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line).get(
            b"Content-Length", b"0"
        )
    )
    while len(tail) < length:
        tail += link.recv(4096)
    return head + b"\r\n\r\n" + tail


def test_oversized_body_does_not_poison_the_connection(server) -> None:
    """Тіло, яке сервер відмовився розбирати, лишається в сокеті.

    На HTTP/1.1 його хвіст розбирається як наступний запит, і цілком справний
    GET отримує 414 замість реплік. Сервер має тіло дочитати й викинути —
    тоді зʼєднання лишається придатним, а сторінка бачить причину відмови.
    """
    base, _ = server
    host, _, port = base.removeprefix("http://").partition(":")
    body = json.dumps({"cues": [{"start": 0, "end": 1, "text": "x" * (2 << 20)}]}).encode()

    with socket.create_connection((host, int(port)), timeout=5) as link:
        link.sendall(
            b"POST /analyze HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        refusal = read_response(link)
        assert refusal.startswith(b"HTTP/1.1 400")
        assert b"Connection: close" not in refusal

        link.sendall(b"GET /cues.json HTTP/1.1\r\nHost: x\r\n\r\n")
        answer = read_response(link)

    assert answer.startswith(b"HTTP/1.1 200")
    assert "Привіт, світе." in answer.decode("utf-8")


def test_body_too_big_even_to_discard_closes_the_connection(server) -> None:
    """Читати гігабайт тільки щоб його викинути — не варте зʼєднання."""
    base, _ = server
    host, _, port = base.removeprefix("http://").partition(":")

    with socket.create_connection((host, int(port)), timeout=5) as link:
        link.sendall(
            b"POST /analyze HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1000000000\r\n\r\n"
        )
        refusal = read_response(link)

    assert refusal.startswith(b"HTTP/1.1 400")
    assert b"Connection: close" in refusal


# --------------------------------------------------------------------------- #
# Файл, що змінився під редактором
# --------------------------------------------------------------------------- #


@pytest.fixture
def guarded(tmp_path):
    """Редактор із відбитком файлу; `state` дає підмінити відбиток на льоту."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 64)
    state = {"stamp": "v1", "written": []}

    running = build_server(
        video=video,
        load=lambda: [cue("Привіт, світе.", 0.0, 3.0)],
        store=lambda cues, force: state["written"].append(cues) or {"stale": []},
        opts=OPTS,
        title="тест",
        port=0,
        stamp=lambda: state["stamp"],
    )
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{running.server_port}", state
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)


def test_cues_carry_the_fingerprint_of_the_file(guarded) -> None:
    base, _ = guarded
    with urllib.request.urlopen(f"{base}/cues.json") as response:
        assert json.loads(response.read())["stamp"] == "v1"


def test_save_refuses_to_overwrite_a_file_that_moved_under_the_editor(guarded) -> None:
    base, state = guarded
    good = {"cues": [{"start": 0, "end": 3, "text": "Норм."}]}

    state["stamp"] = "v2"  # поки редактор був відкритий, subtitle перезібрав файл
    blocked = call(base, "/save", {**good, "stamp": "v1"})
    assert (blocked["saved"], blocked["moved"], blocked["stamp"]) == (False, True, "v2")
    assert state["written"] == []

    forced = call(base, "/save", {**good, "stamp": "v1", "force": True})
    assert forced["saved"] is True
    assert len(state["written"]) == 1


def test_save_goes_through_when_the_file_is_untouched(guarded) -> None:
    base, state = guarded
    data = call(base, "/save", {"cues": [{"start": 0, "end": 3, "text": "Норм."}], "stamp": "v1"})
    assert data["saved"] is True
    assert len(state["written"]) == 1


def test_save_reports_a_refusing_disk_as_json(tmp_path) -> None:
    """Голий 500 від BaseHTTP — це HTML, і response.json() на ньому падає.

    Сторінка тоді каже «сервер відповів не JSON» замість причини, яку
    операційна система вже назвала.
    """
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 64)

    def refuse(cues: list[Cue], force: bool) -> dict:
        raise OSError("файл зайнятий іншим процесом")

    running = build_server(
        video=video,
        load=lambda: [cue("Привіт.", 0.0, 3.0)],
        store=refuse,
        opts=OPTS,
        title="тест",
        port=0,
    )
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{running.server_port}"
        with pytest.raises(urllib.error.HTTPError) as caught:
            call(base, "/save", {"cues": [{"start": 0, "end": 3, "text": "Норм."}]})
        assert caught.value.code == 500
        assert "файл зайнятий" in json.loads(caught.value.read())["error"]
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)
