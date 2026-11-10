from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from videoworks.strike_server import STATIC, build_server

VIDEO = bytes(range(256)) * 8  # 2048 байтів


def make_state(transcript: str = "t1") -> dict:
    return {"transcript": transcript, "words": list(range(10)), "duration": 60.0}


def start(tmp_path, *, store, stamp=lambda: "v1"):
    """Піднімає справжній сервер на вільному порті; повертає (сервер, потік, база)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(VIDEO)
    running = build_server(video=video, load=make_state, store=store, stamp=stamp, port=0)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    return running, thread, f"http://127.0.0.1:{running.server_port}"


def stop(running, thread) -> None:
    running.shutdown()
    running.server_close()
    thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    """Сервер зі змінним відбитком; `calls` збирає все, що дійшло до store."""
    calls: list[tuple[list[int], list[tuple[float, float]]]] = []
    stamp = {"value": "v1"}

    def store(words, manual) -> dict:
        calls.append((words, manual))
        stamp["value"] = "v2"
        return {"struck": words, "manual": manual, "spans": [[1.0, 2.0]], "counts": {"words": 2}}

    running, thread, base = start(tmp_path, store=store, stamp=lambda: stamp["value"])
    try:
        yield base, calls, stamp
    finally:
        stop(running, thread)


def post(base: str, body: object, route: str = "/strikes"):
    request = urllib.request.Request(
        f"{base}{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def good(**extra) -> dict:
    return {"words": [3, 4], "manual": [[40.0, 41.0]], "transcript": "t1", "stamp": "v1", **extra}


def test_state_carries_the_stamp(server) -> None:
    base, _, _ = server
    with urllib.request.urlopen(f"{base}/state.json") as response:
        data = json.loads(response.read())
    assert data["stamp"] == "v1"
    assert data["transcript"] == "t1"


def test_static_files_are_served_with_their_types(server) -> None:
    base, _, _ = server
    expected = {
        "/": "text/html; charset=utf-8",
        "/style.css": "text/css; charset=utf-8",
        "/app.js": "text/javascript; charset=utf-8",
        "/logic.js": "text/javascript; charset=utf-8",
        "/brush.js": "text/javascript; charset=utf-8",
        "/player.js": "text/javascript; charset=utf-8",
        "/sheet.js": "text/javascript; charset=utf-8",
        "/sound.js": "text/javascript; charset=utf-8",
    }
    assert {route: kind for route, (_, kind) in STATIC.items()} == expected
    for route, kind in expected.items():
        with urllib.request.urlopen(f"{base}{route}") as response:
            assert response.headers["Content-Type"] == kind
            assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("route", ["/../cli.py", "/web/strike/app.js", "/strikes.json"])
def test_only_whitelisted_paths_exist(server, route) -> None:
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{base}{route}")
    assert caught.value.code == 404


def test_post_stores_and_returns_fresh_spans(server) -> None:
    base, calls, _ = server
    data = post(base, good())
    assert calls == [([3, 4], [(40.0, 41.0)])]
    assert data["saved"] is True
    assert data["spans"] == [[1.0, 2.0]]
    assert data["counts"] == {"words": 2}
    assert data["struck"] == [3, 4]
    assert data["stamp"] == "v2"


def test_post_with_a_stale_stamp_is_refused_and_writes_nothing(server) -> None:
    base, calls, stamp = server
    stamp["value"] = "v9"
    data = post(base, good())
    assert (data["saved"], data["moved"], data["stamp"]) == (False, True, "v9")
    assert calls == []


def test_post_without_a_stamp_is_refused_unless_forced(server) -> None:
    base, calls, _ = server
    body = good()
    del body["stamp"]
    assert post(base, body)["moved"] is True
    assert calls == []
    assert post(base, {**body, "force": True})["saved"] is True


def test_post_with_force_overrides_a_stale_stamp(server) -> None:
    base, calls, stamp = server
    stamp["value"] = "v9"
    data = post(base, good(force=True))
    assert data["saved"] is True
    assert len(calls) == 1


def test_post_for_another_transcript_is_refused_even_with_force(server) -> None:
    base, calls, _ = server
    data = post(base, good(transcript="other", force=True))
    assert (data["saved"], data["moved"]) == (False, True)
    assert calls == []


def test_range_request_gets_partial_content(server) -> None:
    base, _, _ = server
    request = urllib.request.Request(f"{base}/video", headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(request) as response:
        assert response.status == 206
        assert response.read() == VIDEO[100:200]


def test_head_video_reports_size(server) -> None:
    base, _, _ = server
    request = urllib.request.Request(f"{base}/video", method="HEAD")
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Length"] == str(len(VIDEO))
        assert response.headers["Accept-Ranges"] == "bytes"


def test_refusing_disk_is_reported_as_json(tmp_path) -> None:
    def refuse(words, manual) -> dict:
        raise OSError("файл зайнятий іншим процесом")

    running, thread, base = start(tmp_path, store=refuse)
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            post(base, good())
        assert caught.value.code == 500
        assert "зайнятий" in json.loads(caught.value.read())["error"]
    finally:
        stop(running, thread)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"words": "3"},
        {"words": [1.5]},
        {"words": [True]},
        {"words": [-1]},
        {"words": [999]},
        {"words": [1, 1]},
        {"words": [], "manual": [[5]]},
        {"words": [], "manual": [[5, 5]]},
        {"words": [], "manual": [[6, 5]]},
        {"words": [], "manual": [[-1, 2]]},
        {"words": [], "manual": [[0, 1e9]]},
        {"words": [], "manual": [[float("nan"), 2]]},
        {"words": [], "manual": [[0, 10**400]]},
        {"words": [10**400]},
    ],
)
def test_broken_bodies_get_400_and_write_nothing(server, body) -> None:
    base, calls, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(base, {**body, "transcript": "t1", "stamp": "v1"})
    assert caught.value.code == 400
    assert "error" in json.loads(caught.value.read())
    assert calls == []


def test_non_object_body_gets_400(server) -> None:
    base, calls, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(base, [1, 2])
    assert caught.value.code == 400
    assert calls == []


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
    """Хвіст тіла, яке сервер відмовився розбирати, не має стати наступним запитом."""
    base, calls, _ = server
    host, _, port = base.removeprefix("http://").partition(":")
    body = json.dumps({"words": [], "pad": "x" * (2 << 20)}).encode()

    with socket.create_connection((host, int(port)), timeout=5) as link:
        link.sendall(
            b"POST /strikes HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        refusal = read_response(link)
        assert refusal.startswith(b"HTTP/1.1 400")
        assert b"Connection: close" not in refusal

        link.sendall(b"GET /state.json HTTP/1.1\r\nHost: x\r\n\r\n")
        answer = read_response(link)

    assert answer.startswith(b"HTTP/1.1 200")
    assert b'"transcript"' in answer
    assert calls == []
