"""H1: firmware_date de Hikvision se pierde al guardar el equipo -> auditoria «Desconocido» con firmware vulnerable."""
import asyncio, sys
sys.path.insert(0, ".")
from types import SimpleNamespace
from tests.vendors.conftest import asgi, make_device, TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock
from vms.vendors import client_for
from vms.api.routes.devices import _fetch
from vms.core.models import Device
from vms.ops.security.advisories import version_table
from vms.ops.security.audit import AuditDeps, audit_device

async def main():
    # Camara DS-2CD con firmware 2020 (anterior a 210628: CVE-2021-36260, KEV)
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", firmware="V5.5.0", firmware_released="build 200101")
    dev = make_device("hikvision")
    state = SimpleNamespace(client_factory=lambda d, p: client_for(d, p, transport=asgi(mock.app)))
    changes, _ = await _fetch(state, dev, TEST_PASSWORD, probe=True)   # lo que guarda el alta (routes/devices.py)
    c = client_for(dev, TEST_PASSWORD, transport=asgi(mock.app)); info = await c.probe(); await c.aclose()
    print("DeviceInfo del driver: firmware=%r firmware_date=%r" % (info.firmware, info.firmware_date))
    print("Campos que guarda el alta:", changes)
    saved = Device.model_validate({**dev.model_dump(), **changes})
    print("Device guardado tiene firmware_date?", hasattr(saved, "firmware_date"), getattr(saved, "firmware_date", None))
    async def tcp(h, p, t): return False
    class R: status = 401
    async def rtsp(*a, **k): return R()
    async def onv(*a): return False
    deps = AuditDeps(client_factory=lambda d, p: None, get_password=lambda i: "Larga#Clave99",
                     tcp_check=tcp, rtsp_probe=rtsp, onvif_probe=onv, ssdp=None)
    f = await audit_device(saved, [], deps, version_table(), None, None, None)
    for x in f:
        if x.check == "firmware_cve":
            print("AUDITORIA:", x.status, "|", x.detail_es[:110], x.advisory_ids)
    # Control: con la fecha que SI dio el driver
    from vms.ops.security.advisories import evaluate
    m = evaluate(version_table().table, "hikvision", info.model, info.firmware, info.firmware_date)
    print("CONTROL con info.firmware_date:", [(x.advisory.id, x.verdict) for x in m])
asyncio.run(main())
