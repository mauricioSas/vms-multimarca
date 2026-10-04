# `distribution/`

**Dueño:** B1 (`runtime/`) y B3 (`installer/`, `layout.py`).

Todo lo que convierte el código en un payload por versión y en el instalador.

**Referencia:** PLAN-V2 §1.2, §1.4 y §2.4.

> **Nombre de la carpeta:** el plan la llamaba `packaging/`, pero ese nombre tapa el paquete `packaging` de PyPI (lo usan pip, pytest y `tools.lock_requirements`) en cuanto la carpeta tenga `__init__.py` para `python -m …`. Por eso es `distribution/` (decisión de la fase 0, PLAN-V2 §9).
