"""Локальний переглядач: відео і репліки поруч.

Звіт у терміналі каже, що репліка задовга. Він не каже, як вона лягає на кадр —
а саме там видно, що текст перекриває обличчя або зникає раніше, ніж людина
дочитала. Ця сторінка показує обидва боки одночасно: плеєр, підпис поверх
кадру й список реплік, що підсвічується разом із відтворенням.

Сервер — зі стандартної бібліотеки й тільки на 127.0.0.1: це інструмент для
одного глядача за цією ж машиною, а не спосіб роздати матеріал назовні.
"""

from __future__ import annotations

import json
import math
import mimetypes
import sys
from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from videoworks.editor import CueReport

# Кадр за кадром віддавати файл дорого, цілком — неможливо: годинне відео не
# влізе в памʼять. 256 КБ — компроміс, на якому перемотка ще миттєва.
CHUNK = 1 << 18


# --------------------------------------------------------------------------- #
# Дані для сторінки
# --------------------------------------------------------------------------- #


def payload(reports: list[CueReport]) -> dict:
    """Репліки з метриками у вигляді, готовому до json.dumps."""
    cues = []
    for report in reports:
        levels = {problem.level for problem in report.problems}
        cues.append(
            {
                "index": report.index,
                "start": report.cue.start,
                "end": report.cue.end,
                "lines": list(report.cue.lines),
                "chars": report.char_count,
                # Нульова тривалість дає нескінченність, а Infinity — не JSON:
                # JSON.parse на такому падає, і сторінка лишалась би порожньою.
                "cps": None if math.isinf(report.cps) else round(report.cps, 1),
                "level": ("error" if "error" in levels else "warning" if levels else "ok"),
                "problems": [
                    {"level": problem.level, "message": problem.message}
                    for problem in report.problems
                ],
            }
        )

    return {
        "cues": cues,
        "errors": sum(1 for cue in cues if cue["level"] == "error"),
        "warnings": sum(1 for cue in cues if cue["level"] == "warning"),
    }


# --------------------------------------------------------------------------- #
# Часткова віддача відео
# --------------------------------------------------------------------------- #


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """`Range: bytes=…` → межі включно, або None, якщо треба віддати все.

    Без цього браузер отримує 200 на весь файл і перемотка не працює: плеєр
    не має куди стрибнути, бо сервер не вміє віддати шматок із середини.

    Кидає ValueError на діапазон поза файлом — на нього відповідь 416, а не
    мовчазна віддача чогось іншого, ніж просили.
    """
    if not header:
        return None

    units, _, spec = header.partition("=")
    if units.strip().lower() != "bytes" or "," in spec:
        return None  # multipart-діапазони плеєрам не потрібні

    first, sep, last = spec.strip().partition("-")
    if not sep:
        return None

    try:
        if not first:
            # `bytes=-500` — останні 500 байтів.
            length = int(last)
            if length <= 0:
                raise ValueError(header)
            return max(0, size - length), size - 1
        start = int(first)
        stop = int(last) if last else size - 1
    except ValueError as exc:
        raise ValueError(header) from exc

    if start >= size or start > stop:
        raise ValueError(header)
    return start, min(stop, size - 1)


# --------------------------------------------------------------------------- #
# Сервер
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, page: str, load: Callable[[], dict], video: Path, **kwargs) -> None:
        self._page = page
        self._load = load
        self._video = video
        super().__init__(*args, **kwargs)

    # Кожен запит шматка відео інакше друкував би рядок у консоль, і власний
    # вивід команди потонув би в сотнях 206-х.
    def log_message(self, *args: object) -> None:
        return

    # Імена методів диктує BaseHTTPRequestHandler.
    def do_GET(self) -> None:
        route = self.path.partition("?")[0]
        if route == "/":
            self._send(self._page.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/cues.json":
            body = json.dumps(self._load(), ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        elif route == "/video":
            self._send_video()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        # Плеєр питає розмір і підтримку діапазонів ще до першого байта.
        if self.path.partition("?")[0] == "/video":
            self._send_video(body=False)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_video(self, *, body: bool = True) -> None:
        size = self._video.stat().st_size
        mime = mimetypes.guess_type(self._video.name)[0] or "video/mp4"

        try:
            span = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, stop = span or (0, size - 1)
        length = stop - start + 1

        self.send_response(HTTPStatus.PARTIAL_CONTENT if span else HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if span:
            self.send_header("Content-Range", f"bytes {start}-{stop}/{size}")
        self.end_headers()

        if not body:
            return

        with self._video.open("rb") as stream:
            stream.seek(start)
            left = length
            while left > 0:
                block = stream.read(min(CHUNK, left))
                if not block:
                    break
                # Плеєр обриває зʼєднання на кожній перемотці — це нормальний
                # хід подій, а не збій, тож і трейсбека тут бути не повинно.
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(block)


# --------------------------------------------------------------------------- #
# Сторінка
# --------------------------------------------------------------------------- #

# Рядок сирий: у JS всередині є "\n", і без r"""..."""
# Python перетворив би його на справжній перенос — просто посеред
# рядкового літерала. Сторінка валилася б на SyntaxError і лишалась порожньою.
_PAGE = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #14161a; --panel: #1b1e24; --line: #2a2f38;
    --text: #e8eaed; --dim: #9aa2ae;
    --ok: #4ba3f7; --warn: #e0a53c; --err: #e2564a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 12px 20px; border-bottom: 1px solid var(--line); background: var(--panel);
    position: sticky; top: 0; z-index: 2;
  }
  h1 { margin: 0; font-size: 15px; font-weight: 600; }
  .counts { color: var(--dim); font-variant-numeric: tabular-nums; }
  .counts b { font-weight: 600; }
  .counts .e { color: var(--err); }
  .counts .w { color: var(--warn); }
  header label, header button {
    color: var(--dim); font: inherit; background: none;
    border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer;
  }
  header label:hover, header button:hover { color: var(--text); border-color: var(--dim); }
  header input { accent-color: var(--ok); vertical-align: -2px; margin-right: 6px; }
  main { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .stage { padding: 20px; position: sticky; top: 53px; align-self: start; }
  .frame { position: relative; background: #000; border-radius: 8px; overflow: hidden; }
  video { display: block; width: 100%; max-height: 65vh; background: #000; }
  .caption {
    position: absolute; left: 0; right: 0; bottom: 6%;
    text-align: center; padding: 0 6%; pointer-events: none;
    font-weight: 700; font-size: clamp(14px, 2.4vw, 26px); line-height: 1.25;
    color: #fff; text-shadow: 0 0 4px #000, 0 0 4px #000, 0 2px 3px #000;
    white-space: pre-line;
  }
  .now { margin-top: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .cues { border-left: 1px solid var(--line); max-height: calc(100vh - 53px); overflow-y: auto; }
  .cue {
    display: grid; grid-template-columns: 8px 44px 1fr; gap: 10px; align-items: start;
    padding: 10px 16px; border-bottom: 1px solid var(--line); cursor: pointer;
  }
  .cue:hover { background: #20242b; }
  .cue.active { background: #232a35; box-shadow: inset 3px 0 0 var(--ok); }
  .dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; background: var(--line); }
  .cue.warning .dot { background: var(--warn); }
  .cue.error .dot { background: var(--err); }
  .num { color: var(--dim); font-variant-numeric: tabular-nums; text-align: right; }
  .text { white-space: pre-line; }
  .meta { color: var(--dim); font-size: 12px; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .problem { font-size: 12px; margin-top: 3px; }
  .problem.warning { color: var(--warn); }
  .problem.error { color: var(--err); }
  .empty { padding: 24px; color: var(--dim); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="counts" id="counts"></span>
  <label><input type="checkbox" id="only">лише проблемні</label>
  <label><input type="checkbox" id="follow" checked>стежити</label>
  <button id="reload">перечитати</button>
</header>
<main>
  <section class="stage">
    <div class="frame">
      <video id="video" src="/video" controls preload="metadata"></video>
      <div class="caption" id="caption"></div>
    </div>
    <div class="now" id="now">—</div>
  </section>
  <section class="cues" id="list"></section>
</main>
<script>
const video = document.getElementById("video");
const list = document.getElementById("list");
const caption = document.getElementById("caption");
const counts = document.getElementById("counts");
const now = document.getElementById("now");
const only = document.getElementById("only");
const follow = document.getElementById("follow");

let cues = [];
let active = null;

const clock = (s) => {
  const m = Math.floor(s / 60), rest = s - m * 60;
  return `${m}:${rest.toFixed(2).padStart(5, "0")}`;
};

function render() {
  const shown = only.checked ? cues.filter((c) => c.level !== "ok") : cues;
  list.replaceChildren();
  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = only.checked ? "Проблемних реплік немає." : "Реплік немає.";
    list.append(empty);
    return;
  }
  for (const cue of shown) {
    const row = document.createElement("div");
    row.className = `cue ${cue.level}`;
    row.dataset.index = cue.index;
    row.onclick = () => { video.currentTime = cue.start + 0.001; video.play(); };

    const dot = document.createElement("div");
    dot.className = "dot";
    const num = document.createElement("div");
    num.className = "num";
    num.textContent = cue.index;

    const body = document.createElement("div");
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = cue.lines.join("\n");
    const meta = document.createElement("div");
    meta.className = "meta";
    const speed = cue.cps === null ? "—" : `${cue.cps.toFixed(1)} симв/с`;
    meta.textContent = `${clock(cue.start)}–${clock(cue.end)} · ${(cue.end - cue.start).toFixed(1)} с · ${cue.chars} симв · ${speed}`;
    body.append(text, meta);
    for (const problem of cue.problems) {
      const line = document.createElement("div");
      line.className = `problem ${problem.level}`;
      line.textContent = problem.message;
      body.append(line);
    }
    row.append(dot, num, body);
    list.append(row);
  }
  mark(active, true);
}

function mark(cue, force) {
  if (!force && cue === active) return;
  active = cue;
  for (const row of list.querySelectorAll(".cue.active")) row.classList.remove("active");
  caption.textContent = cue ? cue.lines.join("\n") : "";
  if (!cue) return;
  const row = list.querySelector(`.cue[data-index="${cue.index}"]`);
  if (!row) return;
  row.classList.add("active");
  if (follow.checked) row.scrollIntoView({ block: "center", behavior: "smooth" });
}

video.addEventListener("timeupdate", () => {
  const t = video.currentTime;
  now.textContent = clock(t);
  mark(cues.find((c) => t >= c.start && t < c.end) || null, false);
});

async function load() {
  const data = await (await fetch("/cues.json", { cache: "no-store" })).json();
  cues = data.cues;
  counts.innerHTML = `<b>${cues.length}</b> реплік · <b class="e">${data.errors}</b> помилок · <b class="w">${data.warnings}</b> попереджень`;
  render();
}

only.onchange = render;
document.getElementById("reload").onclick = load;
load();
</script>
</body>
</html>
"""


def page(title: str) -> str:
    """Сторінка переглядача. Дані підтягуються окремим запитом на /cues.json,
    щоб «перечитати» показувало правку в .srt без перезапуску сервера."""
    return _PAGE.replace("__TITLE__", _escape(title))


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Server(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        """Обрив зʼєднання — не збій, і трейсбека він не вартий.

        Плеєр тримає зʼєднання відкритим (HTTP/1.1) і кидає його на кожній
        перемотці. Базовий socketserver друкує на це повний трейсбек із потоку,
        і консоль забивається ними швидше, ніж встигаєш прочитати свій вивід.
        Решта помилок лишається видимою — глушимо тільки розрив.
        """
        if not isinstance(sys.exception(), ConnectionError):
            super().handle_error(request, client_address)


def build_server(
    *,
    video: Path,
    load: Callable[[], dict],
    title: str,
    port: int = 8770,
) -> _Server:
    """Піднімає сервер на 127.0.0.1. Порт 0 — вибрати вільний самому."""
    handler = partial(_Handler, page=page(title), load=load, video=video)
    return _Server(("127.0.0.1", port), handler)
