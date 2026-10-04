# Componentes de terceros y atribuciones

## Licencias

- Tabla resumida de componentes y licencias: [LEEME.md](../LEEME.md#licencias-de-terceros).
- Lista completa con versiones y textos de licencia: [THIRD_PARTY_NOTICES.txt](../THIRD_PARTY_NOTICES.txt),
  generada desde los archivos de bloqueo con `python -m deploy.third_party_notices` (una prueba
  automática comprueba que está al día).
- Reglas y hallazgos de licencias verificados: [CONTRATO.md §10](CONTRATO.md).

## Política

Solo se aceptan licencias permisivas (MIT, BSD, Apache-2.0, PSF, ISC, MPL-2.0) y LGPL usada como
biblioteca sin modificar y enlazada dinámicamente. **Prohibido** en el producto: GPL/AGPL
(Ultralytics YOLO y sus pesos, python-amcrest, PyAV de PyPI con FFmpeg GPL, imageio-ffmpeg, mpv,
ZoneMinder, Shinobi, Moonfire, Bluecherry), pesos RF-DETR XL/2XL (licencia PML) y SDK
propietarios de Hikvision/Dahua. `tests/test_licenses.py` vigila el entorno.

## Código de terceros incorporado al repositorio

| Archivo | Origen | Licencia | Nota |
|---|---|---|---|
| — | — | — | A fecha de esta versión, los clientes de Hikvision (ISAPI), Dahua (CGI/RPC2), ONVIF (sobre `onvif-zeep-async`), el descubrimiento WS-Discovery, el lector WHEP del navegador y el panel central son **implementación propia**. |

Si se adapta código de terceros (por ejemplo `client.py` de `rroller/dahua`, MIT), se añade aquí
una fila con el archivo, el repositorio de origen, la versión o commit, la licencia, y se conserva
el aviso de copyright en la cabecera del archivo adaptado.

## Binarios que descarga el instalador

| Binario | Versión | Licencia | Verificación |
|---|---|---|---|
| MediaMTX | v1.21.1 | MIT | SHA-256 fijado (comprobado contra `checksums.sha256` de la release oficial) |
| WinSW (`WinSW.NET461.exe`) | v2.12.0 | MIT | SHA-256 fijado |
| Python embebible (Windows) | 3.12.10 | PSF-2.0 | SHA-256 fijado (calculado sobre la descarga oficial de python.org) |
| pip (Windows embebible) | 26.2.1 | MIT | SHA-256 de PyPI |
