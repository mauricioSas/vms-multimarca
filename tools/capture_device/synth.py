"""Fixtures SINTÉTICAS para todos los drivers: la herramienta de captura de verdad contra los simuladores.

    python -m tools.capture_device.synth [--out tests/vendors/fixtures] [--only hikvision,dahua]

Se capturan con `vms.vendors.capture.capture()` (el mismo código que con un equipo real) contra los mocks de
`tools/mocks` y el servidor RTSP caótico, y se marcan `"synthetic": true` y `"source": "simulador"`. **No
cuentan como «probado con respuestas reales»**: la madurez `fixtures` exige al menos una carpeta capturada de
un equipo real (`synthetic: false`), lo comprueba `tests/vendors/test_maturity_matches_matrix.py`.

Las pistas de descubrimiento de los perfiles salen de la documentación pública de cada marca (scopes ONVIF
con su nombre y un modelo de ejemplo), no de una captura.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tools.mocks.dahua import DahuaMock
from tools.mocks.hikvision import HikvisionMock
from tools.mocks.onvif import OnvifMock, OnvifProfile, brand_scopes, dahua_scopes, hikvision_scopes
from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.core.interfaces import DetectionHints
from vms.core.models import DeviceBase, DeviceKind
from vms.vendors.capture import capture

PASSWORD = "Sim#Pass:1@/x"
ROOT = Path(__file__).resolve().parents[2] / "tests" / "vendors" / "fixtures"


@dataclass
class Case:
    driver: str
    kind: DeviceKind
    hints: DetectionHints
    app: Callable[[str, int], Any] | None = None     # (base_url, rtsp_port) → app ASGI del mock
    scenario: str = ""                                # escenario del servidor RTSP (códec)
    notes: str = ""


def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _onvif(media2: bool, single: bool, encoding: str = "H264", model: str = "FD9389-HV") -> Callable[[str, int], Any]:
    def make(base: str, rtsp_port: int) -> Any:
        profiles = [OnvifProfile("Profile_1", "main", 2592, 1944, "/live1.sdp", encoding=encoding)]
        if not single:
            profiles.append(OnvifProfile("Profile_2", "sub", 640, 480, "/live2.sdp"))
        return OnvifMock(base_url=base, rtsp_base=f"rtsp://127.0.0.1:{rtsp_port}", password=PASSWORD,
                         manufacturer="VIVOTEK", model=model, serial="0002D1A2B3C4", firmware="0100a",
                         mac="00:02:d1:a2:b3:c4", profiles=profiles, media2=media2).app
    return make


def _ajax(base: str, rtsp_port: int) -> Any:
    return OnvifMock(base_url=base, rtsp_base=f"rtsp://127.0.0.1:{rtsp_port}", password=PASSWORD,
                     manufacturer="Ajax Systems", model="TurretCam", serial="AJX00A1B2C3", firmware="2.1.0",
                     mac="00:1e:c0:a1:b2:c3",
                     profiles=[OnvifProfile("main", "main", 2688, 1520, "/main_stream"),
                               OnvifProfile("sub", "sub", 640, 360, "/sub_stream")]).app


CASES: list[Case] = [
    Case("hikvision", "nvr", DetectionHints(sadp=True, model="DS-7608NI-K2/8P", scopes=hikvision_scopes("DS-7608NI-K2/8P")),
         lambda b, p: HikvisionMock(password=PASSWORD).app, notes="NVR de 4 canales: canal 3 sin vídeo, canal 4 H.265"),
    Case("hikvision", "camera", DetectionHints(sadp=True, model="DS-2CD2143G2-I", scopes=hikvision_scopes()),
         lambda b, p: HikvisionMock(kind="camera", password=PASSWORD, firmware="V5.7.3",
                                    firmware_released="build 220112").app),
    Case("hikvision", "dvr", DetectionHints(sadp=True, model="DS-7204HQHI-K1"),
         lambda b, p: HikvisionMock(kind="dvr", password=PASSWORD).app,
         notes="DVR híbrido: 4 analógicas (1-4) y canales IP desde el 33 (no verificado con hardware)"),
    Case("dahua", "nvr", DetectionHints(dhip=True, model="DHI-NVR4208-8P-4KS2/L", manufacturer="Dahua",
                                        scopes=dahua_scopes("DHI-NVR4208-8P-4KS2/L")),
         lambda b, p: DahuaMock(password=PASSWORD).app, notes="NVR de 4 canales: canal 3 sin vídeo"),
    Case("dahua", "xvr", DetectionHints(dhip=True, model="DH-XVR5108HS-I3", manufacturer="Dahua"),
         lambda b, p: DahuaMock(kind="xvr", password=PASSWORD).app,
         notes="XVR: 4 analógicas y 4 IP desde el canal 5 (no verificado con hardware)"),
    Case("dahua", "camera", DetectionHints(scopes=dahua_scopes()), lambda b, p: DahuaMock(kind="camera", password=PASSWORD).app,
         scenario="h265"),
    Case("onvif", "camera", DetectionHints(scopes=brand_scopes("VIVOTEK", "FD9389-HV")), _onvif(True, False, "H265"),
         scenario="h265", notes="Profile T (Media2) con el principal en H.265"),
    Case("onvif", "camera", DetectionHints(scopes=brand_scopes("VIVOTEK", "FD8166A")), _onvif(False, True, model="FD8166A"),
         notes="Solo Media1 y un único perfil"),
    Case("generic", "camera", DetectionHints()),
    Case("ezviz", "camera", DetectionHints(sadp=True, model="CS-C6N-A0-1C2WFR")),
    Case("imou", "camera", DetectionHints(dhip=True, manufacturer="Imou", model="IPC-A22EP",
                                          scopes=brand_scopes("Imou", "IPC-A22EP"))),
    Case("uniview", "camera", DetectionHints(scopes=brand_scopes("UNV", "IPC2122LB-SF28-A"))),
    Case("tplink-vigi", "camera", DetectionHints(scopes=brand_scopes("VIGI", "VIGI C440"))),
    Case("tapo", "camera", DetectionHints(scopes=brand_scopes("Tapo", "C210"))),
    Case("hanwha", "camera", DetectionHints(scopes=brand_scopes("Hanwha Vision", "XNO-6080R"))),
    Case("axis", "camera", DetectionHints(scopes=brand_scopes("AXIS", "M1065-L"))),
    Case("ajax", "camera", DetectionHints(scopes=brand_scopes("Ajax", "TurretCam")), _ajax,
         notes="Ruta de vídeo leída por ONVIF (en el equipo real se copia de la app); RTSP en el 8554"),
    Case("reolink", "camera", DetectionHints(scopes=brand_scopes("Reolink", "RLC-510A"))),
    Case("bosch", "camera", DetectionHints(scopes=brand_scopes("Bosch", "NBN-73023BA"))),
]


async def build(out: Path, only: set[str] | None = None) -> list[Path]:
    written: list[Path] = []
    for case in CASES:
        if only and case.driver not in only:
            continue
        async with RtspChaosServer(password=PASSWORD, scenario=case.scenario) as chaos:
            base = "http://127.0.0.1:80"
            transport = httpx.ASGITransport(app=case.app(base, chaos.port)) if case.app else None
            dev = DeviceBase(name="captura", vendor=case.driver, kind=case.kind, host="127.0.0.1", http_port=80,
                             rtsp_port=chaos.port, onvif_port=None if case.app else _closed_port(), username="admin")
            folder = await capture(dev, PASSWORD, out / case.driver, transport=transport, source="simulador",
                                   synthetic=True, hints=case.hints, notes=case.notes,
                                   record_failure=case.app is not None)
            written.append(folder)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera fixtures sintéticas con la herramienta de captura")
    ap.add_argument("--out", type=Path, default=ROOT)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    folders = asyncio.run(build(args.out, set(filter(None, args.only.split(","))) or None))
    for f in folders:
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
