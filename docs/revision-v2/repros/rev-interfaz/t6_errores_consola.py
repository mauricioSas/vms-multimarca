"""Todas las páginas con los 6 bloques: errores JS, consola y respuestas >=400 (salvo 401) por perfil."""
import time, sys
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
a.put("/api/walls/1", json={"grid": 4, "cells": [c1, c2]}).raise_for_status()
# usuario operador SIN ámbito
a.post("/api/users", json={"username": "op2", "password": "operador2-pass-1234", "role": "operator"})
pw, b = browser()
PAGES = ["/", "/status", "/playback", "/analytics", "/wall/1"]
WHO = {"admin": ADMIN, "operador-con-ambito": OPER, "operador-sin-ambito": ("op2", "operador2-pass-1234")}
try:
    for who, cred in list(WHO.items()) + [("kiosco", None)]:
        ctx = b.new_context(base_url=srv.base_url, locale="es-ES")
        p = ctx.new_page()
        if cred:
            p.goto("/login?next=/analytics"); p.fill("#username", cred[0]); p.fill("#password", cred[1])
            p.click("#login-submit"); p.wait_for_url(lambda u: "/login" not in u)
        else:
            p.goto("/api/local/kiosk")
            print(who, "intercambio:", p.evaluate("""async () => (await fetch('/api/local/kiosk-session',{method:'POST',
              headers:{'Content-Type':'application/json','X-Requested-With':'vms'},body:JSON.stringify({token:'%s',next:'/wall/1'})})).status""" % be.opt.kiosk_token))
        for path in (PAGES if cred else ["/wall/1"]):
            errs = []
            p.on("pageerror", lambda e, errs=errs: errs.append(f"pageerror: {e}"))
            p.on("console", lambda m, errs=errs: errs.append(f"console.{m.type}: {m.text[:160]}") if m.type in ("error", "warning") else None)
            p.on("response", lambda r, errs=errs: errs.append(f"http {r.status} {r.request.method} {r.url.split(srv.base_url)[-1]}")
                 if r.status >= 400 and r.status != 401 and "/whep" not in r.url else None)
            p.goto(path); p.wait_for_load_state("load"); time.sleep(3)
            uniq = sorted(set(errs))
            print(f"[{who}] {path}: {len(uniq)} problemas" + "".join(f"\n     - {e}" for e in uniq))
            p.remove_listener("pageerror", p.listeners("pageerror")[-1]) if hasattr(p, "listeners") else None
            p.close(); p = ctx.new_page()
        ctx.close()
finally:
    b.close(); pw.stop(); srv.stop()
