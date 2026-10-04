# `distribution/runtime/`

**Dueño:** B1.

Construcción del runtime de Windows: Python 3.12 embebible + `site-packages` instalado en CI con `--require-hashes`, `.pyc` compilados con `unchecked-hash` y `MANIFEST.sha256`. Orden: `python -m distribution.runtime.build --out build/runtime`.

**Referencia:** PLAN-V2 §1.2. Reproducible (mismo SHA-256 en dos builds).
