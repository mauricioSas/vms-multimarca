# Instalación en Windows

Guía para instalar VMS Multimarca 2 en un PC con Windows: el **puesto de control** (grabación y muros en
varios monitores), un **PC de tienda** con analítica, el **panel central** o un PC que **solo mira** las cámaras
de otros equipos. Se instala con un asistente normal de Windows («Siguiente, Siguiente, Instalar»): no hace falta
abrir PowerShell ni escribir órdenes.

Tiempo aproximado: 5 minutos. No necesita Internet: todo lo necesario va dentro del instalador.

> Las capturas de cada página del asistente se generan solas en cada compilación del instalador (artefacto
> `instalador-capturas` de la integración continua). Esta guía describe con palabras lo que verás en cada una.

---

## 1. Antes de empezar

| Necesitas | Detalle |
|---|---|
| **Windows 11** (23H2 o posterior), 64 bits | También Windows Server 2022 o 2025. Pro recomendado. |
| Windows 10 22H2 | **Solo** si el equipo tiene las actualizaciones de seguridad ampliadas (ESU) de pago. El instalador avisa y deja continuar; las funciones nuevas pueden no estar garantizadas. |
| Un usuario **administrador** | Windows pedirá permiso («¿Quieres permitir que esta aplicación haga cambios?»): responde **Sí**. |
| Disco para grabaciones | Al menos **50 GB libres**. Lo ideal es un disco aparte del de Windows (por ejemplo `D:`). Cálculo de capacidad en [REQUISITOS-HARDWARE.md](REQUISITOS-HARDWARE.md). |
| La red de la tienda como **privada** | El firewall solo deja entrar a otros equipos en redes privadas. Si está como pública, el asistente lo detecta y te ofrece cambiarla. |
| El archivo `VMSMultimarca-Setup-2.x.y.exe` | Te lo da Unmanned Studio o soporte técnico. |

Si vas a grabar en un disco distinto de `C:`, formatéalo antes en NTFS y asígnale una letra.

## 2. Instalar con el asistente

Haz doble clic en `VMSMultimarca-Setup-2.x.y.exe` y responde **Sí** al aviso de permisos de Windows.

> **Aviso de SmartScreen («Windows protegió tu PC»).** Mientras el instalador no esté firmado con un
> certificado de empresa, Windows puede mostrar este aviso. Haz clic en **Más información** y después en
> **Ejecutar de todas formas**. Comprueba antes que el archivo te lo dio soporte técnico.

El asistente tiene estas páginas:

1. **Bienvenida.** *Verás el logotipo (una cámara azul) y el texto «Te damos la bienvenida al instalador de VMS
   Multimarca».* Haz clic en **Siguiente**.
2. **Condiciones de uso.** Léelas, marca **Acepto las condiciones** y haz clic en **Siguiente**.
3. **Tipo de puesto.** Elige en la lista desplegable qué papel tiene este PC. Debajo verás lo que se instala con
   cada tipo:

   | Tipo | Para qué | Servicios de Windows | Visor |
   |---|---|---|---|
   | **Puesto de control** | Graba las cámaras y las muestra en los muros | VMSEngine, VMSBackend, VMSUpdater | sí |
   | **Tienda con analítica** | Graba, cuenta personas y avisa de colas; envía el estado al panel central | lo anterior + VMSAnalytics, VMSHeartbeat | opcional |
   | **Panel central** | Ve el estado de todas las tiendas | VMSCentral, VMSUpdater | no |
   | **Solo visor** | Central de vigilancia que mira las cámaras de otros PC | VMSUpdater | sí |

4. **Opciones de este equipo.** En una tienda: «Instalar también el visor de escritorio». En un puesto de
   control: «Abrir los muros a pantalla completa al iniciar sesión» (márcalo en el PC de los monitores).
5. **Grabaciones** (puesto de control y tienda). Elige la carpeta donde se guardan los vídeos, por ejemplo
   `D:\Grabaciones CCTV`. Escribe cuántas cámaras vas a grabar y los Mbit/s aproximados de cada una. *Debajo
   verás el espacio libre del disco y cuántos días de grabación caben.* Si eliges el disco de Windows, te lo
   advierte. Si quedan menos de 50 GB libres, no deja continuar.
6. **Sede** (puesto de control y tienda). Nombre de la tienda, código de tienda (opcional) e identificador.
   *El identificador se rellena solo a partir del código o del nombre (minúsculas y guiones, por ejemplo
   `tienda-centro`).* Si la tienda envía su estado a un panel central, escribe su dirección (empieza por
   `https://`) y el token de la sede que te dieron.
7. **Panel central** (solo ese tipo). La cadena de conexión de PostgreSQL que te dio el administrador de la base
   de datos.
8. **Seguridad.** La contraseña del usuario **admin** (mínimo 8 caracteres), dos veces. Guárdala bien: la
   necesitas para dar de alta cámaras y usuarios.
9. **Red.** *Verás estas opciones:*
   - **Usar HTTPS en la red local** (marcada): cifra la conexión cuando otro PC abre el panel. Déjala marcada.
   - **Abrir también en redes de dominio**: solo si el PC está en el dominio de la empresa.
   - **Puertos** web (8600) y web seguro (8643): cámbialos solo si soporte te lo pide o si el asistente dice
     que están ocupados.
   - Si alguna red está marcada como **Pública**, aparece un aviso y la casilla «Cambiar esas redes a
     Privadas». Márcala solo si es la red de la tienda o la VPN.
10. **Todo listo para instalar.** Un resumen de lo elegido. Revisa la carpeta de grabaciones y la sede y haz clic
    en **Instalar**.
11. **Instalando.** *Una barra de progreso y mensajes como «Registrando los servicios de Windows…» y
    «Comprobando que todo responde (hasta 2 minutos)…».*
12. **Comprobación final.** «Todo listo: los servicios están en marcha y responden». Si algo falló, el texto dice
    en qué paso y aparece el botón **Guardar informe…**: guarda el archivo `.zip` y envíalo a soporte técnico (no
    contiene contraseñas).
13. **Fin.** Deja marcada «Abrir VMS Multimarca» si quieres abrir el visor ahora y haz clic en **Finalizar**.

### Qué hace el instalador

- Copia el programa a `C:\Program Files\VMSMultimarca` (cada versión en su propia carpeta, para poder volver
  atrás si una actualización falla).
- Guarda la configuración, los usuarios y los registros en `C:\ProgramData\VMSMultimarca`, y las grabaciones en
  la carpeta que elegiste. Solo Windows, los administradores y los propios servicios pueden leerlas: los
  usuarios normales del PC no ven grabaciones ni contraseñas.
- Registra los servicios de Windows del tipo de puesto. Arrancan solos con el PC, aunque nadie inicie sesión.
- Abre en el firewall solo los puertos necesarios y solo en redes privadas (y de dominio si lo marcaste).
- Con HTTPS, crea un certificado para este PC.
- Crea el grupo de Windows **VMS Operadores** y mete en él al usuario que instala. Los miembros de ese grupo
  pueden abrir los muros sin escribir contraseña. Mete también la cuenta de muros (en **Administración de
  equipos → Usuarios y grupos locales → Grupos**).
- Crea el acceso **VMS Multimarca** en el menú Inicio.

## 3. Primer acceso

1. Abre **VMS Multimarca** desde el menú Inicio.
2. Entra con el usuario `admin` y la contraseña que pusiste en el asistente. Cámbiala desde el panel si quieres.
3. Da de alta los equipos siguiendo [ALTA-EQUIPOS.md](ALTA-EQUIPOS.md).

Desde otro PC de la red: abre el visor en modo puesto remoto, o el navegador en `https://NOMBRE-DEL-PC:8643/`.

## 4. Muros en varios monitores (puesto de control)

1. Conecta los monitores y, en **Configuración → Sistema → Pantalla**, ponlos en modo **Extender**, en el orden
   físico.
2. Crea un usuario de Windows para los muros (por ejemplo `muros`, sin permisos de administrador) y añádelo al
   grupo **VMS Operadores**.
3. Si en el asistente marcaste «Abrir los muros a pantalla completa al iniciar sesión», al entrar con ese usuario
   se abre un muro a pantalla completa en cada monitor, sin pedir contraseña.

## 5. Instalación silenciosa (muchas tiendas)

Para instalar en muchas tiendas sin pasar por el asistente, prepara dos archivos y lanza el instalador desde una
consola de administrador o desde la herramienta de despliegue de la empresa:

```
VMSMultimarca-Setup-2.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="C:\vms-install.log" ^
    /TYPE=store /LOADINF="tienda.inf" /SECRETS="secrets.json"
```

- `/TYPE=` es el tipo de puesto: `control`, `store` (tienda), `central` o `viewer` (solo visor).
- `/LOADINF=` apunta a las respuestas del asistente (sin nada secreto). Ejemplo completo:
  [`distribution/installer/ejemplos/tienda.inf`](../distribution/installer/ejemplos/tienda.inf). Guárdalo sin
  tildes o en «Unicode»; si lo guardas en UTF-8 el instalador también lo entiende.
- `/SECRETS=` apunta a un JSON con los secretos. **El instalador lo lee y lo borra.** Nunca pongas contraseñas
  en el `.inf` ni en la línea de órdenes. Ejemplo:
  [`distribution/installer/ejemplos/secrets.json`](../distribution/installer/ejemplos/secrets.json).
- `/LOG=` guarda el registro de la instalación (no contiene contraseñas).

Claves de la sección `[Setup]` del `.inf`:

| Clave | Para qué | Por defecto |
|---|---|---|
| `SetupType`, `Tasks` | Las estándar del instalador: tipo de puesto y opciones (`storeviewer`, `walls`) | — |
| `RecordingsDir` | Carpeta de grabaciones | `C:\ProgramData\VMSMultimarca\recordings` |
| `Cameras` | Número de cámaras (para comprobar el disco) | 8 |
| `MbpsPerCamera` | Mbit/s por cámara | 4 |
| `SiteName` | Nombre de la sede | — (obligatorio en control y tienda) |
| `SiteCode` | Código de tienda | vacío |
| `SiteId` | Identificador (3-40 caracteres: minúsculas, números y guiones) | se sugiere del código o del nombre |
| `CentralUrl` | Dirección del panel central | vacío |
| `Https` | `1` para HTTPS en la red local | `1` |
| `DomainProfile` | `1` para abrir también en redes de dominio | `0` |
| `HttpPort` / `HttpsPort` | Puertos web | `8600` / `8643` |
| `SetPrivateNetwork` | `1` para cambiar a privadas las redes públicas | `0` |
| `UpdateSource` | Origen de actualizaciones (`https://…`, `http://…` o `file:///…`); vacío = sin actualizaciones automáticas | vacío |

Claves del archivo de secretos (JSON):

| Clave | Para qué |
|---|---|
| `admin_password` | Contraseña del usuario `admin` (mínimo 8 caracteres). Obligatoria en una instalación nueva de control, tienda o panel central |
| `site_token` | Token de la sede para el panel central (tienda) |
| `pg_dsn` | Cadena de conexión de PostgreSQL (panel central) |

Códigos de salida del instalador:

| Código | Significado |
|---|---|
| 0 | Instalado y comprobado |
| 1 | No arrancó: versión más nueva ya instalada (ver §6), archivo de secretos que no existe o no es JSON, o aviso de Windows 10 cancelado |
| 2 | Cancelado antes de instalar |
| 7 | Algún dato no es válido o no se pudo preparar la instalación (puerto ocupado, actualización en curso…). El motivo está en el registro (`/LOG`) |
| 12 | Instalado, pero el sistema no respondió en 2 minutos. Revisa el registro y, si sigue así, envía el informe de diagnóstico |
| 20 | Falló un paso de la instalación (servicios, permisos, firewall…). El paso y el motivo están en el registro |
| 3, 4, 5, 8 | Errores propios del instalador (archivos dañados, cancelación, reinicio necesario) |

## 6. Actualizar

### Desde la versión 1 (instalada con `install.ps1`)

Si el PC tiene la versión 1 (la que se instalaba con PowerShell), **ejecuta el instalador nuevo encima**, con el
asistente o en silencio. No desinstales antes la versión 1.

- El asistente lo detecta y lo dice en el resumen («Actualización desde la versión 1»).
- **Se conservan** la configuración (cámaras, muros, sede, retención), los usuarios, las contraseñas guardadas
  de las cámaras, el `.env` y **todas las grabaciones** de `C:\ProgramData\VMSMultimarca`. Si la versión 1
  usaba otra carpeta de datos, se sigue usando esa.
- Los servicios antiguos (WinSW) se sustituyen por los nuevos y se borran los restos del programa antiguo de
  `C:\Program Files\VMSMultimarca` (Python, `services\`, código). Los datos no se tocan.
- Como la configuración ya existe, el asistente no vuelve a pedir la carpeta de grabaciones, la sede ni la
  contraseña del administrador.
- Antes de actualizar, conviene hacer una copia de `C:\ProgramData\VMSMultimarca\config`.

### Entre versiones 2

Las actualizaciones llegan solas (servicio **VMSUpdater**, por la noche) y vuelven atrás solas si algo falla.
También puedes ejecutar un instalador más nuevo encima: conserva todo igual.

El instalador **se niega a bajar de versión**: si ya está instalada una versión más nueva, avisa («Ya está
instalada la versión 2.1.3, más nueva que este instalador») y no toca nada. Solo soporte técnico puede forzarlo
con `/ALLOWDOWNGRADE` (queda anotado en el registro de la instalación).

## 7. Desinstalar

**Configuración → Aplicaciones → VMS Multimarca → Desinstalar.** El desinstalador pregunta:

> ¿Quieres conservar las grabaciones y la configuración?

- **Sí** (recomendado): quita el programa, los servicios y las reglas del firewall, y **conserva** las
  grabaciones, los usuarios y los ajustes. Si vuelves a instalar, todo sigue como estaba.
- **No**: lo borra **todo**, también las grabaciones. No se puede deshacer. La carpeta de grabaciones solo se
  borra si la creó el instalador (nunca una carpeta que ya existía ni la raíz de un disco).

En silencio (`unins000.exe /VERYSILENT`, en `C:\Program Files\VMSMultimarca\uninstall`) se conserva todo, salvo
que añadas `/PURGE`.

## 8. Problemas frecuentes

| Problema | Qué hacer |
|---|---|
| «Hay un puerto ocupado por otro programa» | Otro programa usa el puerto web. En la página **Red**, cambia el puerto (por ejemplo 8601) o cierra ese programa |
| «Hacen falta al menos 50 GB libres» | Elige otra carpeta en un disco con más espacio o libera espacio |
| Desde otro PC no se abre el panel | Comprueba que la red es **Privada** (Configuración → Red e Internet → Propiedades) y que usas `https://NOMBRE-DEL-PC:8643/` |
| «Hay una actualización en curso» | Espera unos minutos y vuelve a ejecutar el instalador |
| La comprobación final falla | Pulsa **Guardar informe…** y envía el `.zip` a soporte técnico. El registro de la instalación está en la carpeta temporal de Windows (`Setup Log …txt`) o donde indicaste con `/LOG` |
| Aviso de Windows 10 | El equipo necesita las actualizaciones ESU de pago; lo recomendable es Windows 11 |

## 9. Nota técnica

- Diseño: [PLAN-V2.md](PLAN-V2.md) §1.4 (instalador), §2.3-§2.6 (visor, disco, actualizaciones y red) y
  [CONTRATO.md](CONTRATO.md) §13-§15.
- Compilar el instalador: [EMPAQUETADO.md](EMPAQUETADO.md).
- El instalador no configura el equipo por su cuenta: servicios, permisos, firewall, HTTPS y versión activa los
  hace la herramienta `vmsctl.exe` de cada versión. El instalador solo copia archivos, guarda los ajustes del
  asistente (`.env` y, en una instalación nueva, `config.json`) y crea el grupo **VMS Operadores**.
- La versión 1 (`deploy\windows\install.ps1`) se mantiene solo para equipos que todavía la tienen; su guía está
  en el historial de este archivo.
