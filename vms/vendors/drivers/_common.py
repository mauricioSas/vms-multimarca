"""Ayudas compartidas por los drivers (no es un driver: empieza por «_» y el registro no lo carga)."""
from __future__ import annotations

from collections.abc import Callable

from vms.core.interfaces import StreamPreset
from vms.core.models import DeviceKind

PresetFn = Callable[[int, DeviceKind], StreamPreset]

# Avisos comunes, en lenguaje de instalador.
NOTE_H265_SUB = ("El subflujo tiene que ir en H.264 para verse en el navegador; el principal puede ser H.265 "
                 "(se graba igual).")
NOTE_SMART_CODEC = ("H.264+/H.265+/Smart Codec alargan el GOP: el vídeo tarda más en arrancar en el muro. "
                    "Desactívalos en el subflujo si puedes.")
HINT_READ_ONLY_USER = "Crea en el equipo un usuario de solo lectura para el VMS en lugar de usar el administrador."
HINT_FIXED_IP = "Pon al equipo una IP fija o una reserva DHCP en el router: si cambia de IP, deja de grabar."


def hikvision_style(channel: int, kind: DeviceKind) -> StreamPreset:
    """/Streaming/Channels/N01, N02 y N03 (Hikvision y marcas que copian ISAPI/RTSP de Hikvision)."""
    return StreamPreset(main=f"/Streaming/Channels/{channel}01", sub=f"/Streaming/Channels/{channel}02",
                        third=f"/Streaming/Channels/{channel}03")


def dahua_style(channel: int, kind: DeviceKind) -> StreamPreset:
    """/cam/realmonitor?channel=N&subtype=0|1|2 (Dahua, Imou y OEM). Usa query: MediaMTX la descarta en
    rutas propias (solo importa al simular)."""
    base = f"/cam/realmonitor?channel={channel}&subtype="
    return StreamPreset(main=base + "0", sub=base + "1", third=base + "2", query_safe=False)


def fixed(main: str, sub: str | None, third: str | None = None, *, rtsp_port: int = 554,
          query_safe: bool = True) -> PresetFn:
    """Preset con plantilla `{ch}` (canal 1..N) o rutas fijas de cámara de un solo canal."""
    def preset(channel: int, kind: DeviceKind) -> StreamPreset:
        def fmt(t: str | None) -> str | None:
            return None if t is None else t.format(ch=channel, ch0=channel - 1, ch2=f"{channel:02d}")
        main_p = fmt(main)
        assert main_p is not None
        return StreamPreset(main=main_p, sub=fmt(sub), third=fmt(third), rtsp_port=rtsp_port, query_safe=query_safe)
    return preset


__all__ = ["HINT_FIXED_IP", "HINT_READ_ONLY_USER", "NOTE_H265_SUB", "NOTE_SMART_CODEC", "PresetFn",
           "dahua_style", "fixed", "hikvision_style"]
