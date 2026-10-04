# `tools/release/`

**Dueño:** B4.

Herramientas de publicación: `tuf_repo.py`, `publish.py`, `channel.py`, `mirror.py` (USB de 60 días) y `site_token.py`. La firma de `targets` y `root` se hace en local con la llave física (en desarrollo: SoftHSM o claves software marcadas `dev`).

**Referencia:** PLAN-V2 §1.6, §2.7. La prueba S3 (`spikes/s3-tuf/`) es la referencia.
