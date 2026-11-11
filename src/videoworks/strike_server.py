"""Сервер викреслювача: сторінка, стан, відео й запис позначок.

Сервер нічого не знає про те, як із позначок виходять відрізки глушіння: `load`
віддає стан як непрозорий словник, а `store` повертає те, що вийшло після запису.
Правила живуть у `strikes`, і другої їх копії тут бути не повинно — сервер лише
перевіряє, що тіло запиту має сенс, і не дає затерти файл, якого сторінка не бачила.

Тільки 127.0.0.1 і тільки стандартна бібліотека — як і в `studio`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path

from videoworks import preview
from videoworks.studio import BadRequest
from videoworks.studio import _Handler as _StudioHandler

# Єдині маршрути статики. Шлях із запиту до файлової системи не доходить узагалі:
# ім'я файлу бере таблиця, а не користувач, тож вийти за каталог сторінки нікуди.
STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/logic.js": ("logic.js", "text/javascript; charset=utf-8"),
    "/brush.js": ("brush.js", "text/javascript; charset=utf-8"),
    "/player.js": ("player.js", "text/javascript; charset=utf-8"),
    "/sheet.js": ("sheet.js", "text/javascript; charset=utf-8"),
    "/sound.js": ("sound.js", "text/javascript; charset=utf-8"),
    "/map.js": ("map.js", "text/javascript; charset=utf-8"),
    "/manual.js": ("manual.js", "text/javascript; charset=utf-8"),
    "/tour.js": ("tour.js", "text/javascript; charset=utf-8"),
    "/help.js": ("help.js", "text/javascript; charset=utf-8"),
    "/notes.js": ("notes.js", "text/javascript; charset=utf-8"),
    "/saving.js": ("saving.js", "text/javascript; charset=utf-8"),
    "/keys.js": ("keys.js", "text/javascript; charset=utf-8"),
    "/memory.js": ("memory.js", "text/javascript; charset=utf-8"),
}


def _is_number(value: object) -> bool:
    # bool — підклас int, і `true` у JSON не повинно сходити за слово чи секунду.
    return isinstance(value, int | float) and not isinstance(value, bool)


def read_intent(
    body: object, *, word_count: int, duration: float
) -> tuple[list[int], list[tuple[float, float]]]:
    """Розбирає позначки зі сторінки: індекси слів і ручні відрізки.

    Кидає BadRequest на будь-яке відхилення: тіло приходить із браузера, і
    половина прийнятих позначок з мовчазною втратою решти гірша за відмову.
    """
    if not isinstance(body, dict):
        raise BadRequest("очікується обʼєкт")

    words = body.get("words")
    if not isinstance(words, list):
        raise BadRequest("words має бути списком")
    for index in words:
        if not isinstance(index, int) or isinstance(index, bool):
            raise BadRequest("words містить не ціле число")
        if not 0 <= index < word_count:
            raise BadRequest("слова з таким номером немає")
    if len(set(words)) != len(words):
        raise BadRequest("words містить повтори")

    raw = body.get("manual", [])
    if not isinstance(raw, list):
        raise BadRequest("manual має бути списком")
    manual: list[tuple[float, float]] = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise BadRequest("відрізок має бути парою [початок, кінець]")
        if not all(_is_number(edge) for edge in pair):
            raise BadRequest("межі відрізка мають бути числами")
        try:
            start, end = float(pair[0]), float(pair[1])
        except OverflowError as exc:
            # Ціле на сотні цифр JSON читає без скарг, а float() на ньому падає.
            raise BadRequest("межа відрізка завелика") from exc
        # NaN проходить float() мовчки й ламає всі порівняння; Infinity JSON
        # у браузері не прочитає. Обидва відсіює isfinite.
        if not (math.isfinite(start) and math.isfinite(end)):
            raise BadRequest("межа відрізка не є скінченним числом")
        if not 0 <= start < end <= duration:
            raise BadRequest("відрізок має лежати в межах відео, початок раніше за кінець")
        manual.append((start, end))

    return words, manual


class _Handler(_StudioHandler):
    def __init__(
        self,
        *args,
        video: Path,
        load: Callable[[], dict],
        store: Callable[[list[int], list[tuple[float, float]]], dict],
        stamp: Callable[[], str],
        **kwargs,
    ) -> None:
        self._video = video
        self._load = load
        self._store = store
        self._stamp = stamp
        # Конструктор студії вимагає її власних аргументів, яких тут немає, —
        # тому йдемо в обхід нього, прямо до базового обробника.
        super(_StudioHandler, self).__init__(*args, **kwargs)

    # Імена методів диктує BaseHTTPRequestHandler.
    def do_GET(self) -> None:
        route = self.path.partition("?")[0]
        if route in STATIC:
            name, kind = STATIC[route]
            # Читаємо на кожен запит: правка сторінки видна без перезапуску.
            self._send((files("videoworks") / "web" / "strike" / name).read_bytes(), kind)
        elif route == "/state.json":
            self._json({**self._load(), "stamp": self._stamp()})
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
        if self.path.partition("?")[0] != "/strikes":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        state = self._load()
        try:
            body = self._read_body()
            words, manual = read_intent(
                body, word_count=len(state["words"]), duration=state["duration"]
            )
        except BadRequest as exc:
            # `close_connection` ставить `_read_body`, коли тіло лишилось
            # недочитаним: тоді відповідь — остання в цьому зʼєднанні.
            self._json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
                close=self.close_connection,
            )
            return

        # Сервер багатопотоковий: без замка дві вкладки з тим самим відбитком
        # обидві пройшли б перевірку, і друга мовчки затерла б першу.
        with self.server.save_lock:
            self._save(body, state, words, manual)

    def _save(
        self, body: dict, state: dict, words: list[int], manual: list[tuple[float, float]]
    ) -> None:
        current = self._stamp()
        sent = body.get("stamp")
        force = body.get("force") is True

        # Інша розшифровка — це інший файл: примусовий запис затер би чужі
        # позначки словами, яким вони не належать, тому `force` тут не діє.
        # Решта — відбиток: файл могли перезібрати, поки сторінка була відкрита.
        # Відсутній відбиток вважаємо застарілим: сторінка шле його завжди, а
        # відмовити безпечніше, ніж мовчки затерти чиїсь позначки.
        if body.get("transcript") != state["transcript"] or (
            not force and not (isinstance(sent, str) and sent == current)
        ):
            self._json({"saved": False, "moved": True, "stamp": current})
            return

        try:
            written = self._store(words, manual)
        except OSError as exc:
            # Диск, права, зайнятий файл — сторінка має побачити причину, а не
            # HTML-сторінку 500, на якій її response.json() спіткнеться.
            self._json(
                {"error": f"не вдалось записати: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self._json({"saved": True, **written, "stamp": self._stamp()})


def build_server(
    *,
    video: Path,
    load: Callable[[], dict],
    store: Callable[[list[int], list[tuple[float, float]]], dict],
    stamp: Callable[[], str],
    port: int = 8772,
) -> preview.Server:
    """Піднімає викреслювач на 127.0.0.1. Порт 0 — вибрати вільний самому.

    `load` читає стан із диска, `store` пише позначки й повертає, що вийшло,
    `stamp` віддає відбиток файлу — за ним видно, що файл перезібрали.
    """
    handler = partial(_Handler, video=video, load=load, store=store, stamp=stamp)
    server = preview.Server(("127.0.0.1", port), handler)
    # Один замок на сервер, а не на обробник: обробник живе один запит.
    server.save_lock = threading.Lock()
    return server
