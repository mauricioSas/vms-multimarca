# Guía de red y seguridad

## Principios

1. **Las cámaras y NVR nunca se exponen a Internet.** Nada de abrir puertos en el router, ni P2P
   del fabricante (Hik-Connect, DMSS/Easy4ip), ni DDNS hacia las cámaras.
2. **Las cámaras viven en una VLAN aislada**, sin salida a Internet.
3. **El acceso remoto va por VPN** (Headscale/Tailscale): la tienda «sale» hacia la VPN y no se
   abre ningún puerto de entrada en el router.
4. **El firewall solo deja pasar lo necesario**, y solo desde redes privadas.

## Esquema recomendado por tienda

```
 Internet ── router/firewall de la tienda (sin puertos abiertos hacia dentro)
                │
                ├── VLAN 10  LAN de la tienda (TPV, oficina, PC de control)
                │      └── PC VMS: interfaz 1 (192.168.1.50)
                │
                └── VLAN 20  CÁMARAS (sin Internet, sin ruta a VLAN 10)
                       ├── NVR Hikvision 192.168.20.10
                       ├── NVR Dahua     192.168.20.11
                       └── PC VMS: interfaz 2 (192.168.20.2)   ← única puerta entre ambas
```

- La forma más sencilla: el PC del VMS con **dos tarjetas de red**, una en cada VLAN, y **sin
  enrutar** entre ellas (en Windows, no actives «Compartir conexión»). El VMS lee las cámaras por
  la VLAN 20 y sirve la interfaz web y el vídeo por la VLAN 10.
- Alternativa con una sola tarjeta: el router/switch permite **solo** el tráfico del PC del VMS
  hacia la VLAN de cámaras (RTSP 554/tcp, HTTP 80/tcp, ONVIF) y bloquea el resto.
- En la VLAN de cámaras: bloquea la salida a Internet (actualizaciones del fabricante a mano, con
  el firmware descargado de la web oficial), desactiva UPnP y P2P en cada equipo.
- Cambia las contraseñas por defecto de todos los equipos y usa el **usuario de solo lectura**
  para el VMS ([ALTA-EQUIPOS.md](ALTA-EQUIPOS.md)).

## Puertos

| Puerto | Dónde escucha | Desde dónde se permite |
|---|---|---|
| 8600/tcp (web y API, HTTP) | PC VMS, todas las interfaces; **solo 127.0.0.1 si activas HTTPS** | VPN (cifrada); en la LAN, mejor por HTTPS |
| 8643/tcp (web y API, HTTPS, opcional) | PC VMS, todas las interfaces | LAN de la tienda y VPN |
| 8189/udp y 8189/tcp (vídeo WebRTC) | PC VMS | LAN de la tienda y VPN |
| 8554, 8889, 9996, 9997, 9998 (MediaMTX interno) | **solo 127.0.0.1**, y con usuario y contraseña internos | nadie de fuera; dentro del PC solo el backend y la analítica |
| 554/tcp, 80/tcp (cámaras) | cámaras/NVR | solo el PC VMS |
| 8700/tcp (panel central) | servidor central | VPN |
| 5432/tcp (PostgreSQL) | servidor central | VPN (solo IPs de las tiendas) |

El instalador de Windows abre 8600 y 8189 **solo en el perfil «Privado»** y solo para los
programas del VMS. Si Windows marca la red como «Pública», las reglas no aplican: cámbiala con
`Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private`. En Linux, el
instalador añade reglas de `ufw` solo desde 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 y
100.64.0.0/10 (VPN).

## Web cifrada (HTTPS) en la LAN de la tienda

Por HTTP sin cifrar, quien escuche la red de la tienda vería el login y, al dar de alta un equipo,
su contraseña. Por la VPN no pasa (WireGuard cifra), pero en la LAN sí. Si alguien va a abrir la
web desde otro PC de la tienda, activa HTTPS:

1. En el PC del VMS: `python -m vms tls-cert --host <nombre-del-pc> --ip <IP en la LAN>` (en
   Windows, en una consola de administrador dentro de `C:\Program Files\VMSMultimarca`, con el Python
   de la instalación: `python\python.exe -m vms tls-cert ...` o `.venv\Scripts\python.exe -m vms tls-cert ...`).
   Crea `secrets\tls\vms.crt` y `vms.key` en la carpeta de datos y muestra las dos líneas para el `.env`.
2. Añade al `.env` `VMS_TLS_CERT_FILE=…` y `VMS_TLS_KEY_FILE=…` y reinicia el servicio VMSBackend.
3. Abre el puerto 8643/tcp en el firewall (perfil Privado) y, si quieres, cierra el 8600 hacia
   la LAN: con HTTPS activo, el HTTP solo escucha en 127.0.0.1 (lo usan los muros en kiosco, la
   analítica y el agente del mismo PC, que no salen del equipo).
4. En los PC que abran la web, importa `vms.crt` como raíz de confianza
   (`certutil -addstore Root vms.crt` como administrador) para que el navegador no avise.
   Si el cliente tiene su propia entidad de certificación, usa un certificado suyo.

La cookie de sesión lleva `Secure` cuando se entra por HTTPS. El instalador todavía no lo activa
solo (pendiente de probar en Windows): se hace a mano con estos pasos.

## VPN con Headscale/Tailscale

**Headscale** (BSD-3) es el servidor de coordinación autoalojado; en cada tienda se instala el
cliente **Tailscale** (BSD-3). El tráfico va cifrado (WireGuard) y punto a punto.

1. Servidor central (Linux): instala Headscale siguiendo su documentación oficial, con un dominio
   y certificado TLS. Crea un usuario por cliente: `headscale users create covert`.
2. Genera una clave de preautorización: `headscale preauthkeys create --user covert --reusable
   --expiration 24h --tags tag:tienda`.
3. En cada PC de tienda instala Tailscale y únelo:
   - Windows: `tailscale up --login-server https://headscale.ejemplo.com --authkey <clave>
     --hostname tienda-bcn-001 --unattended`
   - Linux: `sudo tailscale up --login-server https://headscale.ejemplo.com --authkey <clave>
     --hostname tienda-bcn-001`
4. **ACL** (política de Headscale): las tiendas solo pueden hablar con el servidor central
   (5432 y 8700) y el personal de Covert solo con las tiendas (8600, 8189). Las tiendas **no** se
   ven entre sí. Ejemplo:
   ```json
   {"acls": [
     {"action": "accept", "src": ["tag:tienda"], "dst": ["tag:central:5432,8700"]},
     {"action": "accept", "src": ["group:covert"], "dst": ["tag:tienda:8600,8189", "tag:central:8700"]}
   ]}
   ```
5. **No anuncies rutas de subred** de la VLAN de cámaras: el acceso remoto llega al PC del VMS,
   nunca directamente a las cámaras.
6. Para ver vídeo en remoto por la VPN, añade la IP de Tailscale del PC al `.env`:
   `VMS_MTX_WEBRTC_ADDITIONAL_HOSTS=["100.64.0.12"]` y reinicia el servicio. En Windows marca la
   interfaz de Tailscale como red privada si aparece como pública.

## PostgreSQL central

- Escucha solo en la IP de la VPN (`listen_addresses = '100.64.0.1'`).
- `pg_hba.conf`: `hostssl vms all 100.64.0.0/10 scram-sha-256` (TLS y contraseña obligatorios; cada
  tienda entra con su propio rol).
- Roles (ver CONTRATO §7.1): **un rol por tienda** creado con
  `python -m vms.db.site_roles create <sede> --dsn …` (con Row Level Security solo ve y escribe
  las filas de su sede) y `vms_central` (lectura + informes) para el panel. No compartas un mismo
  rol entre tiendas: un PC comprometido podría modificar los datos de todas
  ([PANEL-CENTRAL.md](PANEL-CENTRAL.md)).
- DSN de una tienda: `postgresql://vms_site_bcn_001:CLAVE@100.64.0.1:5432/vms?sslmode=require`.

## Latido sin acceso a la base de datos

Si una tienda no debe tener credenciales de PostgreSQL (por ejemplo, una tienda sin analítica),
usa el **agente de latido HTTP** (servicio VMSHeartbeat / vms-heartbeat): envía su estado al panel
central por HTTPS con un **token propio de la sede** ([PANEL-CENTRAL.md](PANEL-CENTRAL.md)).

## Endurecimiento del PC

- Windows Update activo (fuera del horario de la tienda), antivirus con exclusión de la carpeta
  de grabaciones (mejora el rendimiento de escritura) pero no de la carpeta del programa.
- Cuenta del kiosco sin permisos de administrador.
- BIOS con contraseña y arranque solo desde el disco interno si el PC está en zona accesible.
