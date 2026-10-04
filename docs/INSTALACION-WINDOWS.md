# Instalación en Windows 10/11

Guía para instalar VMS Multimarca en el **PC de control** (4 monitores) o en un PC de tienda con
Windows. Tiempo aproximado: 20–30 minutos (la mayor parte es la descarga de dependencias).

> Las «capturas» de esta guía están descritas con palabras: indican qué verás en pantalla en cada
> paso para que sepas si vas bien.

## 0. Antes de empezar

| Necesitas | Detalle |
|---|---|
| Windows 10 22H2 u 11, 64 bits | Pro recomendado (gestión de energía y directivas más sencillas) |
| Usuario administrador | para instalar servicios y reglas de firewall |
| Internet durante la instalación | o un paquete sin conexión (ver [EMPAQUETADO.md](EMPAQUETADO.md)) |
| Microsoft Edge (ya viene con Windows) o Google Chrome | para los muros en modo kiosco |
| La red de la tienda marcada como **privada** | el firewall solo se abre en redes privadas |
| Disco para grabaciones | ver el cálculo en [REQUISITOS-HARDWARE.md](REQUISITOS-HARDWARE.md) |

Si vas a grabar en un disco distinto de `C:`, formatéalo antes en NTFS y asígnale letra (p. ej. `D:`).

## 1. Copiar el programa

Copia la carpeta del programa al PC, por ejemplo a `C:\Instalacion\vms-multimarca`.

*Captura: en el Explorador de archivos ves las carpetas `vms`, `analytics`, `central`, `deploy`,
`docs` y los archivos `LEEME.md`, `.env.example` y `requirements-*.txt`.*

## 2. Ejecutar el instalador

1. Pulsa **Inicio**, escribe `PowerShell`, haz clic derecho en **Windows PowerShell** y elige
   **Ejecutar como administrador**.

   *Captura: ventana azul con el título «Administrador: Windows PowerShell».*

2. Entra en la carpeta y permite ejecutar el script solo en esta ventana:
   ```powershell
   cd C:\Instalacion\vms-multimarca
   Set-ExecutionPolicy -Scope Process Bypass
   ```
3. Lanza la instalación. Elige los componentes según el equipo:

   | Equipo | Orden |
   |---|---|
   | PC de control, solo vídeo | `.\deploy\windows\install.ps1` |
   | Tienda con analítica y latido a la central | `.\deploy\windows\install.ps1 -Components Backend,Analytics,Heartbeat -SiteId site-bcn-001 -CentralUrl https://central.vpn:8700` |
   | Servidor central en Windows | `.\deploy\windows\install.ps1 -Components Central` |

   Opciones útiles: `-InstallDir` (por defecto `C:\Program Files\VMSMultimarca`), `-DataDir`
   (por defecto `C:\ProgramData\VMSMultimarca`), `-PythonMode Embedded` (no usar el Python del
   equipo), `-AllowDomainProfile` (abrir también en red de dominio), `-NoStart`.

   *Captura: el instalador muestra bloques que empiezan por `==>` (en azul) y líneas `OK` en verde:*
   ```
   ==> Comprobando el equipo
       OK  Windows 10.0.22631 de 64 bits; componentes: Backend
   ==> Preparando Python 3.12
       OK  Python embebible 3.12.10 en C:\Program Files\VMSMultimarca\python
   ==> Instalando dependencias ...
   ==> MediaMTX v1.21.1 para Windows
       OK  MediaMTX verificado en C:\Program Files\VMSMultimarca\bin
   ==> Registrando servicios de Windows (WinSW, MIT)
       OK  VMSBackend: VMS Multimarca - Backend y video
   ==> Firewall de Windows (solo red privada)
   ==> Arrancando servicios
       OK  Backend: estado ok, versión 0.1.0
   Instalación terminada.
   ```
   Las líneas `AVISO` en amarillo indican algo que debes revisar (por ejemplo, que la red está
   marcada como pública). Si aparece un error en rojo, el instalador se detiene sin dejar nada a
   medias que impida repetirlo: corrige el problema y vuelve a ejecutarlo.

### Qué hace el instalador

1. Comprueba Windows de 64 bits y permisos de administrador.
2. Copia el programa a la carpeta de instalación.
3. **Python 3.12:** si el equipo ya lo tiene (`py -3.12`), crea un entorno virtual; si no, descarga
   el **Python embebible oficial 3.12.10** de python.org y comprueba su SHA-256.
4. Instala las dependencias exactas de los archivos de bloqueo (`pip --no-deps --only-binary`): no
   se instala nada que no esté revisado (licencias).
5. Descarga **MediaMTX v1.21.1** para Windows y comprueba su SHA-256.
6. Crea `C:\ProgramData\VMSMultimarca\.env` desde `.env.example` (nunca pisa uno existente),
   genera el token del kiosco y deja `.env` y `secrets\` accesibles solo para SYSTEM y
   Administradores.
7. Registra los servicios con **WinSW** (licencia MIT, versión 2.12.0 con SHA-256 comprobado):
   arranque automático (retrasado), reinicio a los 5 s, 30 s y 60 s si se caen, y registros
   rotativos en `C:\ProgramData\VMSMultimarca\logs\service`.
8. Abre en el firewall **solo en el perfil privado**: 8600/tcp (web) para el proceso de Python y
   8189/udp+tcp (vídeo WebRTC) para MediaMTX. Con el componente Central, 8700/tcp.
9. Arranca los servicios y espera a que `http://127.0.0.1:8600/api/health` responda.

**¿Por qué WinSW y no NSSM?** Ambos sirven. WinSW tiene licencia MIT, se configura con un XML que
queda junto al ejecutable (fácil de revisar y versionar), permite definir la política de
reinicio, la rotación de registros y que la parada envíe Ctrl+C al proceso principal antes que a
sus hijos (así el backend detiene MediaMTX con orden). NSSM es de dominio público pero su última
versión estable es de 2014 y la recomendada para Windows 10 es una versión preliminar.

**¿Por qué no hay un servicio de MediaMTX?** El backend arranca MediaMTX como proceso hijo,
genera su configuración, le registra las cámaras por su API (las contraseñas solo están en
memoria) y lo relanza si se cae. Un segundo servicio ocuparía los mismos puertos y no tendría las
cámaras.

## 3. Primer acceso

1. En el propio PC abre `http://127.0.0.1:8600/`.

   *Captura: página «Primer arranque» con campos Usuario y Contraseña.*

2. Crea el administrador (contraseña de al menos 8 caracteres). Por seguridad, esta página solo
   funciona desde el propio PC.
3. Entra y da de alta los equipos siguiendo [ALTA-EQUIPOS.md](ALTA-EQUIPOS.md).

Desde otro PC de la misma red: `http://IP-DEL-PC:8600/`.

## 4. Muros en los 4 monitores (kiosco)

1. Conecta los 4 monitores y, en **Configuración → Sistema → Pantalla**, ponlos en modo
   **Extender** y colócalos en el orden físico (el monitor de la izquierda muestra el muro 1).
   Pon la **escala al 100 %** en todos si es posible.

   *Captura: cuatro rectángulos numerados 1–4 alineados en la configuración de pantalla.*

2. Crea un usuario local de Windows para el kiosco (p. ej. `muros`, sin permisos de
   administrador).
3. En PowerShell como administrador:
   ```powershell
   cd "C:\Program Files\VMSMultimarca"
   .\deploy\windows\install-kiosk.ps1 -KioskUser "$env:COMPUTERNAME\muros"
   ```
   Opciones: `-Browser Edge|Chrome`, `-Monitors 2` (usar solo 2), `-SkipEdgePolicies`.
4. Cierra sesión y entra como `muros`. Unos segundos después se abre una ventana a pantalla
   completa en cada monitor con `/wall/1`, `/wall/2`, `/wall/3` y `/wall/4`.

   *Captura: cada monitor muestra una cuadrícula de cámaras sin barras del navegador.*

Qué hace el kiosco:
- Abre una ventana de Edge (o Chrome) en modo kiosco por monitor, cada una con su propio perfil y
  en la posición de su pantalla; si el navegador la abre en otra pantalla, la mueve.
- Si cierras una ventana (Alt+F4) o el navegador se cae, la vuelve a abrir.
- Cada 6 horas reabre los muros de uno en uno para renovar la sesión del kiosco (caduca a las
  `VMS_SESSION_HOURS`, 12 h por defecto).
- Mientras está abierto, la pantalla no se apaga y el equipo no se suspende.
- Registro en `%LOCALAPPDATA%\VMSMultimarca\kiosk.log` del usuario del kiosco.

Para hacer mantenimiento: abre PowerShell con el usuario del kiosco y ejecuta
`& "C:\Program Files\VMSMultimarca\deploy\kiosk\start-kiosk.ps1" -Stop`.

**Inicio de sesión automático (opcional):** si quieres que el PC muestre los muros tras un corte de
luz sin que nadie escriba la contraseña, configura el inicio automático del usuario `muros` con
`netplwiz` o con la herramienta Autologon de Microsoft Sysinternals (guarda la contraseña cifrada
en LSA). Valóralo con el responsable de seguridad: cualquiera con acceso físico verá los muros.

## 5. Comprobaciones

```powershell
Get-Service VMS*                                   # deben estar «Running»
Invoke-RestMethod http://127.0.0.1:8600/api/health # status: ok
Get-NetFirewallRule -Group "VMS Multimarca" | Format-Table DisplayName, Profile, Enabled
Get-NetConnectionProfile                           # la red de la tienda debe ser «Private»
```

Registros: `C:\ProgramData\VMSMultimarca\logs\vms.log` (backend), `analytics.log`,
`heartbeat.log` y `logs\service\*.log` (salida de los servicios).

## 6. Configuración (`C:\ProgramData\VMSMultimarca\.env`)

Edítalo con el Bloc de notas **como administrador** y reinicia el servicio afectado
(`Restart-Service VMSBackend`). Variables más usadas:

| Variable | Para qué |
|---|---|
| `VMS_SITE_ID` | identificador único de la tienda (`site-bcn-001`) |
| `VMS_PG_DSN` | PostgreSQL central (conteos y latido directo) |
| `VMS_CENTRAL_URL`, `VMS_SITE_TOKEN` | latido HTTP al panel central (servicio VMSHeartbeat) |
| `VMS_AGENT_USERNAME`, `VMS_AGENT_PASSWORD` | usuario **operador** del backend para que el latido incluya el estado de cada cámara |
| `VMS_TELEGRAM_BOT_TOKEN` | avisos de cola |
| `VMS_MTX_WEBRTC_ADDITIONAL_HOSTS` | IP de la VPN para ver vídeo en remoto, p. ej. `["100.64.0.12"]` |

## 7. Actualizar

Copia la nueva versión en otra carpeta y vuelve a ejecutar `install.ps1` con los mismos
componentes. Para los servicios, sustituye el programa y conserva `.env`, la configuración, las
contraseñas y las grabaciones.

## 8. Desinstalar

```powershell
& "C:\Program Files\VMSMultimarca\deploy\windows\uninstall.ps1"             # conserva datos y grabaciones
& "C:\Program Files\VMSMultimarca\deploy\windows\uninstall.ps1" -RemoveData # borra también datos (pide confirmación)
```
Puedes lanzarlo desde la propia carpeta de instalación: PowerShell carga el script entero antes
de empezar, así que borrar esa carpeta al final no lo interrumpe.

## Validación de estos scripts

Los scripts se han validado con el analizador sintáctico de PowerShell 7 y con PSScriptAnalyzer
(sin avisos), y sus funciones auxiliares (lectura y escritura de `.env`, descarga con
verificación SHA-256, token y URL del kiosco) se ejecutan en las pruebas automáticas. **Falta
probarlos en un Windows real** (servicios, firewall, kiosco con 4 monitores): sigue
[CHECKLIST-PRUEBAS.md](CHECKLIST-PRUEBAS.md) en la primera instalación.
