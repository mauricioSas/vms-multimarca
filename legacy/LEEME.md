# legacy/ — código anterior (NO forma parte del producto)

Aquí está la versión 2.0 de escritorio (PySide6 + PyAV + ffmpeg propio) que el plan
aprobado sustituye por MediaMTX + interfaz web. Se conserva solo como referencia.

- `app/live.py`, `app/ffmpeg_bin.py`: dependían de PyAV / imageio-ffmpeg, cuyas
  wheels de PyPI traen FFmpeg compilado con GPL. **Prohibido importarlos.**
- `app/segments.py`, `app/retention.py`: grabación y retención propias. MediaMTX las
  sustituye (`recordDeleteAfter`). La lógica de retención por % de disco de
  `retention.py` sirve de referencia para el «disk guard» de `vms/engine`.
- `app/config.py`, `app/credentials.py`, `app/rtsp.py`, `app/log.py`, `app/paths.py`,
  `app/models.py`, `app/autostart.py`: ya portados a `vms/core/`.

Esta carpeta no se empaqueta (ver `pyproject.toml`) y pytest no la recorre.
