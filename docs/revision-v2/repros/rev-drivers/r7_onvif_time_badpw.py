"""ONVIF device_time con contraseña guardada mala: la hora (sin autenticar) se lee, pero GetNTP gasta un intento y tira todo."""
import asyncio, sys
sys.path.insert(0, ".")
from tests.vendors.conftest import asgi, make_device
from tools.mocks.onvif import OnvifMock
from tools.mocks.hikvision import HikvisionMock
from vms.vendors import client_for

async def main():
    mock = OnvifMock(password="Buena#1234", clock_offset_s=600)
    for i in range(3):   # 3 comprobaciones horarias
        c = client_for(make_device("onvif"), "Mala#0000", transport=asgi(mock.app))
        try:
            t = await c.device_time(); print("ONVIF hora:", t.skew_s)
        except Exception as e:
            print(f"ONVIF ciclo {i+1}: {type(e).__name__}: {e.message[:60]} | rechazos acumulados en el equipo={mock.rejected}")
        finally:
            await c.aclose()
    hik = HikvisionMock(password="Buena#1234", kind="camera")
    for i in range(3):
        c = client_for(make_device("hikvision"), "Mala#0000", transport=asgi(hik.app))
        try:
            await c.device_time()
        except Exception as e:
            print(f"Hik ciclo {i+1}: {type(e).__name__}: {e.message[:70]} | fallos={hik.auth.rejected_credentials}")
        finally:
            await c.aclose()
asyncio.run(main())
