"""Muro abierto por un operador: «Panel» y luego «Atrás». ¿Vuelve de la caché (bfcache) sin eventos ni vídeo?"""
import time
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
a.put("/api/walls/1", json={"grid": 4, "cells": [c1, c2]}).raise_for_status()
pw, b = browser()
ctx = b.new_context(base_url=srv.base_url)
try:
    p = ctx.new_page(); p.goto("/login?next=/wall/1")
    p.fill("#username", ADMIN[0]); p.fill("#password", ADMIN[1]); p.click("#login-submit")
    p.wait_for_url(lambda u: "/wall/1" in u); time.sleep(2)
    time.sleep(2)
    p.evaluate("window.__marca = 'misma-página'; window.addEventListener('pageshow', e => window.__persisted = e.persisted)")
    p.evaluate("window.__st = 0")
    print("antes: SSE en el bus =", be.app.state.vms.bus.subscribers, "| lectores:", sum(1 for c in p.evaluate("window.__vmsWall.cells()") if c["reader"]))
    p.click("#link-panel", force=True); p.wait_for_url(lambda u: u.endswith("/")); p.wait_for_load_state("load"); time.sleep(1)
    p.go_back(); p.wait_for_url(lambda u: "/wall/1" in u); time.sleep(8)
    restored = p.evaluate("window.__marca === 'misma-página'")
    print("¿restaurada desde bfcache?", restored, "| pageshow.persisted =", p.evaluate("window.__persisted"))
    print("después: SSE en el bus =", be.app.state.vms.bus.subscribers,
          "| lectores activos:", sum(1 for c in p.evaluate("window.__vmsWall.cells()") if c["reader"]),
          "| celdas:", [c["state"] for c in p.evaluate("window.__vmsWall.cells()")][:2])
    # ¿le llegan eventos? cambia el layout del muro desde otra sesión
    a.put("/api/walls/1", json={"grid": 9}).raise_for_status(); time.sleep(3)
    print("tras cambiar el muro a 9 desde el panel: grid visto por el muro =", p.evaluate("window.__vmsWall.layout.grid"))
finally:
    ctx.close(); b.close(); pw.stop(); srv.stop()
