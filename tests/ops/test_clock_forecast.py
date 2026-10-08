"""Hora (CONTRATO §18.3) y previsión de días de grabación (§18.4). Criterios 2 y 3 de B6.

Desfase con dobles ONVIF/ISAPI/CGI (formato real de cada marca) a ±2 h y con hora manual. La previsión se
prueba con un disco simulado (segmentos con el nombre exacto de MediaMTX y un `disk_usage` de mentira).
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

import pytest

from tests.ops.doubles import ClockDevice, DahuaClock, IsapiClock, OnvifClock, dahua_app, isapi_app, onvif_app
from vms.core.interfaces import DeviceClockClient
from vms.ops.health import clock as clk
from vms.ops.health.forecast import DiskStats, forecast, mbps_to_bytes_per_hour

READERS = [("isapi", isapi_app, IsapiClock), ("cgi", dahua_app, DahuaClock), ("onvif", onvif_app, OnvifClock)]


@pytest.mark.parametrize(("source", "app", "reader"), READERS, ids=[r[0] for r in READERS])
@pytest.mark.parametrize("skew_h", [2, -2, 0])
async def test_skew_from_each_protocol(source: str, app: object, reader: type, skew_h: int) -> None:
    dev = ClockDevice(skew=timedelta(hours=skew_h))
    client = reader(app(dev))   # type: ignore[operator]
    assert isinstance(client, DeviceClockClient)
    dt = await client.device_time()
    chk = clk.device_check("dev-00000001", None, dt, 2.0, 30.0)
    assert dt.source == source
    assert chk.skew_s == pytest.approx(skew_h * 3600, abs=1.6)   # los formatos van al segundo
    if skew_h:
        assert chk.status == "critical"
        assert ("adelantada" if skew_h > 0 else "atrasada") in chk.message_es and "2,0 h" in chk.message_es
    else:
        assert chk.status == "ok"
    await client.aclose()


@pytest.mark.parametrize(("source", "app", "reader"), READERS, ids=[r[0] for r in READERS])
async def test_manual_time_mode_is_a_warning_even_if_on_time(source: str, app: object, reader: type) -> None:
    client = reader(app(ClockDevice(mode="manual")))   # type: ignore[operator]
    chk = clk.device_check("dev-00000001", None, await client.device_time(), 2.0, 30.0)
    assert chk.time_mode == "manual" and chk.status == "warning" and "a mano" in chk.message_es
    await client.aclose()


def test_thresholds() -> None:
    assert clk.evaluate(1.0, "ntp", 2, 30)[0] == "ok"
    assert clk.evaluate(-5.0, "ntp", 2, 30)[0] == "warning"
    assert clk.evaluate(45.0, "ntp", 2, 30)[0] == "critical"
    assert clk.evaluate(None, "ntp", 2, 30)[0] == "unknown"
    assert "2 min atrasada" in clk.evaluate(-90, "ntp", 2, 30)[1]
    assert "1,5 s adelantada" in clk.evaluate(1.5, "ntp", 1, 30)[1]


class _SntpServer:
    """Servidor NTP de prueba en UDP local: responde con su reloj desplazado `offset` segundos."""

    def __init__(self, offset: float) -> None:
        self.offset = offset
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.sock.settimeout(5)
        try:
            data, addr = self.sock.recvfrom(48)
        except OSError:
            return
        now = time.time() + self.offset + clk.NTP_EPOCH_OFFSET
        pkt = bytearray(48)
        pkt[0] = 0x24
        struct.pack_into("!II", pkt, 24, *struct.unpack_from("!II", data, 40))
        for off in (32, 40):
            struct.pack_into("!II", pkt, off, int(now), int((now % 1) * 2**32))
        self.sock.sendto(bytes(pkt), addr)
        self.sock.close()


def test_pc_clock_with_sntp_server() -> None:
    srv = _SntpServer(offset=-40.0)   # el servidor va 40 s por detrás → el PC va 40 s adelantado
    skew, rtt = clk.sntp_offset("127.0.0.1", port=srv.port)
    assert skew == pytest.approx(40.0, abs=0.5) and rtt >= 0


def test_pc_clock_without_server_is_unknown_not_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clk, "windows_time_config", lambda: ("unknown", ""))
    chk = clk.pc_check(2.0, 30.0)
    assert chk.status == "unknown" and chk.skew_s is None
    monkeypatch.setattr(clk, "windows_time_config", lambda: ("manual", ""))
    assert clk.pc_check(2.0, 30.0).status == "warning"


# --------------------------------------------------------------------------- previsión
def _sparse(f: BinaryIO, size: int) -> None:
    """Archivo disperso: ocupa la cifra sin escribir los bytes. En Windows, truncate() escribe los ceros de verdad
    (aunque el archivo esté marcado como disperso) y esta prueba llenaba el disco (unos 170 GB): allí se marca como
    disperso (FSCTL_SET_SPARSE) y se escribe solo el último byte."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        returned = wintypes.DWORD()
        if not ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno())), 0x900C4,
                                                      None, 0, None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError()
        f.seek(size - 1)
        f.write(b"\0")
        return
    f.truncate(size)


def _segments(root: Path, cam: str, start: datetime, hours: float, size: int, every_min: int = 15) -> None:
    folder = root / cam / "main"
    folder.mkdir(parents=True, exist_ok=True)
    t = start
    while t < start + timedelta(hours=hours):
        name = t.strftime("%Y-%m-%d_%H-%M-%S") + "-000000+0000.mp4"
        with open(folder / name, "wb") as f:
            _sparse(f, size)
        t += timedelta(minutes=every_min)
    (folder / "basura.txt").write_text("no es un segmento")


def test_forecast_with_simulated_disk(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    seg = 450_000_000   # 15 min a 4 Mbit/s
    _segments(tmp_path, "cam-00000001", now - timedelta(days=3), 72, seg)
    _segments(tmp_path, "cam-00000002", now - timedelta(days=1), 24, seg)
    rate = seg * 4   # bytes/hora de cada cámara
    used = sum(f.stat().st_size for f in tmp_path.rglob("*.mp4"))
    tb = 1_000_000_000_000

    def disk(total: int, free: int) -> object:
        return lambda _p: DiskStats(total, total - free, free)

    fc = forecast(tmp_path, ["cam-00000001", "cam-00000002"], 30, 90, now=now, disk_usage=disk(10 * tb, 8 * tb))
    cams = {c.camera_id: c for c in fc.cameras}
    assert cams["cam-00000001"].bytes_per_hour == pytest.approx(rate, rel=0.05)
    assert cams["cam-00000001"].days_on_disk == pytest.approx(3.0, abs=0.01)
    capacity = used + 8 * tb - 0.10 * 10 * tb
    assert fc.forecast_days == pytest.approx(capacity / (2 * rate * 24), rel=0.05)
    assert fc.status == "ok" and not fc.rgpd_warning
    # disco pequeño: no llega al objetivo
    small = forecast(tmp_path, ["cam-00000001", "cam-00000002"], 30, 90, now=now, disk_usage=disk(1 * tb, 100 * 10**9))
    assert small.status in ("warning", "critical") and "Amplía el disco" in small.message_es
    # objetivo de más de 30 días: aviso RGPD
    assert forecast(tmp_path, ["cam-00000001"], 45, 90, now=now, disk_usage=disk(10 * tb, 8 * tb)).rgpd_warning
    # simulador: añadir 4 cámaras de 4 Mbit/s reduce los días en proporción
    sim = forecast(tmp_path, ["cam-00000001", "cam-00000002"], 30, 90, now=now, disk_usage=disk(10 * tb, 8 * tb),
                   extra_bytes_per_hour=4 * mbps_to_bytes_per_hour(4.0))
    assert sim.forecast_days == pytest.approx(fc.forecast_days * (2 * rate) / (2 * rate + 4 * rate), rel=0.05)
    # lo que la retención borrará (más viejo que el objetivo): nada todavía
    assert fc.reclaimable == 0


def test_forecast_without_recordings_says_so(tmp_path: Path) -> None:
    fc = forecast(tmp_path, ["cam-00000001"], 30, 90, disk_usage=lambda _p: DiskStats(10**12, 0, 10**12))
    assert fc.status == "ok" and "Todavía no hay grabaciones" in fc.message_es
