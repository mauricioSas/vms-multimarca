# `distribution/runtime/`

**Dueño:** B1.

Construye el runtime de Windows: Python 3.12 embebible oficial (`python-3.12.10-embed-amd64.zip`, SHA-256
fijado) + `site-packages` instalado aquí con `--require-hashes`, `.pyc` compilados con `unchecked-hash` y
`MANIFEST.sha256`. En el PC del cliente nunca se ejecuta pip: instalar es copiar archivos verificados.

```
python -m distribution.runtime.build --out build/runtime                       # vms + analytics + central + extra
python -m distribution.runtime.build --out build/runtime-min --requirements requirements-vms.txt
python -m distribution.runtime.build --out build/runtime --verify              # comprueba MANIFEST.sha256
```

- Funciona en Windows, macOS y Linux: los marcadores de los locks (`; sys_platform == "win32"`) se evalúan
  **para Windows**, no para el equipo que construye.
- `python312._pth`: `python312.zip`, `.`, `Lib\site-packages`, `..\app` (el código de la versión vive en
  `versions\<X>\app`, PLAN-V2 §2.4) e `import site`. El intérprete no mira `PYTHONPATH` ni el registro.
- Los `.pyc` llevan rutas relativas (`Lib/site-packages/…`) y se compilan con `PYTHONHASHSEED=0`: dos
  construcciones con las mismas entradas dan el mismo `MANIFEST.sha256` (prueba en
  `tests/distribution/test_runtime_build.py`). Los servicios corren con `PYTHONDONTWRITEBYTECODE=1`.
- Sin red: `--python-zip <zip>` y `--find-links <carpeta de wheels> --no-index`.
- `requirements-windows-extra.txt`: dependencias solo de Windows además de los locks de sede (hoy ninguna:
  DPAPI, Job Objects y `MoveFileExW` se usan con `ctypes`).

**Pendiente (no se hace aquí):** comprobar la firma Authenticode de `python.exe`/`python312.dll` (la PSF los
firma) en el CI de Windows; el componente `app` (código del producto) lo monta `distribution/layout.py` (B3).

**Referencia:** PLAN-V2 §1.2 y §2.4.
