"""Склеювання токенів ASR у читабельний текст."""

from __future__ import annotations

import re

from videoworks.models import Word

_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:…%)\]}»])")
_AFTER_OPEN = re.compile(r"([(\[{«])\s+")
_HYPHEN = re.compile(r"\s+-(?=\w)")
_SPACES = re.compile(r"\s{2,}")


def plural(count: int, one: str, few: str, many: str) -> str:
    """1 сцена, 2 сцени, 5 сцен — інакше зведення читається як машинний вивід."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        return f"{count} {one}"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{count} {few}"
    return f"{count} {many}"


def join_words(words: list[Word]) -> str:
    """Whisper віддає пунктуацію та частини складних слів окремими токенами
    ("Word", "-level"), тому наївне склеювання пробілами дає "Word -level".
    """
    text = " ".join(word.w.strip() for word in words if word.w.strip())
    text = _BEFORE_PUNCT.sub(r"\1", text)
    text = _AFTER_OPEN.sub(r"\1", text)
    text = _HYPHEN.sub("-", text)
    return _SPACES.sub(" ", text).strip()
