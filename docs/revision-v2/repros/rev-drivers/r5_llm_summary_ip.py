"""Diagnóstico: el `summary` que se pasa al LLM no se sanea (los pasos sí) -> la IP y el nombre del equipo salen."""
import asyncio, sys
sys.path.insert(0, ".")
import httpx
from vms.core.models import Device
from vms.ops.diagnose import Diagnoser, DiagnoseInput
from vms.vendors import client_for

def boom(request):
    raise httpx.ConnectError("refused", request=request)

async def main():
    dev = Device(name="Caja Gran Via 32", vendor="hikvision", kind="camera", host="192.168.50.17", username="visor")
    seen = {}
    async def rewriter(summary, steps):
        seen["summary"], seen["steps"] = summary, [s.detail_es for s in steps]
        return "texto"
    async def tcp(h, p, t): return True
    async def http(h, p, s, t): return 200
    class R: status = 200; video_codec = "H.264"; reachable = True; error = ""
    async def rtsp(*a, **k): return R()
    diag = Diagnoser(lambda d, pw: client_for(d, pw, transport=httpx.MockTransport(boom)),
                     tcp_check=tcp, http_probe=http, rtsp_probe=rtsp, rewriter=rewriter)
    res = await diag.run(DiagnoseInput(device=dev, password="Secreta#99", device_id="dev-00000001"))
    print("summary enviado al LLM:", seen["summary"])
    print("paso auth (saneado) :", [s for s in seen["steps"] if "API" in s])
    print("IP en lo enviado al LLM?", "192.168.50.17" in seen["summary"])
asyncio.run(main())
