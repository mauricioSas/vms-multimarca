"""Versiones del producto: SemVer 2.0 (`MAJOR.MINOR.PATCH[-pre][+build]`) con su orden de precedencia.

Sin dependencias (el runtime del actualizador no lleva `packaging`). Las versiones de componentes
(`3.12.10-r4`, `rfdetr-2026.10`) son etiquetas: solo se comparan por igualdad.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()
    text: str = ""

    @classmethod
    def parse(cls, value: str) -> "Version":
        m = _SEMVER.match(value.strip()) if isinstance(value, str) else None
        if not m:
            raise ValueError(f"Versión no válida: {value!r} (se espera X.Y.Z)")
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, value.strip())

    def _key(self) -> tuple[object, ...]:
        # Sin prerrelease va DESPUÉS que con prerrelease (2.0.0-rc.1 < 2.0.0).
        pre_key: tuple[object, ...]
        if not self.pre:
            pre_key = (1,)
        else:
            parts: list[tuple[int, int, str]] = []
            for p in self.pre:
                parts.append((0, int(p), "") if p.isdigit() else (1, 0, p))
            pre_key = (0, tuple(parts))
        return (self.major, self.minor, self.patch, pre_key)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self.text or f"{self.major}.{self.minor}.{self.patch}" + (f"-{'.'.join(self.pre)}" if self.pre else "")


def is_valid(value: str) -> bool:
    try:
        Version.parse(value)
        return True
    except ValueError:
        return False


def newer(a: str, b: str) -> bool:
    """¿`a` es estrictamente mayor que `b`?"""
    return Version.parse(a) > Version.parse(b)


def at_least(a: str, b: str) -> bool:
    return Version.parse(a) >= Version.parse(b)
