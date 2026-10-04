# Informe: app de escritorio e instalador

> Informe de investigación para la v2 (5 de octubre de 2026). Se conserva tal como se entregó al
> arquitecto. Las decisiones finales están en [`../PLAN-V2.md`](../PLAN-V2.md).

**Recomendación: Tauri 2 como visor + Python ya preparado (sin PyInstaller) + Inno Setup 7 + Azure Artifact Signing**

(Revisado el 05-10-2026 con la API de GitHub y leyendo los archivos de licencia de cada repositorio.)

**Idea principal.** Los ejemplos de GitHub que meten un backend Python dentro de Tauri ([dieharders/example-tauri-v2-python-server-sidecar](https://github.com/dieharders/example-tauri-v2-python-server-sidecar), Apache-2.0, sin cambios desde ago-2025; [fudanglp/tauri-fastapi-full-stack-template](https://github.com/fudanglp/tauri-fastapi-full-stack-template), MIT) lanzan el backend desde la ventana con PyInstaller, como proceso acompañante del programa. **Para un VMS ese modelo no sirve:** la grabación tiene que seguir aunque nadie haya iniciado sesión. Por eso conviene separarlo así:
- Backend + MediaMTX como **servicios de Windows**, como ahora.
- La app de escritorio es solo un **visor** que se conecta a `127.0.0.1` o a otro PC de la red.
- Instalador y actualizador se encargan de los dos.

## 1. Contenedor de escritorio: Tauri 2

- **Versión y licencia:** v2.12.1 del 30-09-2026, Apache-2.0/MIT, [repo](https://github.com/tauri-apps/tauri). Ya existe la v3.0.0-alpha.4: no la uses en producción.
- **Tamaño:** pocos MB, porque usa el WebView2 que ya trae Windows 11. No distribuye Chromium.
- **Varios monitores:** tiene `availableMonitors()`, `setPosition()`, `setFullscreen()` y `WebviewWindow`. Con eso se hace una ventana a pantalla completa por monitor ([API](https://v2.tauri.app/reference/javascript/api/namespacewindow/)).
- **Instancia única y arranque con Windows:** con los plugins de [plugins-workspace](https://github.com/tauri-apps/plugins-workspace) (single-instance v2.5.2, 01-10-2026, Apache-2.0).
- **Icono en la bandeja del sistema:** viene en el núcleo de Tauri (no lo verifiqué en el código).
- **Decodificación por hardware:** WebView2 usa «el mismo modelo de procesos que Edge». Las ventanas que comparten carpeta de datos usan un único proceso de GPU ([MS Learn](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/process-model)).
  - **No verificado** que WebRTC decodifique por hardware igual que en Edge. Hay casos reales de aceleración desactivada ([#1469](https://github.com/MicrosoftEdge/WebView2Feedback/issues/1469)). Hay que medirlo en el checklist C abriendo `edge://gpu` dentro de la app con 16 flujos.
  - **H.265 en WebRTC:** Chromium 136 lo da **solo por hardware** ([blink-dev](https://groups.google.com/a/chromium.org/g/blink-dev/c/3h8lL8a377c)). Que funcione en WebView2 de escritorio está **sin verificar**. Mantén los subflujos en H.264 y go2rtc como reserva.

**Alternativas:**
- **Electron** v44.5.1 (MIT). Su ventaja es una versión de Chromium fija y predecible. Pero pesa más de 100 MB y trae FFmpeg con licencia LGPL; esto último lo digo de memoria, no lo comprobé en este repaso.
- **pywebview** 6.2.1 (BSD-3). Su última versión es de abril de 2026; la bandeja va aparte y da menos control.
- **WPF + WebView2.** Es madura, pero obliga a añadir .NET al equipo de desarrollo.
- **Neutralinojs** v6.9.0 (MIT en el núcleo). Tiene un ecosistema más pequeño.

## 2. Runtime de Python: sin empaquetador

Propongo usar el Python embebible oficial (licencia PSF) o [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (MPL-2.0, versión 20261003) y **meter las wheels win_amd64 en el CI** con `pip install --target --platform win_amd64 --only-binary=:all: --require-hashes`.
- Así desaparece el problema del pip embebible: en el PC del cliente nunca se ejecuta pip.
- Funciona desde macOS o Linux: tus locks ya descargan para win_amd64 con código 0.

**Descartados:**
- **PyInstaller** 6.22.3. Licencia GPL con «Bootloader Exception», lo que permite cerrar el producto. El problema son los **falsos positivos de antivirus** que provoca su forma de autoextraerse ([guía](https://www.pythonguis.com/faq/problems-with-antivirus-software-and-pyinstaller/)). Hay hooks para cv2, onnxruntime, psycopg y uvicorn, pero **no hay hook de openvino** en hooks-contrib.
- **Nuitka**: **cambió a AGPL-3.0 el 28-01-2026**, con una excepción para su runtime que permite distribuir el programa compilado como cerrado (versión 4.2.2). Legalmente se puede, pero va contra tu regla de nada AGPL; si se usara, lo vería un abogado.
- **Briefcase** v0.4.5 (BSD-3) da poco control sobre los servicios.

## 3. Instalador: Inno Setup 7.1.0

[Inno Setup](https://github.com/jrsoftware/issrc) 7.1.0, del 12-08-2026.
- **Licencia:** propia y permisiva, no contamina el producto.
- **Licencia comercial:** se **pide** (aunque «not strictly required») a empresas que facturan más de 5.000 USD ([isorder](https://jrsoftware.org/isorder.php)). Recomiendo comprarla.

Qué cubre de lo que pediste:
- Asistente en español.
- Elegir componentes: puesto de control, tienda con analítica o central.
- Página propia en Pascal Script para la carpeta de grabaciones y el disco.
- Servicios con `sc.exe config obj= "NT SERVICE\<servicio>"` (cuenta de servicio virtual, no SYSTEM).
- Firewall con `netsh … profile=private`.
- Desinstalación que pregunta si conservar las grabaciones.
- Instalación silenciosa para las 147 tiendas: `/VERYSILENT /SUPPRESSMSGBOXES /COMPONENTS=… /LOADINF=`.
- Firma también el desinstalador con la directiva `SignTool`.

**Contenedor de servicios.** WinSW v2.12.0 lleva parado desde ene-2023, y la v3 sigue en alfa. Como sustituto: [shawl](https://github.com/mtkennerly/shawl) v1.9.0 (MIT, mayo de 2026) o [windows-service-rs](https://github.com/mullvad/windows-service-rs) (Apache-2.0).

**¿Se puede compilar el instalador fuera de Windows?**
- Funciona con Wine ([amake/innosetup-docker](https://github.com/amake/innosetup-docker), CC0), pero esa imagen se queda en la 6.7.1.
- Lo fiable es un runner **windows-latest** de GitHub Actions.
- NSIS sí compila de forma nativa en macOS/Linux; es lo que usa el instalador NSIS de Tauri.

**Alternativas descartadas:**
- **WiX v7.0.0:** licencia MS-RL más un **acuerdo de cuota de mantenimiento (OSMF)** que obliga a pagar a quien factura 10.000 USD o más si usa los binarios oficiales. Además MSI es más difícil. Solo vale si un cliente exige MSI para GPO o Intune.
- **NSIS:** licencia zlib; el módulo LZMA es CPL pero con excepción para enlazarlo.
- **[Velopack](https://github.com/velopack/velopack)** 1.2.161 (MIT): instala con un clic en `%LocalAppData%`, sin asistente y sin servicios. Su documentación general no describe rollback ni firma de paquetes ([docs](https://docs.velopack.io/packaging/installer)). Como mucho, para el visor.
- **MSIX:** no lo recomiendo con servicios y cuentas virtuales (no verificado a fondo).

## 4. Actualizaciones

El actualizador de Tauri firma siempre las actualizaciones, sin opción de desactivarlo ([docs](https://v2.tauri.app/plugin/updater/)). Pero solo sabe actualizar su propio paquete y tiene que cerrar la app.

**Propuesta:** un actualizador dentro del **servicio** (diseño propio, no sale de ningún proyecto existente):
1. Descarga un manifiesto firmado con ed25519.
2. Comprueba la firma Authenticode y el SHA-256 del instalador.
3. Ejecuta Inno en modo silencioso.
4. Comprueba que el servicio arranca bien (`/health`).
5. Si falla, reinstala la versión anterior, que se guarda en local.

## 5. Firma de código y SmartScreen

- **Azure Artifact Signing:** unos 9,99 USD al mes. Disponible para **organizaciones** de la UE. Para particulares, solo EE. UU. y Canadá ([MS Learn, 29-08-2026](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options)). Que un autónomo español pueda usarlo está **sin verificar**.
- **OV:** Certum en la nube desde 209 € ([tienda](https://shop.certum.eu/standard-code-signing-in-the-cloud.html)). Desde el 27-02-2026 un certificado dura como máximo 459 días.
- **EV:** desde 2024 ya no salta el aviso de SmartScreen de entrada, así que no compensa pagarlo por eso.
- **Sin firmar:** bloqueo fuerte de SmartScreen, y en empresas puede quedar bloqueado del todo.
- **Qué firmar:** todos nuestros .exe, MediaMTX, el instalador y el desinstalador.
- **Firmar desde macOS/Linux:** se puede con [jsign](https://github.com/ebourg/jsign) 7.5 (Apache-2.0), que soporta Artifact Signing.

## Riesgos

- Que WebView2 no decodifique por hardware 16 o más flujos. Plan B: Electron.
- H.265 en WebRTC dentro de WebView2.
- Que SmartScreen siga avisando hasta que la firma gane reputación con el tiempo.
- El cambio de licencia de Nuitka.
- La licencia comercial de Inno Setup y el acuerdo OSMF de WiX.
- Que WinSW está abandonado.

No he modificado ningún archivo del producto.
