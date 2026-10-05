"""security_settings: la contraseña TEMPORAL de administrador hereda allow_basic del equipo -> viaja en Basic por HTTP."""
import asyncio, sys, base64
sys.path.insert(0, ".")
import httpx
from tests.vendors.conftest import asgi, make_device
from tools.mocks.hikvision import HikvisionMock

class Spy(httpx.AsyncBaseTransport):
    def __init__(self, inner): self.inner, self.auth = inner, []
    async def handle_async_request(self, request):
        a = request.headers.get("authorization")
        if a: self.auth.append(a.split()[0] + " " + (base64.b64decode(a.split()[1]).decode() if a.startswith("Basic") else "…"))
        return await self.inner.handle_async_request(request)

async def main():
    hik = HikvisionMock(password="AdminTemporal#1", kind="camera", auth_mode="basic")
    spy = Spy(asgi(hik.app))
    from vms.vendors import client_for
    dev = make_device("hikvision", username="visor", allow_basic=True, https=False)
    c = client_for(dev, "visor-pass", transport=spy)
    try:
        s = await c.security_settings("admin", "AdminTemporal#1")
        print("ajustes leídos:", s.telnet_enabled, s.upnp_enabled)
    finally:
        await c.aclose()
    print("cabeceras Authorization (decodificadas):", spy.auth[:2])
asyncio.run(main())
