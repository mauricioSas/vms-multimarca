"""Hik: un recurso ISAPI no soportado (403 notSupport, statusCode 4) tumba TODO security_settings."""
import asyncio, sys
sys.path.insert(0, ".")
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock, _status, XML
from starlette.responses import Response
from vms.vendors import client_for

mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
orig = mock.network_flag
async def network_flag(request):
    if request.path_params["what"] == "telnetd":   # firmware sin Telnet
        return Response(_status(4, "notSupport", "Invalid Operation", request.url.path), 403, media_type=XML)
    return await orig(request)
mock.network_flag = network_flag
# reconstruir rutas con el handler parcheado
from starlette.routing import Route
for r in mock.app.inner.routes:
    if isinstance(r, Route) and r.path == "/ISAPI/System/Network/{what}":
        r.endpoint = network_flag; r.app = __import__("starlette.routing", fromlist=["request_response"]).request_response(network_flag)

async def main():
    c = client_for(make_device("hikvision", username="visor"), "x", transport=asgi(mock.app))
    try:
        s = await c.security_settings("admin", TEST_PASSWORD)
        print("OK:", s)
    except Exception as e:
        print("EXCEPCION:", type(e).__name__, "-", e)
    finally:
        await c.aclose()
    print("peticiones:", [r for r in mock.requests if "Network" in r or "admin" in r])
asyncio.run(main())
