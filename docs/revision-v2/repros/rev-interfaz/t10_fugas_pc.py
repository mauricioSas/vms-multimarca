"""Muro de 16 con el motor caído (503 en cada WHEP) durante 60 s + 3 reinicios forzados: ¿quedan RTCPeerConnection vivas?"""
import time
from common import *
from tests.web.conftest import PC_TRACKER
be, srv = start()
a, c1, c2 = setup_scope(srv)
a.put("/api/walls/1", json={"grid": 16, "cells": [c1, c2] * 8}).raise_for_status()
pw, b = browser(); ctx = b.new_context(base_url=srv.base_url); ctx.add_init_script(PC_TRACKER)
try:
    p = ctx.new_page(); p.goto("/api/local/kiosk")
    p.evaluate("""async () => fetch('/api/local/kiosk-session',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'vms'},body:JSON.stringify({token:'%s',next:'/wall/1'})})""" % be.opt.kiosk_token)
    p.goto("/wall/1")
    for i in range(6):
        time.sleep(10)
        t = p.evaluate("({created: window.__pcTracker.created, open: window.__pcTracker.open, sse: 0})")
        print(f"t={10*(i+1)} s: RTCPeerConnection creadas={t['created']} abiertas={t['open']} | SSE en el bus={be.app.state.vms.bus.subscribers}")
finally:
    ctx.close(); b.close(); pw.stop(); srv.stop()
