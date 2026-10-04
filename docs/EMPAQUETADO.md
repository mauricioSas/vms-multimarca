# Empaquetado y distribución

## Opción recomendada: carpeta + `install.ps1` / `install.sh`

Es la que usan las guías de instalación. El instalador descarga Python embebible, WinSW y
MediaMTX con versión y SHA-256 fijados, e instala las dependencias desde los archivos de bloqueo.
Ventajas: sin compilar nada, actualizaciones sencillas, código y licencias fáciles de auditar.

## Paquete sin conexión a Internet (tiendas sin salida a Internet)

En un equipo con Internet **de la misma plataforma de destino** (Windows x64 o Linux x64) y con
Python 3.12:

```powershell
# 1. Wheels de las dependencias (mismas versiones que los locks)
py -3.12 -m pip download --no-deps --only-binary=:all: -d wheelhouse `
    -r requirements-vms.txt -r requirements-analytics.txt -r requirements-central.txt

# 2. Binarios externos con el nombre que espera el instalador
mkdir deploy\windows\downloads
#   mediamtx_v1.21.1_windows_amd64.zip, WinSW.NET461.exe,
#   python-3.12.10-embed-amd64.zip, pip-26.2.1-py3-none-any.whl
#   (URL exactas en $Pinned dentro de deploy\windows\install.ps1)
```

Copia la carpeta completa (con `wheelhouse` y `deploy\windows\downloads`) y en el destino:

```powershell
.\deploy\windows\install.ps1 -WheelhouseDir .\wheelhouse
```

En Linux: `sudo deploy/linux/install.sh --wheelhouse ./wheelhouse --downloads ./descargas`.
El SHA-256 de cada binario se comprueba igual que con descarga.

> Ojo: las líneas de los locks sin versión fija (`pywin32-ctypes`, `tzdata`, `colorama` en
> Windows; `jeepney`, `secretstorage` en Linux) se resuelven al descargar. Para una entrega
> cerrada, regenera los locks en la plataforma de destino (`python -m tools.lock_requirements`).

## Instalador `.exe` (opcional)

Si el cliente prefiere un asistente gráfico, se puede envolver la carpeta con **Inno Setup**
(licencia propia de Inno Setup que permite uso comercial; revisar la versión vigente): el `.exe`
copia la carpeta a `C:\Program Files\VMSMultimarca` y ejecuta, en el paso final,
`powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -SourceDir "{app}"` con los
componentes elegidos en el asistente. El desinstalador llama a `uninstall.ps1`. Firmar el `.exe`
y los `.ps1` con un certificado de firma de código evita los avisos de SmartScreen.

## Opción PyInstaller (documentada, no recomendada por defecto)

PyInstaller puede generar ejecutables (`vms.exe`, `analytics.exe`, `central.exe`) que no
necesitan Python instalado.

**Licencia:** PyInstaller es GPL-2.0 **con excepción para el cargador (bootloader)**: los
ejecutables generados se pueden distribuir con cualquier licencia, y PyInstaller en sí no se
entrega al cliente. Aun así, por la regla del proyecto («ante la duda de licencia, no se usa»),
pide la validación del asesor legal antes de adoptarlo.

Inconvenientes frente a la opción recomendada:
- Antivirus: los ejecutables de PyInstaller generan más falsos positivos.
- OpenVINO, ONNX Runtime y OpenCV necesitan recopilar sus DLL y complementos a mano (`--collect-all`).
- Cada actualización requiere recompilar y redistribuir varios cientos de MB.
- `vms.core.paths.install_dir()` ya contempla el modo congelado (`sys.frozen`): busca `bin\mediamtx.exe`
  junto al ejecutable.

Ejemplo (en Windows, dentro del entorno del proyecto, PyInstaller instalado solo en la máquina de
compilación):

```powershell
py -3.12 -m pip install pyinstaller
pyinstaller --name vms --onedir --noconfirm `
    --add-data "vms\web;vms\web" --add-data "vms\db\migrations;vms\db\migrations" `
    --collect-submodules vms --hidden-import uvicorn.lifespan.on `
    vms\__main__.py
pyinstaller --name central --onedir --noconfirm --add-data "central\web;central\web" `
    --collect-submodules central central\__main__.py
# la analítica: añadir --collect-all openvino --collect-all onnxruntime --collect-all cv2
```

Después copia `bin\mediamtx.exe` y `bin\MEDIAMTX-LICENSE.txt` a `dist\vms\bin\`, y adapta
`install.ps1` para que el servicio ejecute `dist\vms\vms.exe` en lugar de `python -m vms`.

## Avisos de terceros

Antes de cada entrega regenera y adjunta `THIRD_PARTY_NOTICES.txt`:

```bash
.venv/bin/python -m deploy.third_party_notices          # escribe el archivo
.venv/bin/python -m deploy.third_party_notices --check  # lo usa la prueba automática
```

Genéralo en la plataforma de destino (Windows/Linux) para que los textos de licencia
correspondan a las wheels que se entregan (por ejemplo, la wheel de OpenCV de macOS incluye un
FFmpeg distinto y **no** se distribuye nunca).
