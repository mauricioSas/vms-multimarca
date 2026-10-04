# Informe: auditoría del código para la v2

> Informe de investigación para la v2 (5 de octubre de 2026). Se conserva tal como se entregó al
> arquitecto. Las decisiones finales están en [`../PLAN-V2.md`](../PLAN-V2.md).

# Auditoría del código de VMS Multimarca: paso a app de Windows con actualizaciones y más marcas

Ejecuté `pytest -q` el 5 de octubre de 2026 a las 00:24 (hora de Madrid). Resultado: **457 pasan, 1 falla y 5 se omiten**, en 6 min 5 s. El documento ESTADO.md dice 459 pasan y 1 se omite. Las 5 omisiones son 3 pruebas de .ps1 (no hay pwsh en el Mac), shellcheck y la comprobación de FFmpeg de OpenCV en macOS. El fallo es `tests/web/test_ui.py::test_playback_timeline_and_click`, y es una prueba que depende de la hora (detalle en P2). No es una regresión.

## P0. Bloquea la app de Windows y las actualizaciones

1. **El arranque está atado a «python.exe + WinSW + .env».** Cada servicio es `python.exe -m vms|analytics|central.agent`, registrado con WinSW 2.12.0. La regla del firewall apunta al `python.exe` base (`install.ps1:481`). Esto trae tres problemas:
   - Si se actualiza Python, la regla se rompe en silencio.
   - En el Administrador de tareas y en el antivirus el proceso aparece como un «python.exe» genérico y sin firmar.
   - WinSW tiene su última versión estable de enero de 2023 y la v3 sigue en alfa desde 2021. Lo verifiqué en https://github.com/winsw/winsw/releases (último commit 30-05-2026, licencia MIT): sigue vivo, pero estancado.

   Se mantiene la separación entre programa (`Program Files`) y datos (`ProgramData`), que ya está bien hecha (`vms/core/paths.py`). Hay que cambiar a un ejecutable propio y firmado por servicio, más un supervisor nativo. Coste: 1-2 semanas.

2. **La instalación descarga cosas de Internet.** Python, pip, WinSW, MediaMTX y todas las wheels de PyPI se bajan durante la instalación (`install.ps1:82-96` y 339-348). Solo funciona sin conexión si se le pasa `-WheelhouseDir`. Para un asistente gráfico y para las actualizaciones hace falta un paquete cerrado y firmado.

3. **Sin migraciones de configuración, el rollback pierde datos.** Detalles:
   - `CONFIG_VERSION = 1` existe, pero no hay código que migre de una versión a otra.
   - Los modelos usan `extra="ignore"`: una versión antigua que vuelva a guardar borra los campos nuevos.
   - `Vendor` es un `Literal` cerrado (`models.py:23`). Tras un rollback, los equipos de una marca nueva se descartan como «problemas» (se guarda una copia, pero desaparecen de la configuración).
   - Las migraciones de PostgreSQL (`vms/db/migrate.py`) solo van hacia delante.

   Antes de tener actualizador hacen falta tres cosas: una foto de `config/` y de la base de datos antes de actualizar, migraciones con versión y la regla de «nunca guardar una configuración de versión mayor». Coste: 3-4 días.

4. **Los servicios corren como LocalSystem.** El XML de WinSW no tiene `<serviceaccount>`. Además, la clave Fernet está en `secrets\secret.key` en texto plano, protegida solo por `icacls`. Lo recomendable es una cuenta virtual (`NT SERVICE\VMSBackend`) y guardar la clave con DPAPI de máquina. Es pendiente 1 de ESTADO.md y hay que probarlo en Windows real. Coste: 2-3 días.

## P1. Compatibilidad multimarca

5. **Marcas que se soportan de verdad:**
   - **Hikvision:** por ISAPI, código propio y bien estructurado.
   - **Dahua:** por CGI, código propio.
   - **ONVIF:** con onvif-zeep-async v4.3.0, publicada el 28-09-2026 (https://github.com/openvideolibs/python-onvif-zeep-async).
   - **RTSP manual:** sin API.

   Todo está probado **solo contra simuladores escritos por nosotros** (`tools/mocks/*`, unas 36 pruebas). No hay ni una respuesta capturada de un equipo real. Uniview, Hanwha, Axis, Bosch, Ezviz y las marcas blancas (LTS, Annke, Lorex, Amcrest) solo se pueden usar por ONVIF o con la ruta RTSP escrita a mano.

6. **Añadir una marca no es enchufar un driver.** Hay que tocar 8 sitios:
   - `models.py` (el `Literal`)
   - `rtsp.py` (los presets)
   - `vendors/__init__.py` (cadenas de `if`)
   - `discovery.py`
   - `panel.js` (repite los presets en JavaScript)
   - `ui.js`
   - `index.html`
   - `app.css`

   La solución es un registro de drivers (id, nombre, presets, clase cliente, pistas para detectar la marca) que la UI lea desde la API. A eso se le suma una batería de contrato común que se ejecute contra cada driver con respuestas grabadas de equipos reales, con versión de firmware. Coste: 4-6 días.

7. **Errores concretos en los drivers:**
   - `guess_vendor` clasifica como Dahua cualquier modelo que empiece por `IPC-`, y Uniview también usa ese prefijo (`discovery.py:54`).
   - `DahuaClient.snapshot` no tiene en cuenta el parámetro `stream`: siempre pide el flujo principal.
   - Falta el «permitir Basic» por equipo (pendiente 3 de ESTADO.md).
   - No hay soporte de RTSPS ni de cámaras que solo dan MJPEG por HTTP.

8. **H.265 es el riesgo multimarca más grande.**
   - `BROWSER_CODECS = ("H264",)` (`views.py:13`).
   - Los NVR modernos traen el flujo principal en H.265/H.265+ por defecto.
   - El doble clic, que abre el flujo principal, y la reproducción de grabaciones (se graba el principal) no se verán en el navegador.
   - Esto hoy solo genera un aviso. Las opciones son: transcodificar, exigir H.264 en el principal o usar un reproductor nativo.
   - **No verificado:** que Edge/Chrome actuales reproduzcan HEVC por WebRTC o en `<video>` con decodificación por hardware en Windows 11.

## P2. Pendientes de ESTADO.md (evaluación y coste)

| Pendiente | Diagnóstico | Coste |
|---|---|---|
| Reconexión del muro en 21-29 s | Causa en el código: el reintento crece 1→2→5→10 s (`whep.js:16`) mientras MediaMTX vuelve a conectar con la cámara, y las respuestas 404/503 suben el contador. Arreglo: enviar un evento SSE «motor relanzado» que reinicie el contador, o poner un tope de 2-3 s cuando el fallo viene del motor | 0,5-1 día |
| Posible fuga de memoria en MediaMTX | +1,4-1,6 MB/min medidos en solo 5 min (`RESULTADOS.md:73`). Con eso no se puede concluir nada: hace falta una prueba de 24-72 h con `pprof` | 1 día de preparar + la prueba |
| HTTPS automático | Hoy son 4 pasos a mano. En el instalador gráfico: generar el certificado, abrir el 8643 e importarlo como raíz | 1-2 días |
| Avisos de mypy/ruff | **No pude verificarlo:** ni ruff ni mypy están en el `.venv` (solo queda una caché de ruff 0.16.10) | Añadirlos a requirements-dev |

## P2. Riesgos de Windows que la instalación real aún no ha mostrado

- **Puertos:** 8600, 8643, 8554, 8889, 8189, 9996, 9997 y 9998 son fijos. Si Hyper-V o WSL reservan alguno (`netsh int ipv4 show excludedportrange`), el arranque falla. Solo se comprueba el puerto de la API de MediaMTX (`process.py:220`).
- **Antivirus/SmartScreen:** `mediamtx.exe`, `python.exe` y las DLL de OpenVINO no están firmados, y además se descomprimen con `Expand-Archive`. Defender o un EDR pueden ponerlos en cuarentena.
- **Reinicio por Windows Update:**
  - Los servicios vuelven solos (inicio automático diferido).
  - El kiosco no: necesita inicio de sesión automático, que no se configura.
  - Edge puede mostrar avisos de reinicio o de actualización; solo se fijan 3 directivas.
- **Perfil de red pública:** solo se avisa, no se corrige. WebRTC por UDP 8189 queda bloqueado.
- **Tildes y espacios:** las rutas de datos están fijas en `ProgramData` (bajo riesgo). Los riesgos reales son el perfil del kiosco en `%LOCALAPPDATA%` con usuarios con tilde y una ruta `-InstallDir` personalizada. Nada de esto está probado.
- **Idioma del sistema:** no se analiza la salida de comandos del sistema desde Python (bien). La conversión de la cuenta del kiosco usa SID (bien).
- **Suspensión:** se fija con `powercfg` solo con corriente. El ahorro de energía de USB/red y las GPU sin decodificación por hardware en el kiosco no se han probado (no verificado).
- **Reinstalar o actualizar:** `install.ps1` para los servicios antes de copiar (bien), pero no hay vuelta atrás si la copia falla a medias.

## P3. Deuda técnica y calidad de las pruebas

- **Prueba que depende de la hora:** el doble de pruebas (`tests/fakes.py:69`) crea grabaciones de «hace 2 h en UTC». Entre las 00:00 y las ~02:00 de Madrid caen en el día anterior y la línea de tiempo de «hoy» sale vacía. Lo confirmé: falla a las 00:30 CEST.
- **Cobertura:** no se mide. No hay pytest-cov ni coverage instalados.
- **Pruebas que dependen del Mac:**
  - Ruta `/opt/homebrew/bin/ffmpeg|ffprobe` como alternativa en `tests/conftest.py:70` y en `system_check.py:58`.
  - La prueba del cambio de hora se omite en Windows (`time.tzset`).
  - Las pruebas de los .ps1 se omiten sin pwsh.
  - Una prueba de despliegue necesita acceso a GitHub.
  - Muchas pruebas se omiten en silencio si falta alguna herramienta, así que «459 pasan» solo es reproducible en este Mac.
- **Sin integración continua:** no hay `.github/` ni una máquina Windows que ejecute la batería. Es lo primero que hay que montar para un producto que se actualiza: un runner Windows que ejecute pytest, el instalador y las pruebas de contrato de cada driver.
- **Licencias:** `THIRD_PARTY_NOTICES.txt` y los locks con hashes están al día. MediaMTX v1.21.1 (20-09-2026) sigue siendo la última versión según https://github.com/bluenviron/mediamtx/releases.

## Orden recomendado

1. Integración continua en Windows y arreglar la prueba que depende de la hora.
2. Registro de drivers con sus pruebas de contrato, y corregir `guess_vendor`.
3. Migraciones de configuración y fotos de seguridad antes de actualizar.
4. Ejecutables firmados más supervisor nativo, con cuenta virtual y DPAPI.
5. Paquete sin conexión (base para el instalador gráfico y el actualizador).
6. Decidir qué hacer con H.265.
7. Reconexión rápida del muro y prueba de memoria de 72 h.

No modifiqué ningún archivo del producto.
