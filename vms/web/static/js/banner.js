// Aviso de versión nueva en las páginas del panel (PLAN-V2 §2.5 «Visor»). Dueño: B2.
//
// Compara cada minuto la versión de /api/health con la que había al abrir la página. Si cambió (el servicio se
// actualizó o volvió atrás), muestra un aviso y recarga la página cuando lleva 60 s sin actividad, para no
// cortar a nadie a mitad de una tarea. Los muros no lo usan: wall.js se recarga solo, sin avisos.
//
// No abre otra conexión de eventos (el navegador solo admite 6 por servidor con HTTP/1.1): pregunta con un GET
// ligero y sin sesión.
//
// Uso: <script type="module" src="/static/js/banner.js"></script> en las páginas del panel.

const CHECK_MS = 60000;
const IDLE_MS = 60000;

let initial = null;
let shown = null;
let lastInput = Date.now();
let idleTimer = null;

async function currentVersion() {
  try {
    const r = await fetch("/api/health", { cache: "no-store", headers: { Accept: "application/json" } });
    if (!r.ok) return null;
    const h = await r.json();
    return typeof h.version === "string" ? h.version : null;
  } catch {
    return null;   // servicio reiniciándose: se vuelve a preguntar en el siguiente minuto
  }
}

function show(version) {
  if (shown === version) return;
  shown = version;
  let box = document.getElementById("vms-update-banner");
  if (!box) {
    box = document.createElement("div");
    box.id = "vms-update-banner";
    box.setAttribute("role", "status");
    box.className = "banner info update-banner";
    const text = document.createElement("span");
    text.className = "update-banner-text";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.textContent = "Recargar ahora";
    btn.addEventListener("click", () => location.reload());
    box.append(text, " ", btn);
    (document.querySelector("main") || document.body).prepend(box);
  }
  box.querySelector(".update-banner-text").textContent =
    `Hay una versión nueva del servicio (${version}). La página se recargará sola cuando no la estés usando.`;
  scheduleIdleReload();
}

function scheduleIdleReload() {
  clearTimeout(idleTimer);
  const wait = Math.max(1000, IDLE_MS - (Date.now() - lastInput));
  idleTimer = setTimeout(() => {
    if (Date.now() - lastInput >= IDLE_MS) location.reload();
    else scheduleIdleReload();
  }, wait);
}

async function check() {
  const v = await currentVersion();
  if (!v) return;
  if (initial === null) initial = v;
  else if (v !== initial) show(v);
}

if (!location.pathname.startsWith("/wall/")) {
  for (const ev of ["mousemove", "mousedown", "keydown", "touchstart", "wheel"]) {
    document.addEventListener(ev, () => { lastInput = Date.now(); }, { passive: true });
  }
  check();
  setInterval(check, CHECK_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
}

// Para pruebas y diagnóstico (sin datos sensibles).
window.__vmsUpdateBanner = { get initial() { return initial; }, get shown() { return shown; }, check };
