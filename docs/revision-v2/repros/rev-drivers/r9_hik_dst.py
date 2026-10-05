"""Hik: localTime sin offset + timeZone POSIX con DST (Madrid). Equipo en hora exacta -> desfase de 1 h en verano."""
import asyncio, sys
sys.path.insert(0, ".")
from datetime import datetime
from zoneinfo import ZoneInfo
from starlette.responses import Response
from starlette.routing import Route, request_response
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock, NS, XML
from vms.vendors import client_for

hik = HikvisionMock(password=TEST_PASSWORD, kind="camera")
async def t(request):
    local = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)     # hora exacta, sin offset
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n<timeMode>NTP</timeMode>\n'
            f"<localTime>{local.isoformat(timespec='seconds')}</localTime>\n"
            "<timeZone>CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00</timeZone>\n</Time>\n")
    return Response(body, media_type=XML)
for r in hik.app.inner.routes:
    if isinstance(r, Route) and r.path == "/ISAPI/System/time":
        r.endpoint, r.app = t, request_response(t)

async def main():
    c = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(hik.app))
    try:
        dt = await c.device_time()
    finally:
        await c.aclose()
    print("device_time:", dt.device_time.isoformat(), "skew_s:", round(dt.skew_s))
asyncio.run(main())
