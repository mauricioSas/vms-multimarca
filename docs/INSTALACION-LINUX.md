# Instalación en Linux (mini PC de tienda y servidor central)

Sistema recomendado: **Ubuntu Server 24.04 LTS** (trae Python 3.12 de serie). Debian 12 trae
Python 3.11 y Debian 13 Python 3.13: el programa necesita exactamente 3.12, así que en Debian
tendrías que instalar Python 3.12 aparte; para los mini PC usa Ubuntu 24.04.

## Componentes

| Componente | Unidad systemd | Proceso |
|---|---|---|
| backend (vídeo, grabación, web) | `vms.service` | `python -m vms` (arranca MediaMTX como hijo) |
| analítica | `vms-analytics.service` | `python -m analytics` |
| latido HTTP | `vms-heartbeat.service` | `python -m central.agent` |
| panel central | `vms-central.service` | `python -m central` |
| informe semanal | `vms-central-reports.timer` + `.service` | `python -m analytics.reports` (lunes 06:00 Madrid) |

Configuraciones típicas:

| Equipo | `--components` |
|---|---|
| Mini PC de tienda que graba y cuenta | `backend,analytics,heartbeat` (por defecto) |
| Mini PC solo de analítica (el vídeo está en otro PC) | `analytics,heartbeat` con `VMS_ANALYTICS_BACKEND_URL` apuntando al backend de la tienda. **No soportado por defecto:** la analítica lee el vídeo del MediaMTX local (127.0.0.1); para leerlo de otro PC hay que exponer su RTSP con un usuario lector (CONTRATO §4.1). Lo recomendado es que el mismo equipo grabe y cuente |
| Servidor central | `central` |

## Instalación

```bash
# copia la carpeta del programa al equipo (scp, USB...) y entra en ella
sudo deploy/linux/install.sh --site-id site-bcn-001 --central-url https://central.vpn:8700
```

El script:
1. Comprueba systemd y la arquitectura (x86_64 o arm64) e instala `python3.12` y
   `python3.12-venv` con apt si faltan.
2. Crea el usuario de sistema `vms` (sin shell) y las carpetas `/opt/vms-multimarca` (programa,
   de root), `/etc/vms-multimarca` (configuración, 0750) y `/var/lib/vms-multimarca` (datos y
   grabaciones, del usuario `vms`).
3. Copia el programa, crea el entorno de Python e instala las dependencias de los archivos de
   bloqueo (`--no-deps --only-binary`).
4. Descarga MediaMTX v1.21.1 y comprueba su SHA-256.
5. Crea `/etc/vms-multimarca/vms.env` desde `.env.example` (permisos 0640 root:vms; nunca pisa uno
   existente) con `VMS_DATA_DIR`, `VMS_CREDENTIAL_BACKEND=file` (no hay llavero sin sesión
   gráfica) y un token de kiosco aleatorio.
6. Instala y habilita las unidades systemd (con aislamiento: `ProtectSystem=strict`,
   `NoNewPrivileges`, `PrivateTmp`, etc.).
7. Si `ufw` está activo, permite 8600/tcp y 8189/udp+tcp (y 8700/tcp en el central) **solo desde
   redes privadas y la VPN** (10/8, 172.16/12, 192.168/16, 100.64/10).
8. Arranca los servicios y comprueba `http://127.0.0.1:8600/api/health`. El latido y el panel
   central no se arrancan hasta que completes sus variables (te lo indica).

Opciones: `--prefix`, `--wheelhouse DIR` y `--downloads DIR` (instalación sin Internet),
`--no-firewall`, `--no-start`, `--help`.

## Después de instalar

```bash
sudoedit /etc/vms-multimarca/vms.env      # VMS_PG_DSN, VMS_SITE_TOKEN, VMS_TELEGRAM_BOT_TOKEN...
sudo systemctl restart vms vms-analytics vms-heartbeat
systemctl status vms vms-analytics vms-heartbeat
journalctl -u vms -f                      # registro en directo
ls /var/lib/vms-multimarca/logs           # registros rotativos de cada servicio
```

Primer administrador: desde el propio equipo (`http://127.0.0.1:8600/setup`, por ejemplo con
`ssh -L 8600:127.0.0.1:8600 tienda`) o definiendo `VMS_ADMIN_INITIAL_PASSWORD` antes del primer
arranque.

**Grabaciones en otro disco:** monta el disco (p. ej. en `/srv/grabaciones`, propietario `vms`),
indícalo en *Ajustes → Grabación* y permite la escritura al servicio:
```bash
sudo systemctl edit vms     # y añade:
[Service]
ReadWritePaths=/srv/grabaciones
```

## Desinstalar

```bash
sudo deploy/linux/uninstall.sh            # conserva /etc/vms-multimarca y /var/lib/vms-multimarca
sudo deploy/linux/uninstall.sh --purge    # borra también datos y grabaciones (pide escribir BORRAR)
```

## Validación

`install.sh` y `uninstall.sh` pasan `bash -n` y `shellcheck` sin avisos, y las unidades se
validan en las pruebas automáticas (secciones, directivas, rutas). **No se han probado todavía en
un Ubuntu real ni con `systemd-analyze verify`** (el desarrollo se hizo en macOS): en la primera
instalación ejecuta `systemd-analyze verify /etc/systemd/system/vms*.service` y sigue
[CHECKLIST-PRUEBAS.md](CHECKLIST-PRUEBAS.md).
