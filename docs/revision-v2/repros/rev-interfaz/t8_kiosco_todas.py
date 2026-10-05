"""Kiosco (cookie del visor): ¿ve cámaras que no están en ningún muro? CONTRATO §18.8: «el kiosco ve el vivo de las
cámaras de los muros»."""
import httpx
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
for m in (1, 2, 3, 4):
    a.put(f"/api/walls/{m}", json={"grid": 1, "cells": [c1] if m == 1 else []}).raise_for_status()
k = httpx.Client(base_url=srv.base_url, headers={"X-Requested-With": "vms"})
print("intercambio de kiosco:", k.post("/api/local/kiosk-session", json={"token": be.opt.kiosk_token, "next": "/wall/1"}).status_code)
print("muros: ", [a.get(f"/api/walls/{m}").json()["cells"] for m in (1, 2, 3, 4)])
print("kiosco GET /api/cameras ->", [(c["id"], c["name"]) for c in k.get("/api/cameras").json()], "(c2 =", c2, ")")
print("kiosco GET /api/live/c2 ->", k.get(f"/api/live/{c2}").status_code)
r = k.post(f"/api/live/{c2}/main/whep", content=b"v=0\r\n", headers={"Content-Type": "application/sdp"})
print("kiosco POST /api/live/c2/main/whep ->", r.status_code, r.text[:90])
print("operador con ámbito, misma cámara ->", client(srv, OPER).get(f"/api/live/{c2}").status_code)
srv.stop()
