"""Dahua getCurrentTime con el formato del ejemplo oficial de la API HTTP («result=2011-7-3 21:02:32»)."""
import asyncio, sys
sys.path.insert(0, ".")
from datetime import datetime
from starlette.responses import PlainTextResponse
from starlette.routing import Route, request_response
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.dahua import DahuaMock
from vms.vendors import client_for

dah = DahuaMock(password=TEST_PASSWORD, kind="camera")
async def g(request):
    n = datetime.now()
    return PlainTextResponse(f"result={n.year}-{n.month}-{n.day} {n.hour}:{n.minute:02d}:{n.second:02d}\r\n")
for r in dah.app.inner.routes:
    if isinstance(r, Route) and r.path == "/cgi-bin/global.cgi":
        r.endpoint, r.app = g, request_response(g)

async def main():
    c = client_for(make_device("dahua"), TEST_PASSWORD, transport=asgi(dah.app))
    try:
        t = await c.device_time(); print("OK", t.skew_s)
    except Exception as e:
        print(type(e).__name__, "-", e)
    finally:
        await c.aclose()
asyncio.run(main())
