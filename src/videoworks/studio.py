"""Візуальний редактор субтитрів: правка в браузері, поверх кадру.

`preview` показує, як репліка лягає на кадр, але полагодити її там не можна —
доводиться йти в текстовий редактор, правити наосліп і повертатись подивитись.
Ця сторінка замикає коло: той самий плеєр і той самий список, але текст і
таймкоди редагуються просто в ньому, а результат видно в кадрі одразу.

Стрічка внизу поводиться як доріжка в монтажці: масштаб під курсором, лінійка
з рідшими підписами й частішими рисками, прилипання країв до сусідів і до
головки. Це не прикраса — на око по пікселях межу репліки не поставиш, а
таймкод, знятий з мовлення, майже завжди збігається з чиїмось краєм.

Розкладку на рядки й перевірки рахує сервер, не сторінка. Це навмисно: правила
живуть у `subtitles` та `editor`, і другої їх копії на JavaScript, яка поволі
розійдеться з першою, бути не повинно. Сторінка на кожну правку питає сервер,
що з цього вийшло, і малює відповідь.

Сервер — зі стандартної бібліотеки й тільки на 127.0.0.1: інструмент для одного
редактора за цією ж машиною, а не спосіб роздати матеріал назовні.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from videoworks import preview
from videoworks.editor import analyze_cues
from videoworks.subtitles import Cue, CueOptions, wrap_lines

# Година субтитрів — це десь 700 реплік і кілька сотень кілобайтів JSON.
# Мегабайт лишає запас, але не дає одним запитом зʼїсти памʼять процесу.
MAX_BODY = 1 << 20

# Тіло, яке ми відмовились розбирати, треба все одно дочитати: на HTTP/1.1 його
# хвіст лишається в сокеті й розбирається як наступний запит — і справний GET
# отримує 414 на власне сміття. Дочитуємо до цієї межі, викидаючи прочитане;
# далі дешевше розірвати зʼєднання, ніж молоти чужі мегабайти.
DRAIN_LIMIT = 16 << 20
DRAIN_CHUNK = 1 << 16


class BadRequest(ValueError):
    """Тіло запиту не годиться.

    Окремий тип, а не голий ValueError: обробник ловить саме його й відповідає
    400. Помилка десь глибше в розборі — це вже 500, і плутати їх не варто.
    """


# --------------------------------------------------------------------------- #
# Дані
# --------------------------------------------------------------------------- #


def limits(opts: CueOptions) -> dict:
    """Ліміти, за якими сторінка малює лічильники й шкали.

    Сторінка їх не перевіряє — перевіряє сервер. Вони потрібні їй, щоб показати
    «37/42», а не голе «37»: без стелі число нічого не каже.
    """
    return {
        "max_line_chars": opts.max_line_chars,
        "max_lines": opts.max_lines,
        "max_cps": opts.max_cps,
        "min_duration": opts.min_duration,
        "max_duration": opts.max_duration,
    }


def analysis(cues: list[Cue], opts: CueOptions) -> dict:
    """Репліки з метриками й проблемами — те саме, що бачить `preview`."""
    data = preview.payload(analyze_cues(cues, opts))
    data["limits"] = limits(opts)
    return data


def read_cues(data: object) -> list[Cue]:
    """Розбирає репліки, надіслані сторінкою.

    Кидає BadRequest на будь-яке відхилення: тіло приходить із браузера, і
    єдина розумна відповідь на несподіваний формат — 400, а не половина
    прочитаних реплік і мовчазна втрата решти.
    """
    if not isinstance(data, dict) or not isinstance(data.get("cues"), list):
        raise BadRequest("очікується {cues: [...]}")

    cues: list[Cue] = []
    for item in data["cues"]:
        if not isinstance(item, dict):
            raise BadRequest("репліка має бути обʼєктом")
        try:
            start = float(item["start"])
            end = float(item["end"])
            text = str(item["text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BadRequest("репліці бракує start, end або text") from exc

        # NaN проходить float() мовчки, а далі отруює і порівняння з лімітами,
        # і сортування: NaN не більший і не менший ні за що. Нескінченність
        # гірша: json.loads приймає Infinity, json.dumps віддає його назад, а
        # JSON.parse у браузері на такому падає — і сторінка застигає німою.
        if not math.isfinite(start) or not math.isfinite(end):
            raise BadRequest("таймкод не є скінченним числом")

        # Порожній текст лишаємо: analyze_cues назве це помилкою і покаже її
        # в списку. Мовчки викинути репліку було б гірше — зникла б і мітка.
        cues.append(Cue(start=start, end=end, lines=text.split("\n")))

    return cues


def rewrap(cues: list[Cue], opts: CueOptions) -> list[Cue]:
    """Перескладає рядки кожної репліки за поточними лімітами.

    Ручні переноси при цьому зникають — тому це окрема дія на вимогу, а не
    те, що стається саме собою на кожну правку тексту.
    """
    return [
        Cue(start=cue.start, end=cue.end, lines=wrap_lines(cue.text, opts), speaker=cue.speaker)
        for cue in cues
    ]


# --------------------------------------------------------------------------- #
# Сервер
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args,
        page_html: str,
        load: Callable[[], list[Cue]],
        store: Callable[[list[Cue], bool], dict],
        opts: CueOptions,
        video: Path,
        stamp: Callable[[], str] | None = None,
        **kwargs,
    ) -> None:
        self._page = page_html
        self._load = load
        self._store = store
        self._opts = opts
        self._video = video
        self._stamp = stamp
        super().__init__(*args, **kwargs)

    # Інакше кожен шматок відео друкував би рядок, і власний вивід команди
    # потонув би в сотнях 206-х.
    def log_message(self, *args: object) -> None:
        return

    # Імена методів диктує BaseHTTPRequestHandler.
    def do_GET(self) -> None:
        route = self.path.partition("?")[0]
        if route == "/":
            self._send(self._page.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/cues.json":
            self._json({**analysis(self._load(), self._opts), "stamp": self._fingerprint()})
        elif route == "/video":
            preview.send_video(self, self._video)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        # Плеєр питає розмір і підтримку діапазонів ще до першого байта.
        if self.path.partition("?")[0] == "/video":
            preview.send_video(self, self._video, body=False)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        route = self.path.partition("?")[0]
        if route not in ("/analyze", "/rewrap", "/save"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            body = self._read_body()
            cues = read_cues(body)
        except BadRequest as exc:
            # `close_connection` ставить `_read_body`, коли тіло лишилось
            # недочитаним: тоді відповідь — остання в цьому зʼєднанні.
            self._json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
                close=self.close_connection,
            )
            return

        if route == "/rewrap":
            cues = rewrap(cues, self._opts)

        if route != "/save":
            self._json(analysis(cues, self._opts))
            return

        self._save(cues, body)

    # ----------------------------------------------------------------- #

    def _save(self, cues: list[Cue], body: dict) -> None:
        report = analysis(cues, self._opts)
        current = self._fingerprint()
        force = bool(body.get("force"))

        # Помилка — це репліка, яку глядач не побачить або не прочитає. Писати
        # такий файл поверх робочого мовчки не можна, тому сторінка має спитати
        # ще раз; це та сама угода, що й `--force` у команді edit.
        if report["errors"] and not force:
            self._json({**report, "saved": False, "blocked": True, "stamp": current})
            return

        # Поки редактор був відкритий, файл міг перезібрати `subtitle` — і тоді
        # запис затер би чужу роботу тим, що сторінка прочитала півгодини тому.
        sent = body.get("stamp")
        if sent is not None and current and sent != current and not force:
            self._json({**report, "saved": False, "moved": True, "stamp": current})
            return

        try:
            written = self._store(cues, force)
        except OSError as exc:
            # Диск, права, зайнятий файл — сторінка має побачити причину, а не
            # сторінку 500 від BaseHTTP, на якій її response.json() спіткнеться.
            self._json(
                {"error": f"не вдалось записати: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self._json({**report, "saved": True, **written, "stamp": self._fingerprint()})

    def _fingerprint(self) -> str:
        return self._stamp() if self._stamp is not None else ""

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise BadRequest("немає Content-Length") from exc
        if length <= 0:
            raise BadRequest("порожнє тіло запиту")
        if length > MAX_BODY:
            self._discard(length)
            raise BadRequest(f"тіло більше за {MAX_BODY} байтів")

        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest("тіло не є JSON") from exc

    def _discard(self, length: int) -> None:
        """Дочитує й викидає тіло, яке ми відмовились розбирати.

        Інакше зʼєднання лишається з чужими байтами напоготові. Якщо тіло надто
        велике навіть щоб його викинути, помічаємо зʼєднання на розрив — це
        єдиний спосіб не читати те, що читати не збирались.
        """
        if length > DRAIN_LIMIT:
            self.close_connection = True
            return

        left = length
        while left > 0:
            chunk = self.rfile.read(min(DRAIN_CHUNK, left))
            if not chunk:
                self.close_connection = True
                return
            left -= len(chunk)

    def _json(self, data: dict, *, status: HTTPStatus = HTTPStatus.OK, close: bool = False) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Тіло, яке ми відмовились читати, лишилось у сокеті — і на HTTP/1.1
        # сервер прочитав би його хвіст як наступний запит, відповідаючи 414 на
        # цілком справні GET. Дешевше розірвати зʼєднання, ніж розбирати сміття.
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(
    *,
    video: Path,
    load: Callable[[], list[Cue]],
    store: Callable[[list[Cue], bool], dict],
    opts: CueOptions,
    title: str,
    port: int = 8771,
    stamp: Callable[[], str] | None = None,
) -> preview.Server:
    """Піднімає редактор на 127.0.0.1. Порт 0 — вибрати вільний самому.

    `load` читає репліки з диска, `store` пише їх назад і повертає, що саме
    сталося, `stamp` віддає відбиток файлу — за ним видно, що поки редактор
    був відкритий, файл перезібрали. Сам модуль файлів не торкається — як
    і `editor`.
    """
    handler = partial(
        _Handler,
        page_html=page(title),
        load=load,
        store=store,
        opts=opts,
        video=video,
        stamp=stamp,
    )
    return preview.Server(("127.0.0.1", port), handler)


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
  /* Палітра системна: ті самі значення, що в елементах керування macOS та iOS.
     Темна — основна (з відео працюють у темряві), світла переозначає токени. */
  :root {
    --ground: #1c1c1e;
    --surface: #2c2c2e;
    --raised: #3a3a3c;
    --hair: rgba(255, 255, 255, .11);
    --fill: rgba(120, 120, 128, .28);
    --fill-hi: rgba(120, 120, 128, .44);
    --ink: #f5f5f7;
    --dim: #98989d;
    --faint: #636366;
    --blue: #0a84ff;
    --blue-soft: rgba(10, 132, 255, .18);
    --red: #ff453a;
    --orange: #ff9f0a;
    --green: #30d158;
    --shade: rgba(0, 0, 0, .55);
    --glass: rgba(28, 28, 30, .72);
    color-scheme: dark;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --ground: #f2f2f7;
      --surface: #ffffff;
      --raised: #ffffff;
      --hair: rgba(60, 60, 67, .16);
      --fill: rgba(120, 120, 128, .16);
      --fill-hi: rgba(120, 120, 128, .28);
      --ink: #1c1c1e;
      --dim: #6e6e73;
      --faint: #aeaeb2;
      --blue: #0071e3;
      --blue-soft: rgba(0, 113, 227, .12);
      --red: #d70015;
      --orange: #b25000;
      --green: #248a3d;
      --shade: rgba(0, 0, 0, .12);
      --glass: rgba(242, 242, 247, .72);
      color-scheme: light;
    }
  }
  :root[data-theme="light"] {
    --ground: #f2f2f7;
    --surface: #ffffff;
    --raised: #ffffff;
    --hair: rgba(60, 60, 67, .16);
    --fill: rgba(120, 120, 128, .16);
    --fill-hi: rgba(120, 120, 128, .28);
    --ink: #1c1c1e;
    --dim: #6e6e73;
    --faint: #aeaeb2;
    --blue: #0071e3;
    --blue-soft: rgba(0, 113, 227, .12);
    --red: #d70015;
    --orange: #b25000;
    --green: #248a3d;
    --shade: rgba(0, 0, 0, .12);
    --glass: rgba(242, 242, 247, .72);
    color-scheme: light;
  }

  /* SF Pro Display і SF Pro Text — два оптичні накреслення одного шрифта, і
     система підставляє їх сама. На Windows цю роль грає Segoe UI Variable. */
  :root {
    --ui: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text",
          "Segoe UI", system-ui, sans-serif;
    --display: -apple-system, BlinkMacSystemFont, "SF Pro Display",
               "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", "Consolas", monospace;
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font: 400 13px/1.45 var(--ui);
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
  }
  .tc, .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }

  :focus-visible {
    outline: 2px solid color-mix(in srgb, var(--blue) 70%, transparent);
    outline-offset: 2px;
    border-radius: 6px;
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
  }

  /* --- Панель інструментів ------------------------------------------- */

  .toolbar {
    display: flex; align-items: center; gap: 14px;
    height: 52px; padding: 0 16px; flex: 0 0 auto;
    border-bottom: 1px solid var(--hair);
    background: var(--glass);
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    position: relative; z-index: 5;
  }
  .doc { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  .doc b {
    font: 600 13px/1.2 var(--display); letter-spacing: -.01em;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .doc span { color: var(--dim); font-size: 11px; }
  .doc span.edited::after { content: " · змінено"; color: var(--orange); }

  .grow { flex: 1 1 auto; }

  /* Сегментований перемикач — той самий елемент, що у панелях керування. */
  .seg {
    display: flex; gap: 2px; padding: 2px; flex: 0 0 auto;
    background: var(--fill); border-radius: 8px;
  }
  .seg button {
    appearance: none; border: 0; cursor: pointer;
    font: 500 12px/1 var(--ui); color: var(--ink);
    background: transparent; border-radius: 6px; padding: 5px 11px;
    transition: background .15s ease;
  }
  .seg button[aria-selected="true"] {
    background: var(--raised);
    box-shadow: 0 1px 2px var(--shade);
  }

  .tally { display: flex; gap: 6px; flex: 0 0 auto; }
  .pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px; border-radius: 999px;
    background: var(--fill); color: var(--dim);
    font-size: 11px; font-variant-numeric: tabular-nums;
  }
  .pill b { color: var(--ink); font-weight: 600; }
  .pill .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); }
  .pill.err .dot { background: var(--red); }
  .pill.err b { color: var(--red); }
  .pill.warn .dot { background: var(--orange); }
  .pill.warn b { color: var(--orange); }
  .pill.zero { opacity: .45; }

  .btn {
    appearance: none; cursor: pointer;
    font: 500 12px/1 var(--ui); color: var(--ink);
    background: var(--fill); border: 0; border-radius: 8px;
    padding: 7px 13px; flex: 0 0 auto;
    transition: background .15s ease, opacity .15s ease;
  }
  .btn:hover { background: var(--fill-hi); }
  .btn.primary { background: var(--blue); color: #fff; font-weight: 600; }
  .btn.primary:hover { filter: brightness(1.08); }
  .btn:disabled { opacity: .38; cursor: default; }
  .btn:disabled:hover { background: var(--fill); filter: none; }
  .btn.icon { padding: 7px 9px; font-family: var(--mono); }
  .btn.on { background: var(--blue-soft); color: var(--blue); }

  /* --- Розкладка ------------------------------------------------------ */

  main {
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(360px, .85fr);
    height: calc(100% - 52px);
  }
  .canvas {
    display: flex; flex-direction: column; gap: 12px;
    padding: 14px 14px 0; min-width: 0; min-height: 0;
  }
  .cues {
    border-left: 1px solid var(--hair);
    overflow-y: auto; overscroll-behavior: contain;
    min-width: 0;
  }
  @media (max-width: 980px) {
    body { overflow: auto; }
    main { grid-template-columns: 1fr; height: auto; }
    .cues { border-left: 0; border-top: 1px solid var(--hair); }
  }

  /* --- Кадр ----------------------------------------------------------- */

  .frame {
    position: relative; container-type: inline-size;
    background: #000; border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 3px var(--shade), 0 12px 32px var(--shade);
    flex: 0 1 auto; min-height: 0;
  }
  video { display: block; width: 100%; max-height: 52vh; background: #000; }
  .caption {
    position: absolute; left: 0; right: 0; bottom: 7%;
    text-align: center; padding: 0 6%; pointer-events: none;
    white-space: pre-line; color: #fff;
    font-family: var(--display); font-weight: 700; line-height: 1.24;
    font-size: clamp(13px, 2.2vw, 26px);
    font-size: 4.6cqw;
    text-shadow: 0 0 4px #000, 0 0 4px #000, 0 2px 3px rgba(0, 0, 0, .9);
  }
  /* Рамка гарантованої видимості: 90% кадру. Те, що виходить за неї, на
     частині екранів обріжеться — і саме там підпис найчастіше й лізе. */
  .safe {
    position: absolute; inset: 5%; pointer-events: none;
    border: 1px dashed rgba(255, 255, 255, .34); border-radius: 3px;
    display: none;
  }
  .frame.guides .safe { display: block; }

  /* --- Транспорт ------------------------------------------------------ */

  .transport {
    display: flex; align-items: center; gap: 10px; flex: 0 0 auto;
  }
  .transport .tc { font-size: 12px; }
  .transport .tc.of { color: var(--dim); }
  .switch {
    display: inline-flex; align-items: center; gap: 6px;
    color: var(--dim); font-size: 12px; cursor: pointer;
  }
  .switch input { accent-color: var(--blue); margin: 0; }
  .switch:hover { color: var(--ink); }

  /* --- Стрічка реплік -------------------------------------------------- */

  .ribbon {
    flex: 0 0 auto; height: 102px; position: relative;
    background: var(--surface); border: 1px solid var(--hair); border-radius: 12px;
    overflow-x: auto; overflow-y: hidden; overscroll-behavior-x: contain;
  }
  .track { position: relative; height: 100%; min-width: 100%; cursor: text; }
  /* Смуга лінійки вгорі: підписи стоять у ній, а не поверх блоків. */
  .track::before {
    content: ""; position: absolute; left: 0; right: 0; top: 21px;
    border-top: 1px solid var(--hair); pointer-events: none;
  }
  .tick { position: absolute; top: 14px; bottom: 0; border-left: 1px solid var(--hair); }
  .tick.lead { top: 0; border-left-color: var(--fill-hi); }
  .tick span {
    position: absolute; top: 3px; left: 5px;
    font-family: var(--mono); font-size: 10px; color: var(--faint);
    font-variant-numeric: tabular-nums; pointer-events: none; white-space: nowrap;
  }
  .blk {
    position: absolute; top: 30px; height: 56px; min-width: 3px;
    border-radius: 6px; overflow: hidden; cursor: grab;
    background: var(--fill); border: 1px solid var(--hair);
    padding: 4px 6px; font-size: 10px; line-height: 1.25; color: var(--ink);
    transition: box-shadow .12s ease;
  }
  .blk:active { cursor: grabbing; }
  .blk.warning { background: color-mix(in srgb, var(--orange) 22%, transparent); }
  .blk.error { background: color-mix(in srgb, var(--red) 24%, transparent); }
  .blk.on { box-shadow: 0 0 0 2px var(--blue); z-index: 2; }
  .blk .grip {
    position: absolute; top: 0; bottom: 0; width: 7px; cursor: ew-resize;
    background: transparent;
  }
  .blk .grip.l { left: 0; }
  .blk .grip.r { right: 0; }
  .blk:hover .grip { background: var(--blue-soft); }
  .head {
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: var(--red); pointer-events: none; z-index: 3;
  }
  .head::before {
    content: ""; position: absolute; top: 0; left: -4px;
    width: 9px; height: 7px; border-radius: 0 0 3px 3px; background: var(--red);
  }
  /* Лінія прилипання: поки її видно, край стоїть не там, де курсор, а там,
     де сусід або головка — інакше зсув на пів пікселя читався б як промах. */
  .magnet {
    position: absolute; top: 14px; bottom: 0; width: 1px;
    background: var(--green); pointer-events: none; z-index: 4; display: none;
  }
  .magnet.show { display: block; }

  /* --- Підказки клавіш -------------------------------------------------- */

  .keys {
    flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: 4px 14px;
    padding: 8px 2px 12px; color: var(--faint); font-size: 11px;
  }
  .keys kbd {
    font-family: var(--mono); font-size: 10px; color: var(--dim);
    background: var(--fill); border-radius: 4px; padding: 1px 5px; margin-right: 3px;
  }

  /* --- Список реплік ---------------------------------------------------- */

  .row {
    display: grid; grid-template-columns: 3px 30px minmax(0, 1fr);
    gap: 0 10px; align-items: start;
    padding: 11px 14px 11px 0; border-bottom: 1px solid var(--hair);
    cursor: default;
  }
  .row:hover { background: color-mix(in srgb, var(--ink) 4%, transparent); }
  .row.on { background: var(--blue-soft); }
  .stripe { align-self: stretch; border-radius: 0 2px 2px 0; background: transparent; }
  .row.warning .stripe { background: var(--orange); }
  .row.error .stripe { background: var(--red); }
  .row .num { color: var(--faint); font-size: 11px; text-align: right; padding-top: 5px; }

  .body { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .times { display: flex; align-items: center; gap: 6px; }
  .times input {
    width: 10ch; padding: 3px 5px; text-align: center;
    font-family: var(--mono); font-size: 11px; font-variant-numeric: tabular-nums;
    color: var(--ink); background: var(--fill); border: 1px solid transparent;
    border-radius: 5px;
  }
  .times input:hover { border-color: var(--hair); }
  .times input:focus { background: var(--raised); border-color: var(--blue); outline: none; }
  .times .arrow { color: var(--faint); font-size: 11px; }
  .times .dur { color: var(--dim); font-size: 11px; font-family: var(--mono); }

  .acts { display: flex; gap: 2px; margin-left: auto; opacity: 0; transition: opacity .12s ease; }
  .row:hover .acts, .row.on .acts, .acts:focus-within { opacity: 1; }
  .acts button {
    appearance: none; border: 0; cursor: pointer; border-radius: 5px;
    background: transparent; color: var(--dim); font: 500 11px/1 var(--ui);
    padding: 4px 7px;
  }
  .acts button:hover { background: var(--fill-hi); color: var(--ink); }
  .acts button.danger:hover { background: color-mix(in srgb, var(--red) 24%, transparent); color: var(--red); }

  .row textarea {
    width: 100%; resize: none; overflow: hidden;
    font: 400 13px/1.4 var(--ui); color: var(--ink);
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: 5px 7px; margin: -1px -8px; box-sizing: content-box;
  }
  .row textarea:hover { border-color: var(--hair); }
  .row textarea:focus {
    background: var(--raised); border-color: var(--blue); outline: none;
    box-shadow: 0 0 0 3px var(--blue-soft);
  }

  .meters { display: flex; align-items: center; gap: 10px; color: var(--dim); font-size: 11px; }
  .meters .num { font-size: 11px; }
  .meters .over { color: var(--red); }
  .meters .near { color: var(--orange); }
  .bar {
    flex: 0 0 56px; height: 3px; border-radius: 2px;
    background: var(--fill); overflow: hidden;
  }
  .bar i { display: block; height: 100%; background: var(--green); border-radius: 2px; }
  .bar i.near { background: var(--orange); }
  .bar i.over { background: var(--red); }

  .why { display: flex; flex-direction: column; gap: 3px; font-size: 11px; }
  .why .warning { color: var(--orange); }
  .why .error { color: var(--red); }

  .blank { padding: 28px 20px; color: var(--dim); text-align: center; }

  /* --- Смуга збереження і сповіщення ------------------------------------ */

  .ask {
    position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
    display: none; align-items: center; gap: 12px; z-index: 20;
    padding: 10px 12px 10px 16px; border-radius: 12px;
    background: var(--glass); backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    border: 1px solid var(--hair); box-shadow: 0 8px 32px var(--shade);
    font-size: 12px; max-width: min(560px, 92vw);
  }
  .ask.show { display: flex; }
  .toast {
    position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%) translateY(8px);
    z-index: 21; padding: 9px 16px; border-radius: 999px;
    background: var(--glass); backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    border: 1px solid var(--hair); box-shadow: 0 8px 32px var(--shade);
    font-size: 12px; opacity: 0; pointer-events: none;
    transition: opacity .2s ease, transform .2s ease;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>

<div class="toolbar">
  <div class="doc">
    <b>__TITLE__</b>
    <span id="stamp">читаю…</span>
  </div>
  <div class="seg" role="tablist" id="seg">
    <button role="tab" aria-selected="true" data-filter="all">Усі</button>
    <button role="tab" aria-selected="false" data-filter="bad">Проблемні</button>
  </div>
  <div class="tally" id="tally"></div>
  <div class="grow"></div>
  <button class="btn icon" id="theme" title="Світла або темна тема">◐</button>
  <button class="btn" id="rewrap" title="Перескласти рядки за лімітами">Перерозкласти</button>
  <button class="btn primary" id="save" disabled>Зберегти</button>
</div>

<main>
  <section class="canvas">
    <div class="frame" id="frame">
      <video id="video" src="/video" preload="metadata" playsinline></video>
      <div class="safe"></div>
      <div class="caption" id="caption"></div>
    </div>

    <div class="transport">
      <button class="btn icon" id="play" title="Пробіл">▶</button>
      <span class="tc" id="clock">0:00.00</span>
      <span class="tc of" id="total">/ —</span>
      <button class="btn" id="split" title="Розрізати вибрану репліку по головці">Розділити</button>
      <div class="grow"></div>
      <button class="btn icon on" id="magnet" title="Прилипання до сусідів і головки" aria-pressed="true">⌁</button>
      <button class="btn icon" id="out" title="Віддалити">−</button>
      <button class="btn icon" id="in" title="Наблизити">+</button>
      <button class="btn" id="fit">Умістити</button>
      <label class="switch"><input type="checkbox" id="guides">Рамка кадру</label>
      <label class="switch"><input type="checkbox" id="follow" checked>Стежити</label>
    </div>

    <div class="ribbon" id="ribbon">
      <div class="track" id="track">
        <div class="magnet" id="magnetLine"></div>
        <div class="head" id="head"></div>
      </div>
    </div>

    <div class="keys">
      <span><kbd>Пробіл</kbd>грати</span>
      <span><kbd>↑ ↓</kbd>репліка</span>
      <span><kbd>← →</kbd>перемотка</span>
      <span><kbd>[</kbd><kbd>]</kbd>межа по головці</span>
      <span><kbd>Enter</kbd>правити текст</span>
      <span><kbd>Ctrl+колесо</kbd>масштаб</span>
      <span><kbd>Ctrl+Z</kbd>відкотити</span>
      <span><kbd>Ctrl+S</kbd>зберегти</span>
    </div>
  </section>

  <section class="cues" id="list"></section>
</main>

<div class="ask" id="ask">
  <span id="askText"></span>
  <div class="grow"></div>
  <button class="btn" id="askNo">Не зараз</button>
  <button class="btn primary" id="askYes">Все одно зберегти</button>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const video = $("video"), list = $("list"), track = $("track"), ribbon = $("ribbon");
const caption = $("caption"), head = $("head"), clock = $("clock"), total = $("total");
const tally = $("tally"), stamp = $("stamp"), saveBtn = $("save"), ask = $("ask");
const magnetLine = $("magnetLine");

// Правда про репліки живе тут: {start, end, text}. Решта — lines, chars, cps,
// problems — приходить із сервера й лише малюється.
let cues = [];
let marks = [];
let limits = { max_line_chars: 42, max_lines: 2, max_cps: 17, min_duration: 0.83 };
let filter = "all";
let picked = 0;
let dirty = false;
let pps = 12;
let snapping = true;
let past = [], future = [];
let rows = new Map();
let blocks = new Map();
let fingerprint = null;

// Два лічильники, бо відповідь буває застарілою двома різними способами.
// seq відкидає ту, яку обігнала наступна; rev — пораховану для іншого набору
// реплік. Без другого розкладка від пʼяти реплік лягла б на чотири, що
// лишились після видалення, і мітки стали б навпроти чужого тексту.
let seq = 0;
let rev = 0;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const duration = () => (isFinite(video.duration) && video.duration > 0)
  ? video.duration
  : Math.max(1, ...cues.map((c) => c.end));

// --- Таймкоди ------------------------------------------------------------ //

function clockText(s) {
  s = Math.max(0, s);
  const m = Math.floor(s / 60);
  return m + ":" + (s - m * 60).toFixed(2).padStart(5, "0");
}

function parseClock(text) {
  const clean = text.trim().replace(",", ".");
  if (!clean) return null;
  const parts = clean.split(":").map(Number);
  if (parts.some((n) => !isFinite(n) || n < 0)) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

// --- Розмова із сервером -------------------------------------------------- //

function wire() {
  return { cues: cues.map((c) => ({ start: c.start, end: c.end, text: c.text })) };
}

let pending = null;
function refresh(delay) {
  clearTimeout(pending);
  pending = setTimeout(() => {
    post("/analyze", wire()).then((answer) => {
      if (answer.fresh) absorb(answer.data);
    }).catch(complain);
  }, delay === undefined ? 220 : delay);
}

async function post(route, body) {
  const at = rev;
  const mine = ++seq;
  const response = await fetch(route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  let data;
  try {
    data = await response.json();
  } catch (error) {
    // Відповідь не JSON — значить, впав не наш обробник, а щось під ним.
    // Ковтнути SyntaxError тут означало б не показати жодної причини.
    throw new Error("сервер відповів не JSON (" + response.status + ")");
  }
  if (!response.ok) throw new Error(data.error || "сервер відмовив");
  return { data: data, fresh: mine === seq && at === rev };
}

function complain(error) {
  flash("Сервер не відповів: " + error.message);
}

function absorb(data) {
  if (!data) return;
  marks = data.cues;
  limits = data.limits;
  if (data.stamp) fingerprint = data.stamp;
  paintTally(data);
  paintRows();
  paintLevels();
  paintCaption();
}

// --- Малювання ------------------------------------------------------------ //

function plural(n, one, few, many) {
  const t = n % 100, u = n % 10;
  if (t > 10 && t < 20) return many;
  if (u === 1) return one;
  if (u > 1 && u < 5) return few;
  return many;
}

const counted = (n) => n + " " + plural(n, "репліка", "репліки", "реплік");

function paintTally(data) {
  tally.replaceChildren();
  const add = (kind, n, word) => {
    const pill = document.createElement("span");
    pill.className = "pill " + kind + (n ? "" : " zero");
    const dot = document.createElement("i");
    dot.className = "dot";
    const count = document.createElement("b");
    count.textContent = n;
    pill.append(dot, count, document.createTextNode(" " + word));
    tally.append(pill);
  };
  add("", cues.length, plural(cues.length, "репліка", "репліки", "реплік"));
  add("err", data.errors, plural(data.errors, "помилка", "помилки", "помилок"));
  add("warn", data.warnings, plural(data.warnings, "попередження", "попередження", "попереджень"));
}

function shown() {
  return cues.map((cue, i) => i).filter((i) => filter === "all" || !marks[i] || marks[i].level !== "ok");
}

// Мітка, поки не відповів сервер. Символи й швидкість тут ті самі, що порахує
// він; рівень і проблеми — його справа, і вони приїдуть наступним кадром.
function placeholder(cue) {
  const lines = cue.text.split("\n");
  const chars = lines.join(" ").length;
  const span = cue.end - cue.start;
  return {
    start: cue.start, end: cue.end, lines: lines, chars: chars,
    cps: span > 0 ? Math.round((chars / span) * 10) / 10 : null,
    level: "ok", problems: [],
  };
}

// Рядки, які справді треба показати. Поки відповідь на правку летить, розкладка
// в marks стосується попереднього тексту — тоді беремо свій, нерозкладений.
function shownLines(i) {
  const mark = marks[i];
  const local = cues[i].text.split("\n");
  if (mark && mark.lines.join(" ") === local.join(" ")) return mark.lines;
  return local;
}

// Одна дія на всі структурні правки: після них у marks не можна вірити, поки
// сервер не перерахує заново, а старі відповіді треба відкинути — звідси rev.
function restructure() {
  rev += 1;
  markKey = null;
  marks = cues.map(placeholder);
  picked = clamp(picked, 0, Math.max(0, cues.length - 1));
  touch();
  buildRows();
  buildBlocks();
  layout();
  refresh(0);
}

function buildRows() {
  rows.clear();
  list.replaceChildren();
  const visible = shown();
  if (!visible.length) {
    const blank = document.createElement("div");
    blank.className = "blank";
    blank.textContent = filter === "bad" ? "Проблемних реплік немає." : "Реплік немає.";
    list.append(blank);
    return;
  }
  for (const i of visible) list.append(makeRow(i));
  paintRows();
}

function makeRow(i) {
  const row = document.createElement("div");
  row.className = "row";
  row.dataset.i = i;
  row.onmousedown = (event) => { if (event.target === row) pick(i, false); };

  const stripe = document.createElement("div");
  stripe.className = "stripe";
  const num = document.createElement("div");
  num.className = "num";
  num.textContent = i + 1;

  const body = document.createElement("div");
  body.className = "body";

  const times = document.createElement("div");
  times.className = "times";
  const from = timeField(i, "start");
  const to = timeField(i, "end");
  const arrow = document.createElement("span");
  arrow.className = "arrow";
  arrow.textContent = "→";
  const dur = document.createElement("span");
  dur.className = "dur";
  times.append(from, arrow, to, dur);
  times.append(actions(i));

  const area = document.createElement("textarea");
  area.rows = 1;
  area.spellcheck = false;
  area.value = cues[i].text;
  area.onfocus = () => pick(i, false);
  area.oninput = () => {
    cues[i].text = area.value;
    touch();
    // Свій рядок перемальовуємо одразу, решту — коли відповість сервер: інакше
    // кожна літера коштувала б перерахунку висоти всіх реплік у списку.
    paintRow(i);
    placeBlock(i);
    paintCaption();
    refresh();
  };
  area.onkeydown = (event) => {
    if (event.key === "Escape") { area.blur(); event.stopPropagation(); }
  };
  area.onbeforeinput = () => remember("text:" + i);
  area.onblur = () => { markKey = null; };

  const meters = document.createElement("div");
  meters.className = "meters";
  const why = document.createElement("div");
  why.className = "why";

  body.append(times, area, meters, why);
  row.append(stripe, num, body);
  rows.set(i, { row: row, from: from, to: to, dur: dur, area: area, meters: meters, why: why, grown: null });
  return row;
}

function timeField(i, key) {
  const field = document.createElement("input");
  field.className = "tc";
  field.value = clockText(cues[i][key]);
  field.onfocus = () => pick(i, false);
  field.onchange = () => {
    const value = parseClock(field.value);
    if (value === null) { field.value = clockText(cues[i][key]); return; }
    remember();
    retime(i, key === "start" ? value : cues[i].start, key === "end" ? value : cues[i].end);
    refresh(0);
  };
  field.onkeydown = (event) => {
    const step = event.shiftKey ? 1 : 0.05;
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    remember("nudge:" + i + key);
    const next = cues[i][key] + (event.key === "ArrowUp" ? step : -step);
    retime(i, key === "start" ? next : cues[i].start, key === "end" ? next : cues[i].end);
    field.value = clockText(cues[i][key]);
    refresh(0);
  };
  field.onblur = () => { markKey = null; };
  return field;
}

function actions(i) {
  const box = document.createElement("div");
  box.className = "acts";
  const make = (label, hint, kind, run) => {
    const button = document.createElement("button");
    button.textContent = label;
    button.title = hint;
    if (kind) button.className = kind;
    button.onclick = run;
    box.append(button);
    return button;
  };
  make("Розділити", "Розрізати репліку по головці", "", () => split(i));
  make("Злити", "Приєднати наступну репліку", "", () => merge(i));
  make("Видалити", "Прибрати репліку", "danger", () => drop(i));
  return box;
}

// Висота поля рахується через scrollHeight, а це синхронний перерахунок
// розкладки. Робимо його лише коли текст справді змінився з минулого разу.
function grow(parts) {
  if (parts.grown === parts.area.value) return;
  parts.grown = parts.area.value;
  parts.area.style.height = "auto";
  parts.area.style.height = parts.area.scrollHeight + "px";
}

function paintRows() {
  for (const i of rows.keys()) paintRow(i);
}

function paintRow(i) {
  const parts = rows.get(i);
  if (!parts) return;
  const mark = marks[i] || placeholder(cues[i]);
  parts.row.className = "row " + mark.level + (i === picked ? " on" : "");
  parts.dur.textContent = (cues[i].end - cues[i].start).toFixed(2) + " с";
  if (document.activeElement !== parts.from) parts.from.value = clockText(cues[i].start);
  if (document.activeElement !== parts.to) parts.to.value = clockText(cues[i].end);
  grow(parts);
  paintMeters(parts.meters, mark);
  parts.why.replaceChildren();
  for (const problem of mark.problems) {
    const line = document.createElement("div");
    line.className = problem.level;
    line.textContent = problem.message;
    parts.why.append(line);
  }
}

// Поля таймкодів рухаються на кожен крок тяги, решта рядка — ні.
function paintTimes(i) {
  const parts = rows.get(i);
  if (!parts) return;
  parts.from.value = clockText(cues[i].start);
  parts.to.value = clockText(cues[i].end);
  parts.dur.textContent = (cues[i].end - cues[i].start).toFixed(2) + " с";
}

function paintMeters(box, mark) {
  box.replaceChildren();
  const budget = limits.max_line_chars * limits.max_lines;
  const chars = document.createElement("span");
  chars.className = "num" + (mark.chars > budget ? " over" : "");
  chars.textContent = mark.chars + "/" + budget + " симв";

  const rate = document.createElement("span");
  const speed = mark.cps === null ? null : mark.cps;
  const over = speed !== null && speed > limits.max_cps;
  const near = speed !== null && !over && speed > limits.max_cps * 0.9;
  rate.className = "num" + (over ? " over" : near ? " near" : "");
  rate.textContent = (speed === null ? "∞" : speed.toFixed(1)) + " симв/с";

  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("i");
  fill.className = over ? "over" : near ? "near" : "";
  fill.style.width = clamp(((speed === null ? limits.max_cps * 2 : speed) / limits.max_cps) * 100, 0, 100) + "%";
  bar.append(fill);

  const lines = document.createElement("span");
  lines.className = "num" + (mark.lines.length > limits.max_lines ? " over" : "");
  lines.textContent = mark.lines.length + "/" + limits.max_lines + " ряд";

  box.append(chars, rate, bar, lines);
}

// --- Лінійка --------------------------------------------------------------- //

// Кроки лінійки: від чверті секунди до півгодини. Підпис і риска беруть крок
// незалежно — підпису треба місце, щоб не злипнутись із сусіднім, рисці ні.
const STEPS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
const MIN_TICK_PX = 16;
const MIN_LABEL_PX = 92;

const divides = (small, big) => Math.abs(big / small - Math.round(big / small)) < 1e-6;

function stepFor(minPx) {
  return STEPS.find((s) => s * pps >= minPx) || STEPS[STEPS.length - 1];
}

function ruler() {
  const label = stepFor(MIN_LABEL_PX);
  // Риска має ділити підпис націло, інакше підпис висить між рисками й
  // виглядає як помилка вимірювання, а не як шкала.
  const tick = STEPS.find((s) => s * pps >= MIN_TICK_PX && divides(s, label)) || label;
  return { label: label, tick: tick };
}

function paintTicks() {
  for (const old of track.querySelectorAll(".tick")) old.remove();
  const steps = ruler();
  const span = duration();
  // Малюємо лише видиме вікно: на великому масштабі година відео — це десятки
  // тисяч рисок, і браузер укладав би їх довше, ніж триває саме відео.
  const from = Math.max(0, Math.floor(ribbon.scrollLeft / pps / steps.tick) * steps.tick);
  const to = Math.min(span, (ribbon.scrollLeft + ribbon.clientWidth) / pps + steps.tick);
  for (let t = from; t <= to; t += steps.tick) {
    const lead = divides(steps.tick, steps.label) && Math.abs(t % steps.label) < 1e-6;
    const tick = document.createElement("div");
    tick.className = "tick" + (lead ? " lead" : "");
    tick.style.left = (t * pps) + "px";
    if (lead) {
      const mark = document.createElement("span");
      mark.textContent = steps.label >= 1 ? clockText(t).replace(/\.\d+$/, "") : clockText(t);
      tick.append(mark);
    }
    track.append(tick);
  }
}

// --- Блоки на стрічці -------------------------------------------------------- //

function buildBlocks() {
  for (const old of track.querySelectorAll(".blk")) old.remove();
  blocks.clear();
  cues.forEach((cue, i) => {
    const block = document.createElement("div");
    block.className = "blk";
    block.dataset.i = i;
    for (const side of ["l", "r"]) {
      const grip = document.createElement("div");
      grip.className = "grip " + side;
      grip.dataset.side = side;
      block.append(grip);
    }
    const label = document.createElement("span");
    block.prepend(label);
    blocks.set(i, { block: block, label: label });
    track.append(block);
  });
}

// Один блок, а не всі: під час тяги це єдине, що рухається, і перебирати
// заради нього кілька сотень інших означало б втрачати кадри на кожен крок.
function placeBlock(i) {
  const parts = blocks.get(i);
  if (!parts) return;
  const cue = cues[i];
  const mark = marks[i];
  parts.block.className = "blk " + (mark ? mark.level : "ok") + (i === picked ? " on" : "");
  parts.block.style.left = (cue.start * pps) + "px";
  parts.block.style.width = Math.max(3, (cue.end - cue.start) * pps) + "px";
  parts.block.title = clockText(cue.start) + " → " + clockText(cue.end);
  if (parts.label.textContent !== cue.text) parts.label.textContent = cue.text;
}

function placeBlocks() {
  for (const i of blocks.keys()) placeBlock(i);
}

// Приїхали свіжі мітки — змінився лише колір, не геометрія.
function paintLevels() {
  for (const [i, parts] of blocks) {
    const mark = marks[i];
    parts.block.className = "blk " + (mark ? mark.level : "ok") + (i === picked ? " on" : "");
  }
}

function layout() {
  track.style.width = (duration() * pps) + "px";
  paintTicks();
  placeBlocks();
  paintHead();
}

function paintHead() {
  head.style.left = (video.currentTime * pps) + "px";
}

// --- Масштаб ----------------------------------------------------------------- //

// Точка під курсором лишається на місці: інакше після наближення шукаєш очима,
// куди поїхало те, на що дивився.
function zoomAt(factor, clientX) {
  const box = ribbon.getBoundingClientRect();
  const x = clientX === undefined ? ribbon.clientWidth / 2 : clamp(clientX - box.left, 0, ribbon.clientWidth);
  const at = (ribbon.scrollLeft + x) / pps;
  pps = clamp(pps * factor, 0.5, 400);
  layout();
  ribbon.scrollLeft = at * pps - x;
}

function fit() {
  pps = clamp((ribbon.clientWidth - 8) / duration(), 0.5, 400);
  layout();
  ribbon.scrollLeft = 0;
}

// --- Прилипання --------------------------------------------------------------- //

const SNAP_PX = 8;

// Межі сусідів і головка. Свою репліку пропускаємо: прилипати до себе нема сенсу.
function snapPoints(skip) {
  const points = [video.currentTime];
  cues.forEach((cue, i) => {
    if (i === skip) return;
    points.push(cue.start, cue.end);
  });
  return points;
}

function snapTo(time, skip) {
  if (!snapping) return { time: time, at: null };
  const reach = SNAP_PX / pps;
  let best = null, closest = Infinity;
  for (const point of snapPoints(skip)) {
    const away = Math.abs(time - point);
    if (away <= reach && away < closest) { closest = away; best = point; }
  }
  return best === null ? { time: time, at: null } : { time: best, at: best };
}

function showMagnet(at) {
  magnetLine.classList.toggle("show", at !== null);
  if (at !== null) magnetLine.style.left = (at * pps) + "px";
}

// --- Правки ---------------------------------------------------------------- //

function snapshot() {
  return JSON.stringify(cues.map((c) => [c.start, c.end, c.text]));
}

// Друк — це сотні подій, і крок історії на кожну зробив би відкат посимвольним,
// а стелю в 120 кроків — одним реченням. Ключ тримає весь сеанс правки одного
// поля за один крок: поки правлять те саме, історія стоїть.
let markKey = null;
function remember(key) {
  if (key !== undefined && key === markKey) return;
  markKey = key === undefined ? null : key;
  const now = snapshot();
  if (past.length && past[past.length - 1] === now) return;
  past.push(now);
  if (past.length > 120) past.shift();
  future.length = 0;
}

function restore(text) {
  cues = JSON.parse(text).map(([start, end, body]) => ({ start: start, end: end, text: body }));
  restructure();
}

function undo() {
  if (!past.length) return;
  future.push(snapshot());
  restore(past.pop());
  flash("Відкотили");
}

function redo() {
  if (!future.length) return;
  past.push(snapshot());
  restore(future.pop());
}

function touch() {
  dirty = true;
  saveBtn.disabled = false;
  stamp.className = "edited";
  stamp.textContent = counted(cues.length);
}

// Куди межа може поїхати, не наїхавши на сусіда.
function room(i) {
  const span = duration();
  return {
    before: i > 0 ? cues[i - 1].end : 0,
    after: i < cues.length - 1 ? cues[i + 1].start : span,
  };
}

const LEAST = 0.05;

function retime(i, start, end) {
  const edge = room(i);
  const latest = Math.max(edge.before, edge.after - LEAST);
  start = clamp(start, edge.before, latest);
  end = clamp(end, start + LEAST, Math.max(start + LEAST, edge.after));
  cues[i] = { start: start, end: end, text: cues[i].text };
  touch();
  placeBlock(i);
  paintRow(i);
  paintCaption();
}

// Куди стане репліка, якщо тягнути її за `side` на `dt` секунд. Рахує, але не
// застосовує: тяга показує наслідок щокроку, а в історію лягає один раз.
function propose(i, side, base, dt) {
  const edge = room(i);
  if (side === "move") {
    // Цілу репліку тягнемо, зберігаючи довжину. Інакше, доїхавши до сусіда,
    // вона схлопувалась би в смужку замість спинитись об нього.
    const width = base.end - base.start;
    const hit = snapTo(base.start + dt, i);
    const start = clamp(hit.time, edge.before, Math.max(edge.before, edge.after - width));
    return { start: start, end: start + width, at: hit.time === start ? hit.at : null };
  }
  if (side === "l") {
    const hit = snapTo(base.start + dt, i);
    const start = clamp(hit.time, edge.before, base.end - LEAST);
    return { start: start, end: base.end, at: hit.time === start ? hit.at : null };
  }
  const hit = snapTo(base.end + dt, i);
  const end = clamp(hit.time, base.start + LEAST, edge.after);
  return { start: base.start, end: end, at: hit.time === end ? hit.at : null };
}

function split(i) {
  const at = video.currentTime;
  const cue = cues[i];
  if (!cue || at <= cue.start + LEAST || at >= cue.end - LEAST) {
    flash("Постав головку всередині репліки");
    return;
  }
  remember();
  const words = cue.text.split(/\s+/).filter(Boolean);
  const share = (at - cue.start) / (cue.end - cue.start);
  const cutAt = clamp(Math.round(words.length * share), 1, Math.max(1, words.length - 1));
  cues.splice(i, 1,
    { start: cue.start, end: at, text: words.slice(0, cutAt).join(" ") },
    { start: at, end: cue.end, text: words.slice(cutAt).join(" ") });
  picked = i;
  restructure();
}

function merge(i) {
  if (i >= cues.length - 1) { flash("Наступної репліки немає"); return; }
  remember();
  const next = cues[i + 1];
  cues.splice(i, 2, { start: cues[i].start, end: next.end, text: (cues[i].text + " " + next.text).trim() });
  picked = i;
  restructure();
}

function drop(i) {
  remember();
  cues.splice(i, 1);
  picked = i;
  restructure();
}

// --- Вибір і відтворення ---------------------------------------------------- //

function pick(i, seek) {
  if (i < 0 || i >= cues.length) return;
  const was = picked;
  picked = i;
  paintRow(was);
  paintRow(i);
  placeBlock(was);
  placeBlock(i);
  if (seek) video.currentTime = cues[i].start + 0.001;
  const row = list.querySelector(".row[data-i=\"" + i + "\"]");
  if (row && $("follow").checked) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function paintCaption() {
  const t = video.currentTime;
  // Шукаємо по своїх репліках, а не по мітках: під час тяги мітки ще несуть
  // старі межі, і підпис блимав би на місці, з якого край уже поїхав.
  const at = cues.findIndex((c) => t >= c.start && t < c.end);
  caption.textContent = at < 0 ? "" : shownLines(at).join("\n");
  return at;
}

let ticking = false;
function tick() {
  clock.textContent = clockText(video.currentTime);
  paintHead();
  const at = paintCaption();
  if (at >= 0 && at !== picked && !video.paused && $("follow").checked) pick(at, false);
  if ($("follow").checked && !video.paused) {
    const x = video.currentTime * pps;
    if (x < ribbon.scrollLeft || x > ribbon.scrollLeft + ribbon.clientWidth - 40) {
      ribbon.scrollLeft = x - ribbon.clientWidth / 3;
    }
  }
}

// Один цикл на все відтворення. Раніше кожна перемотка під час гри запускала
// ще один поверх наявного, і за десяток стрибків їх ставало десяток.
function loop() {
  tick();
  if (video.paused) { ticking = false; return; }
  requestAnimationFrame(loop);
}

function run() {
  if (video.paused) video.play(); else video.pause();
}

// --- Перетягування на стрічці ------------------------------------------------ //

track.addEventListener("pointerdown", (event) => {
  const block = event.target.closest(".blk");
  if (!block) {
    const to = clamp((event.clientX - track.getBoundingClientRect().left) / pps, 0, duration());
    video.currentTime = to;
    return;
  }
  event.preventDefault();
  const i = Number(block.dataset.i);
  const side = event.target.dataset.side || "move";
  const from = event.clientX;
  const base = { start: cues[i].start, end: cues[i].end };
  pick(i, false);
  track.setPointerCapture(event.pointerId);

  // Тяга починається не з натискання, а з першого справжнього руху: інакше
  // звичайний клік по блоку лишав би в історії порожній крок.
  let dragging = false;
  const move = (step) => {
    if (!dragging) {
      if (Math.abs(step.clientX - from) < 2) return;
      dragging = true;
      remember();
    }
    const next = propose(i, side, base, (step.clientX - from) / pps);
    cues[i] = { start: next.start, end: next.end, text: cues[i].text };
    placeBlock(i);
    paintTimes(i);
    showMagnet(next.at);
  };
  const stop = () => {
    track.removeEventListener("pointermove", move);
    track.removeEventListener("pointerup", stop);
    track.removeEventListener("pointercancel", stop);
    showMagnet(null);
    if (!dragging) return;
    touch();
    paintRow(i);
    paintCaption();
    refresh(0);
  };
  track.addEventListener("pointermove", move);
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
});

track.addEventListener("dblclick", (event) => {
  const block = event.target.closest(".blk");
  if (!block) return;
  const parts = rows.get(Number(block.dataset.i));
  if (parts) parts.area.focus();
});

// Ctrl+колесо — масштаб, як у монтажках; просто колесо гортає стрічку вбік,
// бо вертикально їй їхати нікуди.
ribbon.addEventListener("wheel", (event) => {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    zoomAt(Math.exp(-event.deltaY * 0.002), event.clientX);
    return;
  }
  if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
    event.preventDefault();
    ribbon.scrollLeft += event.deltaY;
  }
}, { passive: false });

let scrolling = false;
ribbon.addEventListener("scroll", () => {
  if (scrolling) return;
  scrolling = true;
  requestAnimationFrame(() => { scrolling = false; paintTicks(); });
});

// --- Збереження --------------------------------------------------------------- //

async function store(force) {
  saveBtn.disabled = true;
  ask.classList.remove("show");
  // Відкладений /analyze інакше встиг би збити лічильник запитів, і відповідь
  // на запис сторінка викинула б як застарілу — файл записаний, а на екрані
  // нічого, і кнопка лишилась сірою.
  clearTimeout(pending);
  try {
    const answer = await post("/save", Object.assign(wire(), { force: force, stamp: fingerprint }));
    const data = answer.data;
    if (answer.fresh) absorb(data); else refresh(0);
    if (data.stamp) fingerprint = data.stamp;

    if (data.blocked) {
      $("askText").textContent = "Помилок: " + data.errors + ". Такі репліки глядач не побачить або не прочитає.";
      ask.classList.add("show");
      saveBtn.disabled = false;
      return;
    }
    if (data.moved) {
      $("askText").textContent = "Файл на диску змінився, поки редактор був відкритий. Записати поверх?";
      ask.classList.add("show");
      saveBtn.disabled = false;
      return;
    }

    dirty = false;
    markKey = null;
    stamp.className = "";
    stamp.textContent = "збережено · " + new Date().toLocaleTimeString("uk-UA");
    flash(data.stale && data.stale.length ? "Записано. " + data.stale.join(", ") + " лишились старими" : "Записано");
  } catch (error) {
    saveBtn.disabled = false;
    flash("Не записано: " + error.message);
  }
}

let toastTimer = null;
function flash(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

// --- Клавіатура ----------------------------------------------------------------- //

document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  const meta = event.ctrlKey || event.metaKey;
  const key = event.key || "";

  if (meta && key.toLowerCase() === "s") { event.preventDefault(); if (dirty) store(false); return; }
  if (meta && key.toLowerCase() === "z") {
    event.preventDefault();
    if (event.shiftKey) redo(); else undo();
    return;
  }
  if (typing) return;

  const step = event.shiftKey ? 1 : 0.1;
  if (key === " ") { event.preventDefault(); run(); }
  else if (key === "ArrowRight") { event.preventDefault(); video.currentTime += step; }
  else if (key === "ArrowLeft") { event.preventDefault(); video.currentTime -= step; }
  else if (key === "ArrowDown") { event.preventDefault(); pick(picked + 1, true); }
  else if (key === "ArrowUp") { event.preventDefault(); pick(picked - 1, true); }
  else if (key === "Enter") {
    event.preventDefault();
    const parts = rows.get(picked);
    if (parts) parts.area.focus();
  } else if (key === "[" || key === "]") {
    event.preventDefault();
    const cue = cues[picked];
    if (!cue) return;
    remember();
    if (key === "[") retime(picked, video.currentTime, cue.end);
    else retime(picked, cue.start, video.currentTime);
    refresh(0);
  }
});

// --- Дроти ------------------------------------------------------------------------ //

video.addEventListener("play", () => {
  $("play").textContent = "❙❙";
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(loop);
});
video.addEventListener("pause", () => { $("play").textContent = "▶"; });
video.addEventListener("seeked", () => { if (!ticking) tick(); });
video.addEventListener("timeupdate", () => { if (!ticking) tick(); });
video.addEventListener("loadedmetadata", () => {
  total.textContent = "/ " + clockText(video.duration);
  fit();
  tick();
});
video.addEventListener("error", () => {
  // Відео може бути ще не змонтоване або лежати не там. Текст і таймкоди від
  // цього не залежать, тому редактор лишається робочим — але мовчати не варто.
  total.textContent = "/ немає відео";
  flash("Відео не читається — текст і таймкоди правити можна");
  fit();
});

$("play").onclick = run;
$("in").onclick = () => zoomAt(1.6);
$("out").onclick = () => zoomAt(1 / 1.6);
$("fit").onclick = fit;
$("split").onclick = () => split(picked);
$("guides").onchange = (event) => $("frame").classList.toggle("guides", event.target.checked);
$("save").onclick = () => store(false);
$("askYes").onclick = () => store(true);
$("askNo").onclick = () => ask.classList.remove("show");

$("magnet").onclick = () => {
  snapping = !snapping;
  $("magnet").classList.toggle("on", snapping);
  $("magnet").setAttribute("aria-pressed", String(snapping));
  flash(snapping ? "Прилипання увімкнено" : "Прилипання вимкнено");
};

$("rewrap").onclick = async () => {
  remember();
  try {
    const answer = await post("/rewrap", wire());
    rev += 1;
    cues = answer.data.cues.map((c) => ({ start: c.start, end: c.end, text: c.lines.join("\n") }));
    marks = answer.data.cues;
    limits = answer.data.limits;
    touch();
    buildRows();
    paintTally(answer.data);
    placeBlocks();
    flash("Рядки перескладено");
  } catch (error) {
    complain(error);
  }
};

$("seg").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  filter = button.dataset.filter;
  for (const tab of $("seg").children) tab.setAttribute("aria-selected", String(tab === button));
  buildRows();
};

$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("vw-theme", next);
};
const saved = localStorage.getItem("vw-theme");
if (saved) document.documentElement.dataset.theme = saved;

window.addEventListener("resize", () => { paintTicks(); });

window.addEventListener("beforeunload", (event) => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

// --- Старт -------------------------------------------------------------------------- //

async function boot() {
  try {
    const response = await fetch("/cues.json", { cache: "no-store" });
    const data = await response.json();
    cues = data.cues.map((c) => ({ start: c.start, end: c.end, text: c.lines.join("\n") }));
    marks = data.cues;
    limits = data.limits;
    fingerprint = data.stamp || null;
    stamp.textContent = counted(cues.length);
    paintTally(data);
    buildRows();
    buildBlocks();
    layout();
    tick();
  } catch (error) {
    // Без реплік сторінка нічим не корисна, і мовчазне «читаю…» назавжди —
    // найгірше з можливих пояснень.
    stamp.textContent = "не прочитав репліки";
    flash("Не вдалось прочитати репліки: " + error.message);
  }
}
boot();
</script>
</body>
</html>
"""


def page(title: str) -> str:
    """Сторінка редактора. Дані підтягуються окремим запитом на /cues.json."""
    return _PAGE.replace("__TITLE__", _escape(title))


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
