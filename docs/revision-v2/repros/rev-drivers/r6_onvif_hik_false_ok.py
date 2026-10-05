"""Hikvision dada de alta como «ONVIF (otras marcas)»: firmware con CVE KEV -> auditoría «Sin CVE conocidos» (ok)."""
import asyncio, sys
sys.path.insert(0, ".")
from types import SimpleNamespace
from tests.vendors.conftest import asgi, make_device
from tools.mocks.onvif import OnvifMock
from vms.vendors import client_for
from vms.api.routes.devices import _fetch
from vms.core.models import Device
from vms.ops.security.advisories import version_table
from vms.ops.security.audit import AuditDeps, audit_device

PW = "Onvif#Pass:1@"
async def main():
    mock = OnvifMock(password=PW, firmware="V5.5.0 build 200101")      # HIKVISION DS-2CD2143G2-I por ONVIF
    dev = make_device("onvif")
    state = SimpleNamespace(client_factory=lambda d, p: client_for(d, p, transport=asgi(mock.app)))
    changes, _ = await _fetch(state, dev, PW, probe=True)
    print("Guardado por el alta:", changes)
    async def tcp(h, p, t): return False
    class R: status = 401
    async def rtsp(*a, **k): return R()
    async def onv(*a): return False
    deps = AuditDeps(client_factory=lambda d, p: None, get_password=lambda i: "Larga#Clave99",
                     tcp_check=tcp, rtsp_probe=rtsp, onvif_probe=onv, ssdp=None)
    for vendor in ("onvif", "hikvision"):
        saved = Device.model_validate({**dev.model_dump(), **changes, "vendor": vendor})
        f = [x for x in await audit_device(saved, [], deps, version_table(), None, None, None) if x.check == "firmware_cve"]
        print(f"vendor={vendor:9}", [(x.status, x.detail_es[:70]) for x in f])
asyncio.run(main())
