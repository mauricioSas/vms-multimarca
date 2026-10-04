// Muro de vídeo de un monitor: /wall/{1..4}.
//
// - Rejilla 1/4/9/16 según el layout guardado (GET /api/walls/{n}); se actualiza sola, sin recargar,
//   cuando el servidor avisa por /api/events (scope walls/cameras) o cada 10 minutos por seguridad.
// - Vídeo en vivo por WebRTC (WHEP) con el subflujo; la celda ampliada y el layout de 1 usan el principal.
// - Doble clic (o Intro) amplía una celda; doble clic o Escape vuelve a la rejilla.
// - Solo se reconstruyen las celdas que cambian: las demás no cortan su vídeo.

import { ApiError, get, put, subscribeEvents, isId, enc } from "./api.js";
import { WhepReader } from "./whep.js";

const GRIDS = [1, 4, 9, 16];
const STATE_TEXT = {
  connecting: "Conectando",
  live: "En vivo",
  reconnecting: "Reconectando",
  offline: "Sin señal",
  stopped: "Detenido",
};
const SAFETY_REFRESH_MS = 10 * 60 * 1000;
const IDLE_MS = 3000;
// Recarga preventiva diaria (madrugada) si la página lleva más de 20 h abierta: libera
// cualquier recurso que el navegador no haya devuelto en días de funcionamiento continuo.
const DAILY_RELOAD_HOUR = 4;
const DAILY_RELOAD_MIN_UPTIME_MS = 20 * 3600 * 1000;

const monitorMatch = location.pathname.match(/^\/wall\/([1-4])\/?$/);
const monitor = monitorMatch ? Number(monitorMatch[1]) : 1;
const pageStart = Date.now();

const wallEl = document.getElementById("wall");
const hud = document.getElementById("hud");
const fatalEl = document.getElementById("fatal");

let me = null;
let layout = null;                 // WallLayout
let cameras = new Map();           // id → CameraOut
let online = new Map();            // id → bool (eventos status)
let expanded = -1;                 // índice de la celda ampliada o -1
const cells = new Map();           // índice → Cell
let reloadTimer = null;
let loading = false;
let pendingReload = false;

class Cell {
  constructor(index, cameraId) {
    this.index = index;
    this.cameraId = cameraId;
    this.stream = null;
    this.reader = null;
    this.el = document.createElement("div");
    this.el.className = "cell";
    this.el.tabIndex = 0;
    this.el.dataset.index = String(index);
    this.el.dataset.state = "stopped";
    this.el.innerHTML = `
      <video muted autoplay playsinline disablepictureinpicture disableremoteplayback></video>
      <span class="slot-num">${index + 1}</span>
      <div class="notice"><strong></strong><span class="detail"></span></div>
      <div class="badge" role="status" aria-live="off"></div>
      <div class="label"></div>`;
    this.video = this.el.querySelector("video");
    this.noticeTitle = this.el.querySelector(".notice strong");
    this.noticeDetail = this.el.querySelector(".notice .detail");
    this.badge = this.el.querySelector(".badge");
    this.label = this.el.querySelector(".label");
    this.el.addEventListener("dblclick", () => toggleExpand(this.index));
    this.el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") toggleExpand(this.index);
    });
    this.refresh();
  }

  get camera() {
    return this.cameraId ? cameras.get(this.cameraId) : null;
  }

  wantedStream() {
    const cam = this.camera;
    if (!cam) return null;
    if (!cam.has_sub) return "main";
    return expanded === this.index || (layout && layout.grid === 1) ? "main" : "sub";
  }

  /** Arranca, cambia de flujo o para el lector según el estado actual. */
  sync(active) {
    const cam = this.camera;
    const stream = this.wantedStream();
    const shouldPlay = active && cam && cam.enabled !== false && stream && isId(cam.id);
    if (!shouldPlay) {
      this.stopReader();
      this.refresh();
      return;
    }
    if (this.reader && this.stream === stream) return;
    this.stopReader();
    this.stream = stream;
    const url = `/api/live/${enc(cam.id)}/${stream}/whep`;
    this.reader = new WhepReader({
      url,
      video: this.video,
      onState: (state, info) => this.setState(state, info),
    });
    this.reader.start();
  }

  stopReader() {
    if (this.reader) {
      this.reader.stop();
      this.reader = null;
    }
    this.stream = null;
  }

  setState(state, info = {}) {
    this.el.dataset.state = state;
    this.badge.textContent = STATE_TEXT[state] || state;
    if (state === "offline") {
      this.noticeTitle.textContent = "Sin señal";
      this.noticeDetail.textContent = online.get(this.cameraId) === false
        ? "La cámara no envía vídeo. Se reintenta automáticamente."
        : (info.error || "Se reintenta automáticamente.");
    } else if (state === "reconnecting") {
      this.noticeTitle.textContent = "Reconectando…";
      this.noticeDetail.textContent = info.error ? `Motivo: ${info.error}` : "";
    } else if (state === "connecting") {
      this.noticeTitle.textContent = "Conectando…";
      this.noticeDetail.textContent = "";
    }
  }

  refresh() {
    const cam = this.camera;
    this.el.classList.toggle("empty", !cam);
    let warn = this.el.querySelector(".codec-warn");
    if (!cam) {
      this.label.textContent = "";
      this.label.hidden = true;
      this.badge.hidden = true;
      this.el.dataset.state = "stopped";
      this.noticeTitle.textContent = this.cameraId ? "Cámara eliminada" : "Sin cámara";
      this.noticeDetail.textContent = this.cameraId ? "Asigna otra desde el panel." : "";
      this.el.setAttribute("aria-label", `Celda ${this.index + 1}: sin cámara`);
      warn?.remove();
      return;
    }
    this.label.hidden = false;
    this.badge.hidden = false;
    this.label.textContent = cam.name;
    this.el.setAttribute("aria-label", `Celda ${this.index + 1}: ${cam.name}. Doble clic para ampliar.`);
    if (cam.enabled === false) {
      this.el.dataset.state = "offline";
      this.badge.textContent = "Desactivada";
      this.noticeTitle.textContent = "Cámara desactivada";
      this.noticeDetail.textContent = "Actívala en el panel para ver el vídeo.";
    } else if (!this.reader) {
      this.setState("connecting");
    }
    const msg = cam.live && cam.live.codec_warning;
    if (msg) {
      if (!warn) {
        warn = document.createElement("div");
        warn.className = "codec-warn";
        this.el.append(warn);
      }
      warn.textContent = msg;
    } else {
      warn?.remove();
    }
  }

  destroy() {
    this.stopReader();
    this.el.remove();
  }
}

// ------------------------------------------------------------------ rejilla
function render() {
  if (!layout) return;
  const grid = GRIDS.includes(layout.grid) ? layout.grid : 4;
  const cols = Math.round(Math.sqrt(grid));
  wallEl.style.setProperty("--cols", String(cols));
  if (expanded >= grid) expanded = -1;
  const wanted = new Set();
  for (let i = 0; i < grid; i++) {
    const camId = layout.cells[i] || null;
    wanted.add(i);
    let cell = cells.get(i);
    if (cell && cell.cameraId !== camId) {
      cell.destroy();
      cell = null;
    }
    if (!cell) {
      cell = new Cell(i, camId);
      cells.set(i, cell);
    }
  }
  for (const [i, cell] of cells) {
    if (!wanted.has(i)) {
      cell.destroy();
      cells.delete(i);
    }
  }
  // orden visual = índice
  for (let i = 0; i < grid; i++) wallEl.append(cells.get(i).el);
  wallEl.classList.toggle("expanded", expanded >= 0);
  for (const [i, cell] of cells) {
    cell.el.classList.toggle("is-expanded", i === expanded);
    cell.refresh();
    // con una celda ampliada, las demás paran su vídeo (menos carga de decodificación)
    cell.sync(expanded < 0 || i === expanded);
  }
  renderLayoutButtons(grid);
  document.title = `${layout.name || `Monitor ${monitor}`} · Muro`;
  document.getElementById("hud-title").textContent = layout.name || `Monitor ${monitor}`;
}

function toggleExpand(index) {
  if (expanded >= 0) {
    expanded = -1;
  } else {
    const cell = cells.get(index);
    if (!cell || !cell.camera) return;   // una celda vacía no se amplía
    expanded = index;
  }
  render();
  const target = cells.get(expanded >= 0 ? expanded : index);
  target?.el.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ datos
async function load() {
  if (loading) {
    pendingReload = true;
    return;
  }
  loading = true;
  try {
    const [wall, cams] = await Promise.all([get(`/api/walls/${monitor}`), get("/api/cameras")]);
    cameras = new Map(cams.map((c) => [c.id, c]));
    for (const c of cams) {
      if (c.live && typeof c.live.online === "boolean") online.set(c.id, c.live.online);
    }
    layout = wall;
    hideFatal();
    render();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return; // ya redirige al login
    console.error("No se pudo cargar el layout del muro", err);
    if (!layout) showFatal("No se pudo cargar la configuración del muro.", err.message || String(err));
    scheduleReload(5000);
  } finally {
    loading = false;
    if (pendingReload) {
      pendingReload = false;
      scheduleReload(50);
    }
  }
}

function scheduleReload(ms = 300) {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(load, ms);
}

function onStatus(data) {
  if (!data || !Array.isArray(data.cameras)) return;
  for (const c of data.cameras) {
    const was = online.get(c.camera_id);
    online.set(c.camera_id, !!c.online);
    if (c.online && was === false) {
      // la cámara volvió: las celdas que la esperan reintentan ya, sin esperar el backoff
      for (const cell of cells.values()) {
        if (cell.cameraId === c.camera_id && cell.reader) cell.reader.kick();
      }
    }
  }
}

function showFatal(title, detail) {
  fatalEl.hidden = false;
  fatalEl.innerHTML = "";
  const t = document.createElement("div");
  t.textContent = title;
  const d = document.createElement("small");
  d.textContent = detail ? `${detail}. Se reintenta automáticamente.` : "Se reintenta automáticamente.";
  t.append(d);
  fatalEl.append(t);
}

function hideFatal() {
  fatalEl.hidden = true;
}

// ------------------------------------------------------------------ HUD y controles
function renderLayoutButtons(current) {
  const box = document.getElementById("layout-buttons");
  const canEdit = me && !me.kiosk && (me.role === "operator" || me.role === "admin");
  box.hidden = !canEdit;
  if (!canEdit) return;
  if (!box.childElementCount) {
    for (const g of GRIDS) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.grid = String(g);
      b.textContent = String(g);
      b.title = `Distribución de ${g} ${g === 1 ? "cámara" : "cámaras"}`;
      b.addEventListener("click", () => changeGrid(g));
      box.append(b);
    }
  }
  for (const b of box.children) b.setAttribute("aria-pressed", String(Number(b.dataset.grid) === current));
}

async function changeGrid(grid) {
  if (!layout || layout.grid === grid) return;
  const previous = layout;
  layout = { ...layout, grid };
  expanded = -1;
  render();
  try {
    layout = await put(`/api/walls/${monitor}`, { grid });
    render();
  } catch (err) {
    console.error("No se pudo guardar la distribución", err);
    layout = previous;
    render();
  }
}

let idleTimer = null;
function wake() {
  document.body.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.body.classList.add("idle"), IDLE_MS);
}

function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  document.getElementById("hud-clock").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  if (d.getHours() === DAILY_RELOAD_HOUR && Date.now() - pageStart > DAILY_RELOAD_MIN_UPTIME_MS) {
    location.reload();
  }
}

function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch((err) => console.warn("exitFullscreen", err));
  } else {
    document.documentElement.requestFullscreen().catch((err) => console.warn("requestFullscreen", err));
  }
}

// ------------------------------------------------------------------ arranque
async function main() {
  try {
    me = await get("/api/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    showFatal("No hay conexión con el servidor del VMS.", err.message);
    setTimeout(main, 5000);
    return;
  }
  document.getElementById("link-panel").hidden = !!me.kiosk;
  document.getElementById("btn-fullscreen").addEventListener("click", toggleFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && expanded >= 0) {
      expanded = -1;
      render();
    } else if (e.key === "f" || e.key === "F") {
      toggleFullscreen();
    }
  });
  for (const ev of ["mousemove", "mousedown", "keydown", "touchstart"]) {
    document.addEventListener(ev, wake, { passive: true });
  }
  wake();
  tickClock();
  setInterval(tickClock, 1000);
  await load();
  subscribeEvents({
    config: (data) => {
      if (data && (data.scope === "walls" || data.scope === "cameras" || data.scope === "devices")) scheduleReload(300);
    },
    status: onStatus,
    onOpen: () => scheduleReload(300),   // tras una caída del servidor, vuelve a leer el layout
  });
  setInterval(() => scheduleReload(0), SAFETY_REFRESH_MS);
  window.addEventListener("pagehide", () => {
    for (const cell of cells.values()) cell.stopReader();
  });
}

// Estado para diagnóstico (consola del navegador o pruebas automáticas). Sin datos sensibles.
window.__vmsWall = {
  monitor,
  get layout() { return layout; },
  get expanded() { return expanded; },
  cells() {
    return [...cells.values()].map((c) => ({
      index: c.index, cameraId: c.cameraId, stream: c.stream, state: c.el.dataset.state,
      reader: c.reader ? { attempts: c.reader.stats.attempts, reconnects: c.reader.stats.reconnects } : null,
    }));
  },
};

main();
