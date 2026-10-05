"""Operador con ámbito (solo c1): ¿qué le llega por /api/events de la cámara ajena c2?"""
import json, threading, time
from common import *

be, srv = start()
a, c1, c2 = setup_scope(srv)
op = client(srv, OPER)
print("REST: operador GET /api/cameras ->", [c["id"] for c in op.get("/api/cameras").json()],
      "| GET /api/status ->", [c["camera_id"] for c in op.get("/api/status").json()["cameras"]])
print("REST: operador GET /api/bookmarks?camera_id=c2 ->", op.get("/api/bookmarks", params={"camera_id": c2}).status_code)
got = []
def reader():
    with op.stream("GET", "/api/events", timeout=None) as r:
        ev = None
        for line in r.iter_lines():
            if line.startswith("event:"): ev = line[6:].strip()
            elif line.startswith("data:"): got.append((ev, json.loads(line[5:])))
threading.Thread(target=reader, daemon=True).start()
time.sleep(1.0)
r = a.post("/api/bookmarks", json={"camera_id": c2, "start": "2026-10-04T10:00:00Z", "note": "Robo en caja 2"})
print("admin crea marcador en c2:", r.status_code)
# aviso agrupado de B6 (mismo camino que vms/ops/service.py::_notice, que añade camera_ids «para que el SSE filtre»)
ops = be.app.state.vms_ops if hasattr(be.app.state, "vms_ops") else None
time.sleep(6)
for ev, data in got:
    if ev == "status":
        print("SSE status ->", [c["camera_id"] for c in data["cameras"]]); break
for ev, data in got:
    if ev != "status": print("SSE", ev, "->", data)
print("¿c2 aparece en el SSE del operador?", any(c2 in json.dumps(d) for _, d in got))
srv.stop()
