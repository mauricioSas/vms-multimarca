"""Misma avería (zona horaria del equipo mal: hora en pantalla/OSD +6 h, UTC bien por NTP) -> veredicto distinto por marca."""
import asyncio, sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from starlette.responses import Response
from starlette.routing import Route, request_response
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock, NS, XML
from tools.mocks.dahua import DahuaMock
from vms.vendors import client_for
from vms.ops.health.clock import device_check

# Hikvision de fábrica: zona CST-8 (China), NTP ok. localTime = UTC+8 con su offset.
hik = HikvisionMock(password=TEST_PASSWORD, kind="camera")
async def hik_time(request):
    local = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n<timeMode>NTP</timeMode>\n'
            f"<localTime>{local.isoformat(timespec='seconds')}</localTime>\n<timeZone>CST-8:00:00</timeZone>\n</Time>\n")
    return Response(body, media_type=XML)
for r in hik.app.inner.routes:
    if isinstance(r, Route) and r.path == "/ISAPI/System/time":
        r.endpoint, r.app = hik_time, request_response(hik_time)

# Dahua con la misma avería: su hora local va +6 h respecto a la del PC de Madrid (UTC+8 vs UTC+2)
pc_off = datetime.now().astimezone().utcoffset().total_seconds()
dah = DahuaMock(password=TEST_PASSWORD, kind="camera", clock_offset_s=8 * 3600 - pc_off)

async def main():
    for name, mock in (("hikvision", hik), ("dahua", dah)):
        c = client_for(make_device(name), TEST_PASSWORD, transport=asgi(mock.app))
        try:
            t = await c.device_time()
        finally:
            await c.aclose()
        chk = device_check("dev-00000001", None, t, 2.0, 30.0)
        print(f"{name:10} device_time={t.device_time.isoformat()} skew={t.skew_s:+.0f}s -> {chk.status}: {chk.message_es[:70]}")
asyncio.run(main())
