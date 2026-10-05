"""Aviso (notice) de B6 sobre una cámara ajena: ¿lo ve en el panel un operador con ámbito?"""
import asyncio, time
from common import *
from vms.ops.notify import Alert

be, srv = start()
a, c1, c2 = setup_scope(srv)
a.patch(f"/api/cameras/{c2}", json={"name": "Despacho de dirección"})
pw, b = browser()
ctx = b.new_context(base_url=srv.base_url)
try:
    p = ctx.new_page(); p.goto("/login?next=/")
    p.fill("#username", OPER[0]); p.fill("#password", OPER[1]); p.click("#login-submit")
    p.wait_for_url(lambda u: "/login" not in u); p.wait_for_load_state("networkidle"); time.sleep(1.5)
    print("Cámaras que el operador ve en el panel:", [c["name"] for c in p.evaluate(
        "async () => (await fetch('/api/cameras',{headers:{'X-Requested-With':'vms'}})).json()")])
    ops = be.app.state.ops
    loop = srv.server.servers[0].get_loop()
    alert = Alert(kind="tamper", severity="critical", title_es="Cámara tapada: Despacho de dirección", camera_id=c2,
                  camera_name="Despacho de dirección")
    asyncio.run_coroutine_threadsafe(ops.notifier.emit(alert), loop).result(5)
    time.sleep(1.5)
    print("Avisos (toast) en el panel del operador:", p.evaluate(
        "[...document.querySelectorAll('.toast, [role=status], [role=alert]')].map(e => e.textContent.trim()).filter(Boolean)"))
finally:
    ctx.close(); b.close(); pw.stop(); srv.stop()
