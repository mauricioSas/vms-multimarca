# Empaquetado y distribución

Cómo se compila el instalador de Windows de la v2 (`VMSMultimarca-Setup-X.Y.Z.exe`), con o sin GitHub. La
instalación en sí está en [INSTALACION-WINDOWS.md](INSTALACION-WINDOWS.md); el diseño, en
[PLAN-V2.md](PLAN-V2.md) §1.2-§1.8 y §4.1.

## 1. Qué se genera

| Pieza | De dónde sale | Quién |
|---|---|---|
| `vmshost.exe`, `vmsctl.exe` | `native/` (`cargo build --release`) | B1 |
| `VMS.exe` (visor) | `native/viewer` (`cargo tauri build --no-bundle`) | B2 |
| Runtime de Python 3.12 (`runtime\`) | `python -m distribution.runtime.build` | B1 |
| Motor (`mediamtx.exe` + licencia) | `python -m tools.fetch_mediamtx --platform windows_amd64` (SHA-256 de la publicación) | — |
| Payload por versión y zips de componentes | `python -m distribution.layout` | B3 |
| SBOM CycloneDX | `python -m tools.build sbom` | B3 |
| Instalador | Inno Setup 7.1.0 (`distribution/installer/VMSMultimarca.iss`) | B3 |

`python -m tools.build all` hace todo en orden. Los flujos de GitHub Actions solo llaman a estas órdenes, así que
lo mismo funciona a mano en cualquier Windows 10/11 x64.

## 2. Compilar en un Windows sin GitHub

Requisitos: Python 3.12 (`py -3.12`), Rust con `rustup` (el toolchain se fija solo con `rust-toolchain.toml`) e
Internet la primera vez (dependencias, MediaMTX e Inno Setup; todo se verifica con SHA-256).

```powershell
cd C:\src\vms-multimarca
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --require-hashes --no-deps -r requirements-vms.txt
.venv\Scripts\python -m pip install --no-deps -e .

# 1. Inno Setup 7.1.0 (descarga verificada; se instala en C:\Program Files\Inno Setup 7)
.venv\Scripts\python -m tools.build inno --install

# 2. Todo: nativos, runtime, payload, SBOM e instalador → dist\
.venv\Scripts\python -m tools.build all --version 2.0.0 --out dist

# 3. Comprobar que el payload es reproducible (dos builds limpias, mismo SHA-256)
.venv\Scripts\python -m tools.build repro --version 2.0.0 --out dist\repro

# 4. Firma: «all» firma el payload antes de compilar si hay certificado (decisión N1: todavía no).
#    Para ver qué PE se firmarían:
.venv\Scripts\python -m tools.build sign --dist dist\layout\payload --dry-run

# 5. e2e (¡en una VM de pruebas! instala y desinstala de verdad)
.venv\Scripts\python tests\windows\e2e_requirements.py > e2e-req.txt
.venv\Scripts\python -m pip install --no-deps -r e2e-req.txt
.venv\Scripts\python -m tests.windows.run_e2e --installer dist\VMSMultimarca-Setup-2.0.0.exe
```

Mientras B1 y B2 no entregan sus binarios, añade `--doubles` a `all` y `repro`: el payload lleva los dobles de
`tests/windows/doubles` (un `vmsctl.exe` que registra cada llamada, un `vmshost.exe` que solo abre el visor y un
visor que solo abre una ventana) y un runtime de prueba sin Python. El instalador resultante es una **build de
prueba** (`TestBuild`): admite parámetros de simulación (`/SIMULATEWINBUILD=`, `/MINFREEGB=`, `/CAPTURESTATE=`)
que no existen en una build de publicación. **Nunca** se entrega a un cliente.

Salida en `dist\`:

| Archivo | Qué es |
|---|---|
| `VMSMultimarca-Setup-X.Y.Z.exe` | el instalador |
| `layout\payload\` | lo que copia el instalador (`bin\`, `versions\X.Y.Z\`, `updater\slot-a\`) |
| `layout\components\*.zip`, `layout\components.json` | componentes del actualizador (§2.7), cada zip con `MANIFEST.sha256` |
| `layout\payload-manifest.json` | SHA-256 de cada archivo del payload: el e2e lo compara con lo instalado |
| `sbom-X.Y.Z.cdx.json` | SBOM CycloneDX 1.6 (locks de sede, crates y binarios de terceros) |
| `build-info.json` | resumen: versión, fecha, dobles sí/no, estado de la firma |
| `iscc.log` | salida del compilador de Inno |

## 3. Reproducibilidad (PLAN-V2 §4.1)

- **Se exige** que los zips de `app`, `runtime`, `models` y `updater` salgan con el mismo SHA-256 en dos builds
  limpias con la misma entrada: orden fijo, fecha `SOURCE_DATE_EPOCH` (por defecto, la del último commit),
  permisos fijos, `.pyc` compilados con `unchecked-hash` y ruta de origen estable (`app/...`).
  `python -m tools.build repro` lo comprueba y falla si no (lo ejecuta CI en cada build).
- Los binarios de Rust se intentan reproducibles (B1); si no lo son, vale la atestación de procedencia de GitHub.
- El instalador de Inno no es reproducible (lleva fechas propias). Lo que se comprueba es que **instala
  exactamente el payload reproducible**: el e2e compara cada archivo instalado con `payload-manifest.json`.

## 4. Firma (decisión N1: sin certificado todavía)

`python -m tools.build sign` y la directiva `SignTool=` del instalador están preparadas para:

- **jsign** (producción, con Certum o la llave física): `VMS_JSIGN_JAR`, `VMS_SIGN_STORETYPE`,
  `VMS_SIGN_KEYSTORE`, `VMS_SIGN_ALIAS`, `VMS_SIGN_STOREPASS` (se pasa como `env:`, nunca por la línea de
  órdenes) y `VMS_SIGN_TSA` (sello de tiempo RFC 3161, obligatorio).
- **signtool** con un `.pfx` de prueba (CI): `VMS_SIGN_PFX` y `VMS_SIGN_PFX_PASSWORD`.

Sin esas variables no se firma nada y la orden lo dice. Con ellas, `tools.build all --sign` firma también el
instalador y el desinstalador (ISCC llama a `python -m tools.build sign-file`). Se firman todos los PE nuestros o
redistribuidos sin firma; `python.exe` de la PSF ya viene firmado y no se toca.

## 5. En GitHub Actions

| Flujo | Cuándo | Qué hace |
|---|---|---|
| `ci.yml` → `b3-instalador` | cada push | dobles (fmt, clippy para Windows, pruebas), mypy estricto de `distribution` y `tools.build`, recursos del asistente y actionlint |
| `ci.yml` → `b3-windows` | push que toca el instalador | `windows-installer.yml` completo |
| `build.yml` | PR a `main`, etiquetas `v*`, a mano | build + reproducibilidad + SBOM; en etiquetas, atestación de procedencia |
| `e2e-windows.yml` | `main` y etiquetas | build + e2e completo |
| `windows-installer.yml` | reutilizable | Inno 7.1.0 verificado → `tools.build all` → `tools.build repro` → `run_e2e` (v1 → v2, asistente con capturas, pasos 1-5 y 10-12). Artefactos: `instalador`, `instalador-capturas`, `e2e-windows` |

Desde el Mac no se compila el instalador (no hay ISCC): sí se montan el payload, los zips, el SBOM y todas las
pruebas de `tests/windows` salvo el e2e. Para revisar el `[Code]` del instalador sin Windows hay una ayuda
opcional con Free Pascal: `VMS_FPC=… python -m tools.build pascal-check` (no sustituye a ISCC).

## 6. Avisos de terceros

Antes de cada entrega regenera y adjunta `THIRD_PARTY_NOTICES.txt` (va dentro de cada versión del payload):

```bash
.venv/bin/python -m deploy.third_party_notices          # escribe el archivo
.venv/bin/python -m deploy.third_party_notices --check  # lo usa la prueba automática
```

Genéralo en la plataforma de destino para que los textos de licencia correspondan a las wheels que se entregan.

## 7. Versión 1 (`install.ps1`)

La v1 se instalaba copiando la carpeta del programa y ejecutando `deploy\windows\install.ps1`, que descargaba
Python embebible, WinSW y MediaMTX. Se mantiene solo para los equipos que todavía la tienen; para pasar a la v2
basta con ejecutar el instalador nuevo encima (ver [INSTALACION-WINDOWS.md](INSTALACION-WINDOWS.md) §6). La
opción PyInstaller que se documentaba para la v1 está descartada en la v2 (PLAN-V2 §1.2).
