// #timeline-events-root (playback.html): capas de eventos sobre la línea de tiempo de reproducción: huecos de
// grabación, marcadores, tramos protegidos, salud de imagen, hora y alertas de colas. «Sin grabación» se pinta
// distinto de «hay grabación» y cada marca lleva texto (no solo color). Dueño: B6. CONTRATO §18.13.
//
// Se apoya en lo que publica playback.js (`window.__vmsPlayback`: vista, posición y `play`) y en sus elementos
// (#pb-camera, #pb-date, #timeline-track, #timeline-range): no modifica su código.
import { get, enc, isId } from "./api.js";
import { h, clear, me, section, onOpsEvent } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("timeline-events-root");
const LAYERS = {
  recording_gap: "Sin grabación", bookmark: "Marcadores", protected: "Protegidos", health: "Imagen",
  clock: "Hora", analytics_alert: "Colas",
};
const enabled = new Set(Object.keys(LAYERS));
let layer = null;
let legend = null;
let events = [];
let seq = 0;

function view() {
  const pb = window.__vmsPlayback;
  if (!pb) return null;
  const v = pb.view;
  const from = Date.parse(v.from);
  const to = Date.parse(v.to);
  return Number.isFinite(from) && Number.isFinite(to) && to > from ? { from, to } : null;
}

function cameraId() {
  const v = document.getElementById("pb-camera")?.value || "";
  return isId(v) ? v : "";
}

async function load() {
  const cam = cameraId();
  const v = view();
  const my = ++seq;
  if (!cam || !v) { events = []; render(); return; }
  const day = new Date(v.from);
  const start = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  const end = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1);
  try {
    const res = await get(`/api/timeline/${enc(cam)}?start=${enc(start.toISOString())}&end=${enc(end.toISOString())}`);
    if (my !== seq) return;
    events = res;
  } catch (err) {
    if (my !== seq) return;
    events = [];
    console.warn("Eventos de la línea de tiempo no disponibles", err);
  }
  render();
}

function fmt(ms) {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function render() {
  if (!layer) return;
  clear(layer);
  const v = view();
  if (!v) return;
  const len = v.to - v.from;
  for (const ev of events) {
    if (!enabled.has(ev.layer)) continue;
    const s = Date.parse(ev.start);
    const e = ev.end ? Date.parse(ev.end) : s + Math.max(len / 400, 60000);
    if (e <= v.from || s >= v.to) continue;
    const a = Math.max(0, (s - v.from) / len);
    const b = Math.min(1, (e - v.from) / len);
    const title = `${LAYERS[ev.layer] || ev.layer}: ${ev.title_es} (${fmt(s)}${ev.end ? `–${fmt(e)}` : ""})`;
    const mark = h("button", { type: "button", class: `tl-ev tl-${ev.layer} sev-${ev.severity}`, title, "aria-label": title,
      onclick: (e2) => { e2.stopPropagation(); window.__vmsPlayback?.play(new Date(Math.max(s, v.from)).toISOString()); } });
    mark.style.left = `${(a * 100).toFixed(4)}%`;
    mark.style.width = `${Math.max(0.3, (b - a) * 100).toFixed(4)}%`;
    layer.append(mark);
  }
  if (legend) {
    for (const [k, input] of legend) {
      const n = events.filter((ev) => ev.layer === k).length;
      input.parentElement.querySelector(".tl-count").textContent = n ? ` (${n})` : "";
    }
  }
}

async function main() {
  if (!root) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk) return;
  const track = document.getElementById("timeline-track");
  if (track) {
    layer = h("div", { class: "tl-events", "aria-label": "Eventos en la línea de tiempo" });
    track.append(layer);
  }
  const body = section(root, { id: "h-ops-events", title: "Eventos en la línea de tiempo",
    subtitle: "Pulsa una marca de la línea de tiempo para ir a ese momento" });
  legend = new Map();
  body.append(h("div", { class: "tl-legend", role: "group", "aria-label": "Capas" }, Object.entries(LAYERS).map(([k, label]) => {
    const input = h("input", { type: "checkbox", checked: true, onchange: () => {
      if (input.checked) enabled.add(k); else enabled.delete(k);
      render();
    } });
    legend.set(k, input);
    return h("label", { class: "check tl-legend-item" }, input, h("span", { class: `tl-swatch tl-${k}`, "aria-hidden": "true" }),
      label, h("span", { class: "tl-count muted small" }));
  })));
  document.getElementById("pb-camera")?.addEventListener("change", () => setTimeout(load, 50));
  document.getElementById("pb-date")?.addEventListener("change", () => setTimeout(load, 50));
  document.getElementById("pb-reload")?.addEventListener("click", () => setTimeout(load, 50));
  const range = document.getElementById("timeline-range");
  let lastDay = "";
  if (range) {
    new MutationObserver(() => {
      const v = view();
      const day = v ? new Date(v.from).toDateString() : "";
      if (day !== lastDay) { lastDay = day; load(); } else render();
    }).observe(range, { childList: true, characterData: true, subtree: true });
  }
  onOpsEvent("bookmark", (ev) => { if (ev.camera_id === cameraId()) load(); });
  onOpsEvent("health", (ev) => { if (ev.camera_id === cameraId()) load(); });
  window.__vmsTimelineEvents = { reload: load, get events() { return events; } };
  setTimeout(load, 300);
}

main();
