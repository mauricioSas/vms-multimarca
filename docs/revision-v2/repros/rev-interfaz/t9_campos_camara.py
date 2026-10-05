from common import *
be, srv = start()
a, c1, c2 = setup_scope(srv)
cam = a.get("/api/cameras").json()[0]
print("claves CameraOut:", sorted(cam)); print("live:", cam.get("live"))
print("vendors[0] claves:", sorted(a.get("/api/vendors").json()[0]))
srv.stop()
