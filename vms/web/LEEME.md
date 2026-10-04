# Interfaz web

HTML, CSS y JavaScript sin paso de compilación (módulos ES nativos). No carga nada de Internet:
los muros funcionan en una red sin salida. El backend la sirve en el puerto 8600.

## Páginas

| Ruta | Para qué | Quién |
|---|---|---|
| `/login` | Iniciar sesión. Vuelve a la página pedida (`?next=`, solo rutas internas). | todos |
| `/setup` | Primer arranque: crear el administrador (solo desde el propio equipo). | — |
| `/` | Equipos (alta, prueba de conexión, búsqueda ONVIF, importar canales), cámaras y asignación de cámaras a los 4 monitores. | operador (ver y monitores), administrador (todo) |
| `/wall/1` … `/wall/4` | Muro de un monitor a pantalla completa. | también kiosco |
| `/playback` | Reproducción con línea de tiempo y descarga de clips. | operador |
| `/analytics` | Dibujar la línea de puerta y las zonas de cola sobre una imagen de la cámara. | administrador |
| `/status` | Estado del motor, cámaras, grabación, disco y analítica. | operador |

## Muro (`/wall/N`)

- Vídeo en vivo por WebRTC (WHEP) con el subflujo; la celda ampliada y la distribución de 1 usan
  el flujo principal.
- **Doble clic** (o Intro) en una celda: la amplía. Doble clic o **Escape**: vuelve a la rejilla.
  Mientras hay una celda ampliada, las demás paran su vídeo para no gastar CPU.
- **F**: pantalla completa. Al mover el ratón aparece una barra con el nombre del monitor, la
  hora y la distribución (1/4/9/16). En modo kiosco la barra no permite cambiar nada.
- Estados de cada celda: **En vivo**, **Conectando**, **Reconectando** y **Sin señal**. Si el
  vídeo se corta, la celda reintenta sola con esperas crecientes (1, 2, 5, 10 y 30 s); si el
  servidor avisa de que la cámara ha vuelto, reintenta en el momento.
- Si alguien cambia el monitor desde el panel, el muro se actualiza solo, sin recargar, y solo
  se reconectan las celdas que cambian.
- Pensado para funcionar días sin intervención: cada intento de conexión cierra el anterior y
  avisa al servidor; si el vídeo deja de avanzar durante 12 s se reconecta aunque el navegador
  crea que sigue conectado; y, si la página lleva más de 20 h abierta, se recarga sola a las 4:00.

## Reproducción (`/playback`)

Elige cámara y día. Los tramos verdes de la línea de tiempo son lo grabado. Clic en la línea para
reproducir desde ese momento; rueda del ratón o los botones 24 h / 6 h / 1 h para acercar.
Botones de −1 min, −10 s, +10 s y +1 min (o flechas del teclado con la línea seleccionada; con
Mayúsculas, saltos de 10 min). «Descargar clip» genera un MP4 desde la grabación original.

## Analítica (`/analytics`)

- **+ Línea de puerta**: clic en un extremo de la puerta y clic en el otro. La flecha ámbar
  marca el sentido de **entrada**; «Invertir el sentido de entrada» lo cambia.
- **+ Zona de cola**: un clic por esquina; se cierra con clic en el primer punto, doble clic o
  Intro. Retroceso borra el último punto y Escape cancela.
- Con una regla seleccionada se arrastran sus puntos o la regla entera.
- Umbral de aviso: número de personas en la zona durante un tiempo seguido, pausa mínima entre
  avisos y valor por debajo del cual termina el aviso.
- La imagen se pide a la cámara al abrir la página, vive solo en la memoria del navegador y no se
  guarda en ningún sitio.

## Probar la interfaz sin cámaras

```bash
# simulador de cámaras + MediaMTX real + backend de pruebas, con dos NVR ya dados de alta
.venv/bin/python -m tests.web.stub_backend --camsim
# abre http://127.0.0.1:8600/  (admin / admin-pass-1234)
```

Pruebas automáticas en Chromium (Playwright), con capturas en `tests/e2e/screenshots/`:

```bash
.venv/bin/python -m pytest tests/web                   # interfaz contra el backend de pruebas
.venv/bin/python -m pytest tests/e2e/test_web_e2e.py   # vídeo real: WebRTC, grabación y reproducción
VMS_TEST_WEB_BACKEND=real .venv/bin/python -m pytest tests/web tests/e2e/test_web_e2e.py   # contra vms.api
```
