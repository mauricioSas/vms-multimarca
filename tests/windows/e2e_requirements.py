"""Imprime las líneas de ``requirements-test.txt`` que necesita el e2e de Windows (pytest y sus dependencias).

El e2e no necesita Playwright ni PostgreSQL: instalar solo esto ahorra minutos en el runner de Windows y mantiene
las versiones fijadas en un único lock.

    python tests/windows/e2e_requirements.py > e2e-req.txt && python -m pip install --no-deps -r e2e-req.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KEEP = {"pytest", "pluggy", "iniconfig", "pygments", "pytest-timeout", "pytest-asyncio", "colorama"}


def lines(lock: Path) -> list[str]:
    out = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(==|>=)", line)
        if m and m.group(1).lower() in KEEP:
            out.append(line.split("  #", 1)[0].strip())
    return out


def main() -> int:
    lock = Path(__file__).resolve().parents[2] / "requirements-test.txt"
    found = lines(lock)
    missing = KEEP - {re.split(r"[=>]", x, maxsplit=1)[0].lower() for x in found}
    if missing:
        print(f"Faltan en {lock.name}: {sorted(missing)}", file=sys.stderr)
        return 1
    print("\n".join(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
