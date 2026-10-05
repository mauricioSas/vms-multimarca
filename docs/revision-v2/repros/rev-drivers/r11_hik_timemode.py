import asyncio, sys
sys.path.insert(0, ".")
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock
from vms.vendors import client_for
from vms.ops.health.clock import device_check
async def main():
    for mode in ("NTP", "satellite", "timecorrect", "SDK", "ONVIF"):
        m = HikvisionMock(password=TEST_PASSWORD, kind="camera", time_mode=mode)
        c = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(m.app))
        try:
            t = await c.device_time()
        finally:
            await c.aclose()
        chk = device_check("dev-00000001", None, t, 2.0, 30.0)
        print(f"timeMode={mode:12} -> time_mode={t.time_mode:8} skew={t.skew_s:+.1f}s estado={chk.status}: {chk.message_es[-70:]}")
asyncio.run(main())
