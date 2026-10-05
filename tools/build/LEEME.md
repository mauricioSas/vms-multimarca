# `tools/build/`

**Dueño:** B3.

Orquestación de la build de Windows sin depender de GitHub (PLAN-V2 §1.8). Los flujos de CI solo llaman a estas
órdenes. Guía completa: [docs/EMPAQUETADO.md](../../docs/EMPAQUETADO.md).

| Orden | Qué hace |
|---|---|
| `python -m tools.build all --version X.Y.Z [--doubles]` | nativos, runtime, motor, payload (`distribution.layout`), firma si hay certificado, SBOM e instalador |
| `python -m tools.build repro --version X.Y.Z [--doubles]` | dos builds limpias del payload y comparación de SHA-256 (§4.1) |
| `python -m tools.build inno --install` | Inno Setup 7.1.0 con SHA-256 fijado |
| `python -m tools.build sign --dist <carpeta> [--dry-run]` | firma Authenticode (jsign o signtool); sin certificado no hace nada (N1) |
| `python -m tools.build sbom --version X.Y.Z --out <archivo>` | SBOM CycloneDX 1.6 |
| `python -m tools.build pascal-check` | ayuda de desarrollo: compila el `[Code]` del instalador con Free Pascal |

Pruebas: `tests/windows/test_build_tools.py` y `tests/windows/test_layout.py`.
