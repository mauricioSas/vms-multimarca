"""Orquestación de la build de Windows sin depender de GitHub (PLAN-V2 §1.8). Dueño: B3.

    python -m tools.build all --version 2.0.0 --out dist        runtime, nativos, payload, SBOM e instalador
    python -m tools.build inno --install                         instala Inno Setup 7.1.0 verificado (SHA-256)
    python -m tools.build sign --dist dist                       firma Authenticode si hay certificado (N1)
    python -m tools.build sbom --out dist/sbom.cdx.json          SBOM CycloneDX a partir de los locks
    python -m tools.build repro --version 2.0.0                  dos builds limpias → mismo SHA-256 del payload

Los flujos de GitHub Actions solo llaman a estas órdenes: lo mismo funciona a mano en cualquier Windows 10/11
x64 con Python 3.12, Rust (rustup) y, para el instalador, Inno Setup 7.1.0 (``inno --install`` lo pone).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = ROOT / "distribution" / "installer"
ISS_FILE = INSTALLER_DIR / "VMSMultimarca.iss"
DOUBLES_DIR = ROOT / "tests" / "windows" / "doubles"
NATIVE_DIR = ROOT / "native"

#: Inno Setup fijado (PLAN-V2 §1.4). SHA-256 comprobado el 5-oct-2026 contra el «digest» que publica GitHub
#: para el asset de la release ``is-7_1_0`` de jrsoftware/issrc y contra la descarga directa.
INNO_VERSION = "7.1.0"
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe"
INNO_SHA256 = "0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f"
