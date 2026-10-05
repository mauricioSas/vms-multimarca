"""Panel y muros en el MISMO perfil (WebView2 compartido, PLAN-V2 §2.3 / CONTRATO §17.2) = mismo almacén de
cookies. Ambas sesiones usan la cookie «vms_session» con path=/ en el mismo origen."""
import time
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
a.put("/api/walls/1", json={"grid": 4, "cells": [c1, c2]}).raise_for_status()
pw, b = browser()
ctx = b.new_context(base_url=srv.base_url)          # un contexto = un perfil (como WebView2 compartido)
EXCHANGE = """async () => { const r = await fetch('/api/local/kiosk-session', {method:'POST', credentials:'same-origin',
  headers:{'Content-Type':'application/json','X-Requested-With':'vms'}, body: JSON.stringify({token:'%s', next:'/wall/1'})});
  return r.status; }""" % be.opt.kiosk_token
me = "async () => (await fetch('/api/auth/me',{headers:{'X-Requested-With':'vms'}})).json()"
try:
    panel = ctx.new_page()
    panel.goto("/login?next=/")
    panel.fill("#username", OPER[0]); panel.fill("#password", OPER[1]); panel.click("#login-submit")
    panel.wait_for_url(lambda u: "/login" not in u); panel.wait_for_load_state("load")
    print("1) panel tras login:", panel.evaluate(me))
    # el visor abre el muro 1: página de intercambio + script (kiosk.rs::exchange_script)
    wall = ctx.new_page()
    wall.goto("/api/local/kiosk")
    print("   intercambio de kiosco:", wall.evaluate(EXCHANGE))
    wall.goto("/wall/1"); wall.wait_for_load_state("load"); time.sleep(1.5)
    print("2) panel tras abrir el muro:", panel.evaluate(me))
    st = panel.evaluate("async () => (await fetch('/api/devices',{headers:{'X-Requested-With':'vms'}})).status")
    print("   panel GET /api/devices ->", st)
    panel.goto("/"); print("   panel recarga / ->", panel.url)
    # el operador vuelve a entrar en el panel
    panel.goto("/login?next=/status"); time.sleep(1.5)
    print("   panel abre /login?next=/status ->", panel.url, "(login.js ve sesión de kiosco y salta)")
    # mismo POST que hace el formulario de login.js
    print("   POST /api/auth/login desde el panel:", panel.evaluate("""async () => (await fetch('/api/auth/login',{method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'vms'},body:JSON.stringify({username:'%s',password:'%s'})})).status""" % OPER))
    print("3) muro tras el login del panel:", wall.evaluate(me))
    print("   /api/cameras visto por el muro:", [c["id"] for c in wall.evaluate(
        "async () => (await fetch('/api/cameras',{headers:{'X-Requested-With':'vms'}})).json()")], "(c1=%s c2=%s)" % (c1, c2))
    wall.reload(); wall.wait_for_function("window.__vmsWall && window.__vmsWall.cells().length === 4", timeout=10000)
    time.sleep(1)
    print("   celdas del muro:", [(x["cameraId"], x["state"]) for x in wall.evaluate("window.__vmsWall.cells()")][:2],
          "| aviso celda 2:", wall.inner_text(".cell[data-index='1'] .notice"))
    # el operador cierra sesión en el panel
    panel.evaluate("async () => fetch('/api/auth/logout',{method:'POST',headers:{'X-Requested-With':'vms'}})")
    wall.reload(); time.sleep(1)
    print("4) muro tras cerrar sesión el panel ->", wall.url)
finally:
    ctx.close(); b.close(); pw.stop(); srv.stop()
