import time
from common import *
be, srv = start()
pw, b = browser(); ctx = b.new_context(base_url=srv.base_url)
try:
    w = ctx.new_page(); w.goto("/api/local/kiosk")
    print("intercambio:", w.evaluate("""async () => (await fetch('/api/local/kiosk-session',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'vms'},body:JSON.stringify({token:'%s',next:'/wall/1'})})).status""" % be.opt.kiosk_token))
    p = ctx.new_page()
    for path in ["/", "/login?next=/", "/status"]:
        p.goto(path); time.sleep(4)
        print(f"panel abre {path:16s} -> acaba en {p.url.replace(srv.base_url, '')}; título={p.title()!r} url_js={p.evaluate("location.pathname")}")
finally:
    ctx.close(); b.close(); pw.stop(); srv.stop()
