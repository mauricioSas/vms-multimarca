# `vms/vendors/drivers/`

**Dueño:** B5. Un archivo por marca. El registro (`vms/vendors/registry.py`) importa todos los `.py` de
esta carpeta que no empiezan por `_` y cada uno se registra solo con `register(DriverSpec(...))`.

## Añadir una marca

1. Crea `drivers/<id>.py` (el id va en minúsculas, con guiones: `tplink-vigi` → archivo `tplink_vigi.py`)
   con su `DriverSpec`: marcas, tipos de equipo, puertos, autenticación, bloqueo, presets RTSP, detector,
   avisos (`notes_es`), pasos previos (`setup_hints_es`) y **madurez real** (`experimental`, `community`,
   `fixtures` o `verified`).
2. Añade fixtures en `tests/vendors/fixtures/<id>/<modelo>__<firmware>/` (`meta.json`, `rtsp/` y
   `discovery/`, más `http/` u `onvif/` si el driver tiene API). Con un equipo real:
   `python -m tools.capture_device --host … --driver <id> --user … --out tests/vendors/fixtures/<id>/`.
3. Añade su fila a `docs/COMPATIBILIDAD.md` con la misma madurez.

No hace falta tocar el registro, la API, la interfaz (`GET /api/vendors` la alimenta) ni el motor: la
batería `tests/vendors/contract/` recorre registro × fixtures y `tests/vendors/test_registry.py` comprueba
que cada driver cumple las reglas (rutas que empiezan por `/`, sin `//` salvo que se declare, etc.).

`_common.py` reúne presets y ayudas compartidas (no es un driver).
