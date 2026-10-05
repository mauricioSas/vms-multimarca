// SharedWorker de los muros: UNA sola conexión a /api/events para todos los muros abiertos en el mismo
// navegador (en el visor de escritorio, todas sus ventanas comparten el mismo perfil de WebView2).
//
// Por qué: con HTTP/1.1 el navegador abre como mucho 6 conexiones a la vez con el servidor. Cada SSE ocupa
// una para siempre: con 4 muros y el panel quedaría una sola para todo lo demás, y tras una caída del motor
// las 64 negociaciones WHEP de 4 muros de 16 irían en fila. Con este worker, los muros gastan una.
//
// Mensajes al muro: {type: "open"} | {type: "error"} | {type: "event", name, data}. El muro manda "bye" al irse.

const NAMES = ["config", "status", "engine", "update"];
const STALE_MS = 45000;      // los «status» llegan cada 5 s: 45 s sin nada = conexión muerta
const ports = new Set();
let es = null;
let lastSeen = 0;
let watchdog = null;

function broadcast(msg) {
  for (const p of ports) {
    try {
      p.postMessage(msg);
    } catch {
      ports.delete(p);
    }
  }
}

function open() {
  if (es) es.close();
  es = new EventSource("/api/events");
  lastSeen = Date.now();
  es.onopen = () => {
    lastSeen = Date.now();
    broadcast({ type: "open" });
  };
  es.onerror = () => broadcast({ type: "error" });
  es.onmessage = () => {
    lastSeen = Date.now();
  };
  for (const name of NAMES) {
    es.addEventListener(name, (ev) => {
      lastSeen = Date.now();
      broadcast({ type: "event", name, data: ev.data });
    });
  }
}

function closeIfUnused() {
  if (ports.size) return;
  clearInterval(watchdog);
  watchdog = null;
  if (es) es.close();
  es = null;
}

self.onconnect = (e) => {
  const port = e.ports[0];
  ports.add(port);
  port.onmessage = (m) => {
    if (m.data === "bye") {
      ports.delete(port);
      closeIfUnused();
    }
  };
  port.start();
  if (!es) {
    open();
    watchdog = setInterval(() => {
      if (Date.now() - lastSeen > STALE_MS) open();
    }, 15000);
  } else if (es.readyState === EventSource.OPEN) {
    port.postMessage({ type: "open" });
  }
};
