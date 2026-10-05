"""Cuántas conexiones SSE abre cada página y qué pasa con el límite de 6 conexiones HTTP/1.1 por origen."""
import time
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
a.put("/api/walls/1", json={"grid": 4, "cells": [c1, c2]}).raise_for_status()
bus = be.app.state.vms.bus
pw, b = browser()
def login(ctx):
    p = ctx.new_page(); p.goto("/login?next=/status")
    p.fill("#username", ADMIN[0]); p.fill("#password", ADMIN[1]); p.click("#login-submit")
    p.wait_for_url(lambda u: "/login" not in u); return p
try:
    for path in ["/", "/status", "/playback", "/analytics", "/wall/1"]:
        ctx = b.new_context(base_url=srv.base_url); p = login(ctx)
        base = bus.subscribers
        p.goto(path); p.wait_for_load_state("load"); time.sleep(2.5)
        print(f"{path:12s} conexiones /api/events abiertas por la página: {bus.subscribers - base + (1 if base else 0)}"
              f" (total en el bus={bus.subscribers})")
        ctx.close(); time.sleep(1.0)
    # un operador con varias pestañas en el mismo navegador (mismo perfil = mismo cupo de 6 conexiones)
    ctx = b.new_context(base_url=srv.base_url); p0 = login(ctx)
    p0.goto("/"); p0.wait_for_load_state("load")
    q = ctx.new_page(); q.goto("/status"); q.wait_for_load_state("load"); time.sleep(2)
    print("Pestañas: panel (/) + estado (/status): SSE en el bus =", bus.subscribers)
    w = ctx.new_page(); w.goto("/wall/1"); w.wait_for_load_state("load"); time.sleep(2)
    print("+ muro 1: SSE en el bus =", bus.subscribers)
    FETCH = """async () => { const c = new AbortController(); setTimeout(() => c.abort(), 8000);
        const t = performance.now(); try { const r = await fetch('/api/cameras', {signal: c.signal, headers:{'X-Requested-With':'vms'}}); return 'HTTP ' + r.status + ' en ' + Math.round(performance.now()-t) + ' ms'; }
        catch (e) { return 'fetch /api/cameras abortado tras ' + Math.round(performance.now()-t) + ' ms: ' + e.name; } }"""
    print("  (control) desde el muro con 5 SSE, GET /api/cameras:", w.evaluate(FETCH))
    pb = ctx.new_page(); t0 = time.time()
    try:
        pb.goto("/playback", timeout=20000); pb.wait_for_load_state("load", timeout=20000)
        time.sleep(3)
        print(f"+ grabaciones: navegó en {time.time()-t0:.1f} s; SSE en el bus = {bus.subscribers}; "
              f"cámaras en su selector: {pb.evaluate('document.querySelectorAll(\"select option\").length')}")
    except Exception as e:
        print(f"+ grabaciones: NO cargó en 20 s ({type(e).__name__}); SSE en el bus = {bus.subscribers}")
    r = w.evaluate("""async () => { const c = new AbortController(); setTimeout(() => c.abort(), 8000);
        const t = performance.now(); try { const r = await fetch('/api/cameras', {signal: c.signal, headers:{'X-Requested-With':'vms'}}); return 'HTTP ' + r.status + ' en ' + Math.round(performance.now()-t) + ' ms'; }
        catch (e) { return 'fetch /api/cameras abortado tras ' + Math.round(performance.now()-t) + ' ms: ' + e.name; } }""")
    print("Desde el muro, GET /api/cameras:", r)
    print("Celdas del muro:", [(c["cameraId"], c["state"], c["reader"] and c["reader"]["attempts"]) for c in w.evaluate("window.__vmsWall.cells()")][:2])
    ctx.close()
finally:
    b.close(); pw.stop(); srv.stop()
