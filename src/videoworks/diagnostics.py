"""Спільний тип для перевірок: preflight монтажу, придатність субтитрів."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    level: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == "error"]


def warnings(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == "warning"]
