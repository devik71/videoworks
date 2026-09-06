from __future__ import annotations

import pytest

from videoworks.text import plural


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 сцена"),
        (2, "2 сцени"),
        (4, "4 сцени"),
        (5, "5 сцен"),
        (0, "0 сцен"),
        (11, "11 сцен"),  # не «11 сцена»
        (12, "12 сцен"),
        (14, "14 сцен"),
        (21, "21 сцена"),
        (22, "22 сцени"),
        (25, "25 сцен"),
        (101, "101 сцена"),
        (111, "111 сцен"),
    ],
)
def test_plural_follows_ukrainian_rules(count: int, expected: str) -> None:
    assert plural(count, "сцена", "сцени", "сцен") == expected
