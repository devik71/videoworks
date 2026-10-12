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
<link rel="icon" href="data:,">
<style>
  /* Темна тема — основна: з відео працюють у темряві, і світлий інтерфейс
     навколо кадру збиває око з кольору. Світла лише переозначає токени. */
  :root {
    --ground: #111113;
    --panel: #18181b;
    --surface: #1f1f23;
    --raised: #2a2a2f;
    --ruler: #141416;
    --hair: rgba(255, 255, 255, .07);
    --line: rgba(255, 255, 255, .12);
    --fill: rgba(255, 255, 255, .06);
    --fill-hi: rgba(255, 255, 255, .11);
    --ink: #ededef;
    --dim: #a0a0a8;
    --faint: #66666e;
    --blue: #4c8dff;
    --blue-soft: rgba(76, 141, 255, .16);
    --clip: #2b3a55;
    --clip-edge: #5b82c7;
    --red: #ff5a52;
    --orange: #f5a524;
    --green: #3ecf6e;
    --shade: rgba(0, 0, 0, .5);
    --glass: rgba(17, 17, 19, .82);
    color-scheme: dark;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --ground: #e9e9ec;
      --panel: #f6f6f8;
      --surface: #ffffff;
      --raised: #ffffff;
      --ruler: #efeff2;
      --hair: rgba(20, 20, 30, .08);
      --line: rgba(20, 20, 30, .14);
      --fill: rgba(20, 20, 30, .05);
      --fill-hi: rgba(20, 20, 30, .09);
      --ink: #18181b;
      --dim: #5f5f68;
      --faint: #9a9aa3;
      --blue: #2563eb;
      --blue-soft: rgba(37, 99, 235, .11);
      --clip: #dbe5f8;
      --clip-edge: #6b8fd6;
      --red: #d92d20;
      --orange: #b54708;
      --green: #16803c;
      --shade: rgba(20, 20, 30, .12);
      --glass: rgba(246, 246, 248, .86);
      color-scheme: light;
    }
  }
  :root[data-theme="light"] {
    --ground: #e9e9ec;
    --panel: #f6f6f8;
    --surface: #ffffff;
    --raised: #ffffff;
    --ruler: #efeff2;
    --hair: rgba(20, 20, 30, .08);
    --line: rgba(20, 20, 30, .14);
    --fill: rgba(20, 20, 30, .05);
    --fill-hi: rgba(20, 20, 30, .09);
    --ink: #18181b;
    --dim: #5f5f68;
    --faint: #9a9aa3;
    --blue: #2563eb;
    --blue-soft: rgba(37, 99, 235, .11);
    --clip: #dbe5f8;
    --clip-edge: #6b8fd6;
    --red: #d92d20;
    --orange: #b54708;
    --green: #16803c;
    --shade: rgba(20, 20, 30, .12);
    --glass: rgba(246, 246, 248, .86);
    color-scheme: light;
  }

  /* Системні шрифти: на macOS це SF, на Windows — Segoe UI Variable. Таймкоди
     моноширинні з табличними цифрами, інакше вони тремтять під час гри. */
  :root {
    --ui: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI",
          system-ui, sans-serif;
    --display: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Display",
               "Segoe UI", system-ui, sans-serif;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono", Consolas, monospace;
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font: 400 13px/1.4 var(--ui);
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
    display: grid;
    grid-template-rows: 48px minmax(0, 1fr) auto 26px;
  }
  .tc, .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  svg { display: block; flex: 0 0 auto; }

  :focus-visible {
    outline: 2px solid color-mix(in srgb, var(--blue) 70%, transparent);
    outline-offset: 1px;
    border-radius: 6px;
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
  }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb {
    background: var(--fill-hi); border-radius: 10px;
    border: 3px solid transparent; background-clip: padding-box;
  }
  ::-webkit-scrollbar-thumb:hover { background-color: var(--faint); }
  ::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background: transparent; }

  /* --- Кнопки ----------------------------------------------------------- */

  .btn {
    appearance: none; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    height: 30px; padding: 0 12px; flex: 0 0 auto;
    font: 500 12px/1 var(--ui); color: var(--ink);
    background: var(--fill); border: 1px solid var(--hair); border-radius: 7px;
    transition: background .12s ease, color .12s ease, border-color .12s ease;
  }
  .btn:hover { background: var(--fill-hi); }
  .btn:active { transform: translateY(.5px); }
  .btn.primary {
    background: var(--blue); border-color: transparent; color: #fff; font-weight: 600;
  }
  .btn.primary:hover { background: color-mix(in srgb, var(--blue) 88%, #fff); }
  .btn:disabled { opacity: .4; cursor: default; transform: none; }
  .btn:disabled:hover { background: var(--fill); }
  .btn.primary:disabled:hover { background: var(--blue); }
  .btn.ghost { background: transparent; border-color: transparent; color: var(--dim); }
  .btn.ghost:hover { background: var(--fill-hi); color: var(--ink); }
  .btn.icon { width: 30px; padding: 0; }
  .btn.on { background: var(--blue-soft); border-color: transparent; color: var(--blue); }
  .btn svg { width: 16px; height: 16px; }

  .sep { width: 1px; height: 18px; background: var(--line); flex: 0 0 auto; }
  .grow { flex: 1 1 auto; }

  /* --- Верхня панель ---------------------------------------------------- */

  .toolbar {
    display: flex; align-items: center; gap: 10px;
    padding: 0 12px 0 14px; min-width: 0;
    background: var(--panel); border-bottom: 1px solid var(--line);
  }
  .mark {
    width: 24px; height: 24px; border-radius: 6px; flex: 0 0 auto;
    display: grid; place-items: center; color: #fff;
    background: linear-gradient(135deg, var(--blue), color-mix(in srgb, var(--blue) 55%, #8b5cf6));
  }
  .mark svg { width: 14px; height: 14px; }
  .doc { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
  .doc b {
    font: 600 13px/1.2 var(--display); letter-spacing: -.005em;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .doc span { color: var(--dim); font-size: 12px; white-space: nowrap; }
  .doc span.edited { color: var(--orange); }
  .doc span.edited::before {
    content: ""; display: inline-block; width: 6px; height: 6px; margin-right: 6px;
    border-radius: 50%; background: var(--orange); vertical-align: 1px;
  }

  /* --- Робоча зона: кадр і список -------------------------------------- */

  main {
    display: grid; min-height: 0;
    grid-template-columns: minmax(0, 1fr) minmax(400px, 34%);
  }

  .viewer {
    position: relative; min-width: 0; min-height: 0;
    display: grid; place-items: center; padding: 16px;
    background: var(--ground);
    container-type: size;
  }
  /* Кадр вписується у вільне місце, як у переглядачі монтажки: не ширший за
     колонку й не вищий за неї. Пропорцію сторінка бере з самого відео. */
  .frame {
    --ar: 1.7778;
    position: relative; aspect-ratio: var(--ar);
    width: min(100cqw, 100cqh * var(--ar));
    background: #000; border-radius: 4px; overflow: hidden;
    box-shadow: 0 0 0 1px var(--hair), 0 18px 48px var(--shade);
    container-type: inline-size;
  }
  video { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
  /* Обвідка як у вжитих субтитрах: штрих під заливкою, а не тінь, — інакше
     на світлому кадрі підпис у переглядачі читається краще, ніж читатиметься
     у готовому відео. */
  .caption {
    position: absolute; left: 0; right: 0; bottom: 7%;
    padding: 0 6%; text-align: center; pointer-events: none;
    white-space: pre-line; color: #fff;
    font-family: var(--display); font-weight: 650; line-height: 1.22;
    font-size: 4.2cqw;
    -webkit-text-stroke: .16em #000; paint-order: stroke fill;
    text-shadow: 0 .06em .12em rgba(0, 0, 0, .6);
  }
  /* Рамка гарантованої видимості: 90% кадру. Те, що виходить за неї, на
     частині екранів обріжеться — і саме там підпис найчастіше й лізе. */
  .safe {
    position: absolute; inset: 5%; pointer-events: none;
    border: 1px dashed rgba(255, 255, 255, .4); border-radius: 2px;
    display: none;
  }
  .frame.guides .safe { display: block; }
  .nofeed {
    position: absolute; inset: 0; display: none; place-items: center;
    color: rgba(255, 255, 255, .45); font-size: 12px; letter-spacing: .02em;
  }
  .frame.empty .nofeed { display: grid; }

  /* --- Список реплік ------------------------------------------------------ */

  .side {
    display: grid; grid-template-rows: auto auto minmax(0, 1fr);
    min-width: 0; min-height: 0;
    background: var(--panel); border-left: 1px solid var(--line);
  }
  .side-head {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 10px 12px; border-bottom: 1px solid var(--line);
  }

  /* Сегментований перемикач. */
  .seg {
    display: flex; gap: 2px; padding: 2px; flex: 0 0 auto;
    background: var(--fill); border: 1px solid var(--hair); border-radius: 8px;
  }
  .seg button {
    appearance: none; border: 0; cursor: pointer;
    font: 500 12px/1 var(--ui); color: var(--dim);
    background: transparent; border-radius: 6px; padding: 6px 11px;
    transition: background .12s ease, color .12s ease;
  }
  .seg button:hover { color: var(--ink); }
  .seg button[aria-selected="true"] {
    background: var(--raised); color: var(--ink);
    box-shadow: 0 1px 2px var(--shade), 0 0 0 1px var(--hair);
  }

  .tally { display: flex; gap: 4px; flex: 0 0 auto; margin-left: auto; }
  .pill {
    display: inline-flex; align-items: center; gap: 5px;
    height: 24px; padding: 0 8px; border-radius: 6px;
    color: var(--dim); font-size: 11px; font-variant-numeric: tabular-nums;
  }
  .pill b { color: var(--ink); font-weight: 600; }
  .pill .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); }
  .pill.err .dot { background: var(--red); }
  .pill.err b { color: var(--red); }
  .pill.warn .dot { background: var(--orange); }
  .pill.warn b { color: var(--orange); }
  .pill.zero { opacity: .45; }

  /* Заголовки колонок — та сама сітка, що в рядках, щоб стояти над ними. */
  .cols, .row { display: grid; grid-template-columns: 3px 28px 78px minmax(0, 1fr) 60px; gap: 0 10px; }
  .cols {
    padding: 6px 12px 6px 0; border-bottom: 1px solid var(--line);
    color: var(--faint); font-size: 10px; font-weight: 600;
    letter-spacing: .06em; text-transform: uppercase;
  }
  .cols span:nth-child(2), .cols span:last-child { text-align: right; }
  .cols span:nth-child(3) { padding-left: 6px; }

  .cues { overflow-y: auto; overscroll-behavior: contain; min-width: 0; }

  .row {
    position: relative; align-items: start;
    padding: 9px 12px 9px 0; border-bottom: 1px solid var(--hair);
    cursor: default; scroll-margin: 8px;
  }
  .row:hover { background: var(--fill); }
  .row.on { background: var(--blue-soft); }
  .row.on::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--blue) 45%, transparent);
  }
  .stripe { align-self: stretch; margin: -9px 0; background: transparent; }
  .row.warning .stripe { background: var(--orange); }
  .row.error .stripe { background: var(--red); }
  .row > .num { color: var(--faint); font-size: 11px; text-align: right; padding-top: 6px; }
  .row.on > .num { color: var(--blue); }

  .times { display: flex; flex-direction: column; gap: 2px; }
  .times input {
    width: 100%; height: 22px; padding: 0 5px;
    font-family: var(--mono); font-size: 11px; font-variant-numeric: tabular-nums;
    color: var(--ink); background: transparent;
    border: 1px solid transparent; border-radius: 5px;
  }
  .times input + input { color: var(--dim); }
  .times input:hover { background: var(--fill); }
  .times input:focus {
    background: var(--surface); color: var(--ink); border-color: var(--blue); outline: none;
  }
  .times .dur { color: var(--faint); font-size: 10px; font-family: var(--mono); padding: 1px 6px 0; }

  .body { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .row textarea {
    display: block; width: 100%; resize: none; overflow: hidden;
    font: 400 13px/1.42 var(--ui); color: var(--ink);
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 3px 6px; margin: 0 -7px; width: calc(100% + 14px);
  }
  .row textarea:hover { background: var(--fill); }
  .row textarea:focus {
    background: var(--surface); border-color: var(--blue); outline: none;
    box-shadow: 0 0 0 3px var(--blue-soft);
  }

  .why { display: flex; flex-direction: column; gap: 2px; font-size: 11px; line-height: 1.35; }
  .why:empty { display: none; }
  .why .warning { color: var(--orange); }
  .why .error { color: var(--red); }

  /* Метрики праворуч: швидкість — головна, бо саме вона найчастіше ламає
     репліку; символи й рядки — довідково під нею. */
  .meters {
    display: flex; flex-direction: column; align-items: flex-end; gap: 4px;
    padding-top: 4px; color: var(--faint); font-size: 10px;
  }
  .meters .cps { font-size: 12px; color: var(--dim); }
  .meters .over { color: var(--red); }
  .meters .near { color: var(--orange); }
  .bar { width: 100%; height: 3px; border-radius: 2px; background: var(--fill-hi); overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--green); border-radius: 2px; }
  .bar i.near { background: var(--orange); }
  .bar i.over { background: var(--red); }

  .acts {
    position: absolute; bottom: 6px; right: 8px; z-index: 1;
    display: flex; gap: 1px; padding: 2px;
    background: var(--raised); border: 1px solid var(--line); border-radius: 7px;
    box-shadow: 0 4px 14px var(--shade);
    opacity: 0; pointer-events: none; transition: opacity .1s ease;
  }
  .row:hover .acts, .acts:focus-within { opacity: 1; pointer-events: auto; }
  .acts button {
    appearance: none; border: 0; cursor: pointer; border-radius: 5px;
    display: grid; place-items: center; width: 24px; height: 22px;
    background: transparent; color: var(--dim);
  }
  .acts button svg { width: 14px; height: 14px; }
  .acts button:hover { background: var(--fill-hi); color: var(--ink); }
  .acts button.danger:hover { background: color-mix(in srgb, var(--red) 20%, transparent); color: var(--red); }

  .blank { padding: 40px 20px; color: var(--dim); text-align: center; }

  /* --- Таймлайн ---------------------------------------------------------- */

  .timeline {
    display: flex; flex-direction: column; min-width: 0;
    background: var(--panel); border-top: 1px solid var(--line);
  }
  .transport {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    padding: 6px 10px; border-bottom: 1px solid var(--hair);
  }
  .transport .btn.play { width: 34px; }
  .transport .play .i-pause, .transport .play.playing .i-play { display: none; }
  .transport .play.playing .i-pause { display: block; }
  .clock {
    display: flex; align-items: baseline; gap: 6px; padding: 0 8px; min-width: 150px;
  }
  .clock .tc { font-size: 15px; font-weight: 500; letter-spacing: -.01em; }
  .clock .tc.of { font-size: 11px; color: var(--faint); font-weight: 400; }
  .switch {
    display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 6px;
    color: var(--dim); font-size: 12px; cursor: pointer; user-select: none;
  }
  .switch input { accent-color: var(--blue); margin: 0; }
  .switch:hover { color: var(--ink); }

  .ribbon {
    position: relative; height: 112px;
    overflow-x: auto; overflow-y: hidden; overscroll-behavior-x: contain;
    background: var(--ground);
  }
  .track {
    position: relative; height: 100%; min-width: 100%; cursor: text;
    background: linear-gradient(var(--ruler) 0 24px, transparent 24px);
  }
  /* Межа лінійки: підписи стоять над нею, блоки — під нею. */
  .track::before {
    content: ""; position: absolute; left: 0; right: 0; top: 24px;
    border-top: 1px solid var(--line); pointer-events: none;
  }
  .tick { position: absolute; top: 18px; height: 6px; border-left: 1px solid var(--line); pointer-events: none; }
  .tick.lead { top: 10px; bottom: 0; height: auto; border-left-color: var(--hair); }
  .tick.lead::before {
    content: ""; position: absolute; top: 0; left: -1px; height: 14px;
    border-left: 1px solid var(--faint);
  }
  .tick span {
    position: absolute; top: -6px; left: 5px;
    font-family: var(--mono); font-size: 10px; color: var(--faint);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }

  .blk {
    position: absolute; top: 34px; height: 66px; min-width: 3px;
    border-radius: 5px; overflow: hidden; cursor: grab;
    background: var(--clip);
    border: 1px solid color-mix(in srgb, var(--clip-edge) 55%, transparent);
    border-left: 2px solid var(--clip-edge);
    padding: 5px 7px; color: var(--ink);
    font-size: 11px; line-height: 1.3;
    transition: box-shadow .12s ease, filter .12s ease;
  }
  .blk span {
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3;
    overflow: hidden; pointer-events: none; overflow-wrap: anywhere;
  }
  .blk:hover { filter: brightness(1.12); }
  .blk:active { cursor: grabbing; }
  .blk.thin { padding: 0; }
  .blk.thin span { display: none; }
  .blk.warning {
    background: color-mix(in srgb, var(--orange) 24%, var(--surface));
    border-color: color-mix(in srgb, var(--orange) 50%, transparent);
    border-left-color: var(--orange);
  }
  .blk.error {
    background: color-mix(in srgb, var(--red) 26%, var(--surface));
    border-color: color-mix(in srgb, var(--red) 55%, transparent);
    border-left-color: var(--red);
  }
  .blk.on { box-shadow: 0 0 0 2px var(--ink); z-index: 2; }
  .blk .grip {
    position: absolute; top: 0; bottom: 0; width: 7px; cursor: ew-resize;
    background: transparent;
  }
  .blk .grip.l { left: 0; }
  .blk .grip.r { right: 0; }
  .blk:hover .grip::after {
    content: ""; position: absolute; top: 50%; width: 2px; height: 18px;
    margin-top: -9px; border-radius: 1px; background: color-mix(in srgb, var(--ink) 60%, transparent);
  }
  .blk .grip.l::after { left: 2px; }
  .blk .grip.r::after { right: 2px; }

  .head {
    position: absolute; top: 0; bottom: 0; width: 0;
    border-left: 1px solid var(--red); pointer-events: none; z-index: 3;
  }
  .head::before {
    content: ""; position: absolute; top: 0; left: -6px;
    width: 11px; height: 12px; background: var(--red);
    clip-path: polygon(0 0, 100% 0, 100% 55%, 50% 100%, 0 55%);
  }
  /* Лінія прилипання: поки її видно, край стоїть не там, де курсор, а там,
     де сусід або головка — інакше зсув на пів пікселя читався б як промах. */
  .magnet {
    position: absolute; top: 24px; bottom: 0; width: 0;
    border-left: 1px solid var(--green); pointer-events: none; z-index: 4; display: none;
    box-shadow: 0 0 6px var(--green);
  }
  .magnet.show { display: block; }

  /* --- Рядок стану з клавішами ---------------------------------------------- */

  .keys {
    display: flex; align-items: center; gap: 4px 16px; overflow: hidden; white-space: nowrap;
    padding: 0 14px; color: var(--faint); font-size: 11px;
    background: var(--panel); border-top: 1px solid var(--hair);
  }
  .keys kbd {
    font-family: var(--mono); font-size: 10px; color: var(--dim);
    background: var(--fill); border: 1px solid var(--hair); border-bottom-width: 2px;
    border-radius: 4px; padding: 0 4px; margin-right: 4px;
  }

  /* --- Смуга збереження і сповіщення ------------------------------------ */

  .ask {
    position: fixed; left: 50%; bottom: 190px; transform: translateX(-50%);
    display: none; align-items: center; gap: 12px; z-index: 20;
    padding: 10px 10px 10px 16px; border-radius: 10px;
    background: var(--raised); border: 1px solid var(--line);
    box-shadow: 0 12px 40px var(--shade);
    font-size: 12px; max-width: min(600px, 92vw);
  }
  .ask::before {
    content: ""; width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto;
    background: var(--orange);
  }
  .ask.show { display: flex; }
  .toast {
    position: fixed; left: 50%; bottom: 190px; transform: translateX(-50%) translateY(8px);
    z-index: 21; padding: 8px 14px; border-radius: 8px;
    background: var(--glass); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--line); box-shadow: 0 8px 28px var(--shade);
    font-size: 12px; opacity: 0; pointer-events: none;
    transition: opacity .18s ease, transform .18s ease;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* --- Вузький екран: усе в стовпчик, сторінка гортається ------------------ */

  @media (max-width: 1000px) {
    body { overflow: auto; display: block; }
    main { grid-template-columns: 1fr; }
    .viewer { container-type: inline-size; padding: 12px; }
    .frame { width: 100%; }
    .side { border-left: 0; border-top: 1px solid var(--line); }
    .cues { max-height: 70vh; }
    .timeline { position: sticky; bottom: 0; z-index: 6; }
    .keys { display: none; }
    .ask, .toast { bottom: 24px; }
  }
</style>
</head>
<body>

<div class="toolbar">
  <div class="mark" aria-hidden="true">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 6h10M3 10h6"/></svg>
  </div>
  <div class="doc">
    <b>__TITLE__</b>
    <span id="stamp">читаю…</span>
  </div>
  <div class="grow"></div>
  <button class="btn ghost icon" id="theme" title="Світла або темна тема" aria-label="Тема">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5.5"/><path d="M8 2.5a5.5 5.5 0 0 1 0 11z" fill="currentColor"/></svg>
  </button>
  <button class="btn" id="rewrap" title="Перескласти рядки за лімітами">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11M2.5 8h9a2 2 0 0 1 0 4H8m1.5-1.5L8 12l1.5 1.5M2.5 12h3"/></svg>
    Перерозкласти
  </button>
  <button class="btn primary" id="save" disabled title="Ctrl+S">Зберегти</button>
</div>

<main>
  <section class="viewer">
    <div class="frame" id="frame">
      <video id="video" src="/video" preload="metadata" playsinline></video>
      <div class="nofeed">Відео не читається — текст і таймкоди правити можна</div>
      <div class="safe"></div>
      <div class="caption" id="caption"></div>
    </div>
  </section>

  <section class="side">
    <div class="side-head">
      <div class="seg" role="tablist" id="seg">
        <button role="tab" aria-selected="true" data-filter="all">Усі</button>
        <button role="tab" aria-selected="false" data-filter="bad">Проблемні</button>
      </div>
      <div class="tally" id="tally"></div>
    </div>
    <div class="cols" aria-hidden="true">
      <span></span><span>№</span><span>Час</span><span>Текст</span><span title="Символів за секунду">Темп</span>
    </div>
    <div class="cues" id="list"></div>
  </section>
</main>

<section class="timeline">
  <div class="transport">
    <button class="btn icon play" id="play" title="Грати / пауза (Пробіл)" aria-label="Грати">
      <svg class="i-play" viewBox="0 0 16 16" fill="currentColor"><path d="M4.5 2.8v10.4a.6.6 0 0 0 .9.5l8.2-5.2a.6.6 0 0 0 0-1L5.4 2.3a.6.6 0 0 0-.9.5z"/></svg>
      <svg class="i-pause" viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="2.5" width="3" height="11" rx=".8"/><rect x="9.5" y="2.5" width="3" height="11" rx=".8"/></svg>
    </button>
    <div class="clock">
      <span class="tc" id="clock">0:00.00</span>
      <span class="tc of" id="total">/ —</span>
    </div>
    <span class="sep"></span>
    <button class="btn ghost" id="split" title="Розрізати вибрану репліку по головці">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="4" cy="4.5" r="2"/><circle cx="4" cy="11.5" r="2"/><path d="M5.7 5.6 13.5 12M5.7 10.4 13.5 4"/></svg>
      Розділити
    </button>
    <div class="grow"></div>
    <label class="switch"><input type="checkbox" id="guides">Рамка кадру</label>
    <label class="switch"><input type="checkbox" id="follow" checked>Стежити</label>
    <span class="sep"></span>
    <button class="btn ghost icon on" id="magnet" title="Прилипання до сусідів і головки" aria-pressed="true">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3.5 2.5v5a4.5 4.5 0 0 0 9 0v-5"/><path d="M3.5 5h2.5M10 5h2.5" /><path d="M6 2.5v5a2 2 0 0 0 4 0v-5"/></svg>
    </button>
    <button class="btn ghost icon" id="out" title="Віддалити" aria-label="Віддалити">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M5 7h4M10.5 10.5 14 14"/></svg>
    </button>
    <button class="btn ghost icon" id="in" title="Наблизити" aria-label="Наблизити">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M5 7h4M7 5v4M10.5 10.5 14 14"/></svg>
    </button>
    <button class="btn ghost" id="fit" title="Показати все відео">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8h12M4.5 5.5 2 8l2.5 2.5M11.5 5.5 14 8l-2.5 2.5"/></svg>
      Умістити
    </button>
  </div>

  <div class="ribbon" id="ribbon">
    <div class="track" id="track">
      <div class="magnet" id="magnetLine"></div>
      <div class="head" id="head"></div>
    </div>
  </div>
</section>

<div class="keys">
  <span><kbd>Пробіл</kbd>грати</span>
  <span><kbd>↑</kbd><kbd>↓</kbd>репліка</span>
  <span><kbd>←</kbd><kbd>→</kbd>перемотка</span>
  <span><kbd>[</kbd><kbd>]</kbd>межа по головці</span>
  <span><kbd>Enter</kbd>правити текст</span>
  <span><kbd>Ctrl</kbd>+колесо — масштаб</span>
  <span><kbd>Ctrl+Z</kbd>відкотити</span>
  <span><kbd>Ctrl+S</kbd>зберегти</span>
</div>

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

// Іконки дій у рядку. Розмітка своя, не з даних, тож innerHTML тут безпечний.
const SVG = "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>";
const ICONS = {
  split: SVG + "<circle cx='4' cy='4.5' r='2'/><circle cx='4' cy='11.5' r='2'/><path d='M5.7 5.6 13.5 12M5.7 10.4 13.5 4'/></svg>",
  merge: SVG + "<path d='M3 3v3.5a2 2 0 0 0 2 2h6a2 2 0 0 1 2 2V13M3 13v-2.5M10.5 11 13 13.5 15.5 11'/></svg>",
  drop: SVG + "<path d='M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5'/></svg>",
};

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
  from.title = "Початок · ↑↓ крок 0,05 с, із Shift — 1 с";
  to.title = "Кінець · ↑↓ крок 0,05 с, із Shift — 1 с";
  const dur = document.createElement("span");
  dur.className = "dur";
  times.append(from, to, dur);

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

  body.append(area, why);
  row.append(stripe, num, times, body, meters, actions(i));
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
  const make = (icon, hint, kind, run) => {
    const button = document.createElement("button");
    button.innerHTML = ICONS[icon];
    button.title = hint;
    button.setAttribute("aria-label", hint);
    if (kind) button.className = kind;
    button.onclick = run;
    box.append(button);
    return button;
  };
  make("split", "Розрізати репліку по головці", "", () => split(i));
  make("merge", "Приєднати наступну репліку", "", () => merge(i));
  make("drop", "Прибрати репліку", "danger", () => drop(i));
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

  const rate = document.createElement("span");
  const speed = mark.cps === null ? null : mark.cps;
  const over = speed !== null && speed > limits.max_cps;
  const near = speed !== null && !over && speed > limits.max_cps * 0.9;
  rate.className = "num cps" + (over ? " over" : near ? " near" : "");
  rate.title = "Швидкість читання, символів за секунду; стеля " + limits.max_cps;
  rate.textContent = speed === null ? "∞" : speed.toFixed(1);

  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("i");
  fill.className = over ? "over" : near ? "near" : "";
  fill.style.width = clamp(((speed === null ? limits.max_cps * 2 : speed) / limits.max_cps) * 100, 0, 100) + "%";
  bar.append(fill);

  const chars = document.createElement("span");
  chars.className = "num" + (mark.chars > budget ? " over" : "");
  chars.title = "Символів із " + budget;
  chars.textContent = mark.chars + "/" + budget;

  const lines = document.createElement("span");
  lines.className = "num" + (mark.lines.length > limits.max_lines ? " over" : "");
  lines.title = "Рядків із " + limits.max_lines;
  lines.textContent = mark.lines.length + "/" + limits.max_lines + " ряд";

  box.append(rate, bar, chars, lines);
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
  const width = (cue.end - cue.start) * pps;
  parts.thin = width < THIN_PX;
  parts.block.className = blockClass(i, parts);
  parts.block.style.left = (cue.start * pps) + "px";
  parts.block.style.width = Math.max(3, width) + "px";
  parts.block.title = clockText(cue.start) + " → " + clockText(cue.end);
  if (parts.label.textContent !== cue.text) parts.label.textContent = cue.text;
}

function placeBlocks() {
  for (const i of blocks.keys()) placeBlock(i);
}

// Приїхали свіжі мітки — змінився лише колір, не геометрія.
function paintLevels() {
  for (const [i, parts] of blocks) parts.block.className = blockClass(i, parts);
}

// Вужчий за це блок — лише кольорова риска: два-три обрізані символи в ньому
// не читаються, а на загальному плані зливаються в суцільну кашу з літер.
const THIN_PX = 34;

function blockClass(i, parts) {
  const mark = marks[i];
  return "blk " + (mark ? mark.level : "ok") + (parts.thin ? " thin" : "") + (i === picked ? " on" : "");
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

// Стартовий масштаб — робочий, а не загальний план: на чотирнадцяти хвилинах
// «умістити все» дає сотні рисок без тексту. Сорок секунд у вікні — це кілька
// реплік, які видно й можна тягнути; коротке відео вміщається цілим.
const OPENING_SPAN = 40;

function settle() {
  const whole = (ribbon.clientWidth - 8) / duration();
  pps = clamp(Math.max(whole, ribbon.clientWidth / OPENING_SPAN), 0.5, 400);
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
  $("play").classList.toggle("playing", true);
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(loop);
});
video.addEventListener("pause", () => { $("play").classList.toggle("playing", false); });
video.addEventListener("seeked", () => { if (!ticking) tick(); });
video.addEventListener("timeupdate", () => { if (!ticking) tick(); });
video.addEventListener("loadedmetadata", () => {
  total.textContent = "/ " + clockText(video.duration);
  // Кадр бере пропорцію з самого відео: вертикальний ролик у рамці 16:9
  // показав би підпис не там, де його побачить глядач.
  if (video.videoWidth && video.videoHeight) {
    $("frame").style.setProperty("--ar", String(video.videoWidth / video.videoHeight));
  }
  settle();
  tick();
});
video.addEventListener("error", () => {
  // Відео може бути ще не змонтоване або лежати не там. Текст і таймкоди від
  // цього не залежать, тому редактор лишається робочим — але мовчати не варто.
  total.textContent = "/ немає відео";
  $("frame").classList.add("empty");
  settle();
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
    // Відео могло відмовити ще до реплік — тоді масштаб рахували по порожній
    // довжині, і його треба перерахувати по самих репліках.
    if (isFinite(video.duration) && video.duration > 0) layout(); else settle();
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
