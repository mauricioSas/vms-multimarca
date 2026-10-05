# `distribution/installer/`

**Dueño:** B3.

Instalador de Windows con Inno Setup 7.1.0 (PLAN-V2 §1.4). Se compila con `python -m tools.build all`
(ver [docs/EMPAQUETADO.md](../../docs/EMPAQUETADO.md)); la guía de uso es
[docs/INSTALACION-WINDOWS.md](../../docs/INSTALACION-WINDOWS.md).

| Archivo | Qué es |
|---|---|
| `VMSMultimarca.iss` | script principal: `[Setup]`, tipos de puesto, archivos, accesos directos e `#include` del código |
| `pascal/util.pas` | texto, JSON mínimo, SemVer, rutas y parámetros de la línea de órdenes |
| `pascal/state.pas` | estado global, detección de lo instalado (v1, versión, datos), `/LOADINF` y `/SECRETS` |
| `pascal/vmsctl.pas` | llamadas a `vmsctl.exe --json` (CONTRATO §14): solo códigos de salida y JSON, nunca texto de Windows |
| `pascal/files.pas` | `.env`, `config.json` inicial, carpeta de grabaciones, grupo «VMS Operadores», red pública (WMI), restos de la v1 |
| `pascal/pages.pas` | páginas propias: grabaciones, sede, panel central, seguridad, red y comprobación final |
| `pascal/events.pas` | eventos: Windows 10, negativa a bajar de versión, validación, cerrojo, pasos de `vmsctl`, códigos de salida |
| `pascal/uninstall.pas` | desinstalación: conservar o `/PURGE` |
| `lang/es.isl`, `lang/en.isl` | textos (español con tuteo por defecto; inglés con `/LANG=en`) |
| `lang/LICENCIA-*.txt` | condiciones de uso (pendientes de revisión legal) |
| `assets/` | icono e imágenes del asistente; se generan con `python -m distribution.installer.assets.make_assets` |
| `ejemplos/` | `tienda.inf` y `secrets.json` para la instalación silenciosa |

Reglas:
- La configuración del equipo (servicios, ACL, firewall, TLS, versión activa) la hace **siempre** `vmsctl`; el
  instalador no usa PowerShell.
- Los textos y archivos van en UTF-8 con BOM. Comentarios de Pascal sin `{` dentro, ninguna línea que empiece por
  `#` salvo directivas, sin `Exit(valor)` ni rutinas anidadas: lo comprueba `tests/windows/test_installer_script.py`.
- Lo que solo sirve para probar (`/SIMULATEWINBUILD=`, `/MINFREEGB=`, `/CAPTURESTATE=`) va dentro de
  `#ifdef TestBuild`.
- Firma: directiva `SignTool=` solo si la build la pide (decisión N1: sin certificado todavía).
