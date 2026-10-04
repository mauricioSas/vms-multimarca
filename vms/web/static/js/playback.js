// Reproducción de grabaciones: cámara + día, línea de tiempo con los tramos grabados,
// clic para reproducir desde ese momento, saltos de ±10 s / ±1 min y descarga de clips.
import { ApiError, get, enc, isId } from "./api.js";
import { $, $$, mountShell, toast, toastError, fmtTime, pad2, localDateValue, isoUtc, fmtDuration } from "./ui.js";

const MAX_CHUNK_S = 3600;            // límite del contrato para /video
const TODAY_REFRESH_MS = 30000;

const video = $("#pb-video");
const overlay = $("#pb-overlay");
const track = $("#timeline-track");
const head = $("#timeline-head");
const hover = $("#timeline-hover");

const st = {
  me: null,
  cameras: [],
  cameraId: "",
  day: null,          // Date a las 00:00 locales
  dayEnd: null,       // Date a las 00:00 del día siguiente
  spans: [],          // [{start: ms, end: ms}]
  view: { from: 0, to: 0 },
  hours: 24,
  chunkStart: null,   // ms absolutos del inicio del vídeo cargado
  chunkDuration: 0,   // s
  position: null,     // ms absolutos de la posición actual
  loadSeq: 0,
};

// ------------------------------------------------------------------ utilidades de tiempo
function parseDay(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtClock(ms) {
  return fmtTime(new Date(ms));
}

function setDay(d) {
  st.day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  st.dayEnd = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1);  // respeta cambios de hora
  $("#pb-date").value = localDateValue(st.day);
}

function setView(hours, centerMs) {
  st.hours = hours;
  const dayFrom = st.day.getTime();
  const dayTo = st.dayEnd.getTime();
  const span = Math.min(hours * 3600 * 1000, dayTo - dayFrom);
  let from = (centerMs ?? (dayFrom + dayTo) / 2) - span / 2;
  from = Math.max(dayFrom, Math.min(from, dayTo - span));
  st.view = { from, to: from + span };
  $$("#pb-zoom button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.hours) === hours)));
  renderTimeline();
}

function frac(ms) {
  return (ms - st.view.from) / (st.view.to - st.view.from);
}

// ------------------------------------------------------------------ línea de tiempo
function renderTicks() {
  const box = $("#timeline-ticks");
  const len = st.view.to - st.view.from;
  const stepMin = len > 12 * 3600e3 ? 120 : len > 3 * 3600e3 ? 30 : 5;
  const step = stepMin * 60e3;
  const first = Math.ceil(st.view.from / step) * step;
  const out = [];
  for (let t = first; t <= st.view.to; t += step) {
    const d = new Date(t);
    // alinea a la hora local (los pasos son múltiplos de 5 min, válidos en cualquier zona con desfase entero)
    out.push(`<span style="left:${(frac(t) * 100).toFixed(3)}%">${pad2(d.getHours())}:${pad2(d.getMinutes())}</span>`);
  }
  box.innerHTML = out.join("");
  $("#timeline-range").textContent = `${fmtClock(st.view.from).slice(0, 5)} – ${st.view.to >= st.dayEnd.getTime() ? "24:00" : fmtClock(st.view.to).slice(0, 5)}`;
}

function renderTimeline() {
  renderTicks();
  const html = [];
  for (const s of st.spans) {
    if (s.end <= st.view.from || s.start >= st.view.to) continue;
    const a = Math.max(0, frac(s.start));
    const b = Math.min(1, frac(s.end));
    html.push(`<i style="left:${(a * 100).toFixed(4)}%;width:${((b - a) * 100).toFixed(4)}%"
      title="${fmtClock(s.start)} – ${fmtClock(s.end)}"></i>`);
  }
  $("#timeline-spans").innerHTML = html.join("");
  renderHead();
  const total = st.spans.reduce((acc, s) => acc + (s.end - s.start), 0) / 1000;
  $("#timeline-summary").textContent = st.spans.length
    ? `${st.spans.length} ${st.spans.length === 1 ? "tramo" : "tramos"} · ${fmtDuration(total)} grabados`
    : (st.cameraId ? "Sin grabaciones este día" : "");
}

function renderHead() {
  const tl = $("#timeline");
  if (st.position == null || st.position < st.view.from || st.position > st.view.to) {
    head.hidden = true;
  } else {
    head.hidden = false;
    head.style.left = `${(frac(st.position) * 100).toFixed(4)}%`;
  }
  if (st.position != null) {
    $("#pb-clock").textContent = fmtClock(st.position);
    $("#pb-position").textContent = `${localDateValue(new Date(st.position)).split("-").reverse().join("/")} ${fmtClock(st.position)}`;
    tl.setAttribute("aria-valuenow", String(Math.round((st.position - st.day.getTime()) / 1000)));
    tl.setAttribute("aria-valuetext", fmtClock(st.position));
  }
}

async function loadTimeline() {
  if (!st.cameraId) return;
  const seq = ++st.loadSeq;
  try {
    const q = `start=${enc(isoUtc(st.day))}&end=${enc(isoUtc(st.dayEnd))}`;
    const res = await get(`/api/recordings/${enc(st.cameraId)}/timeline?${q}`);
    if (seq !== st.loadSeq) return;
    st.spans = (res.spans || []).map((s) => ({ start: Date.parse(s.start), end: Date.parse(s.end) }))
      .filter((s) => Number.isFinite(s.start) && Number.isFinite(s.end) && s.end > s.start)
      .sort((a, b) => a.start - b.start);
    renderTimeline();
  } catch (err) {
    if (seq !== st.loadSeq) return;
    st.spans = [];
    renderTimeline();
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "No se pudo leer la línea de tiempo");
  }
}

// ------------------------------------------------------------------ reproducción
function spanAt(ms) {
  return st.spans.find((s) => ms >= s.start && ms < s.end) || null;
}

function nextSpanAfter(ms) {
  return st.spans.find((s) => s.start > ms) || null;
}

function showOverlay(title, detail = "") {
  overlay.hidden = false;
  overlay.innerHTML = "";
  const t = document.createElement("strong");
  t.textContent = title;
  const d = document.createElement("span");
  d.textContent = detail;
  overlay.append(t, d);
}

function play(ms, { quiet = false } = {}) {
  if (!st.cameraId) return;
  let span = spanAt(ms);
  if (!span) {
    const next = nextSpanAfter(ms);
    if (!next) {
      if (!quiet) toast("No hay grabación en ese momento ni después en este día.", "bad");
      return;
    }
    if (!quiet) toast(`Sin grabación a las ${fmtClock(ms)}; se reproduce desde las ${fmtClock(next.start)}.`);
    span = next;
    ms = next.start;
  }
  const duration = Math.max(1, Math.min(MAX_CHUNK_S, (span.end - ms) / 1000));
  st.chunkStart = ms;
  st.chunkDuration = duration;
  st.position = ms;
  const url = `/api/recordings/${enc(st.cameraId)}/video?start=${enc(isoUtc(new Date(ms)))}` +
    `&duration=${duration.toFixed(3)}&format=fmp4`;
  overlay.hidden = true;
  video.src = url;
  video.playbackRate = Number($("#pb-rate").value) || 1;
  const p = video.play();
  if (p && p.catch) p.catch((err) => { if (err.name !== "AbortError") console.warn("play()", err); });
  $("#pb-play").disabled = false;
  if (st.position < st.view.from || st.position > st.view.to) setView(st.hours, st.position);
  renderHead();
  updateClipFromPosition(false);
  history.replaceState(null, "", `/playback?camera=${enc(st.cameraId)}&date=${localDateValue(st.day)}&t=${enc(isoUtc(new Date(ms)))}`);
}

function seekBy(seconds) {
  if (st.position == null) return;
  const target = st.position + seconds * 1000;
  // dentro del vídeo ya cargado y con datos: salto local, sin pedir otro clip
  if (st.chunkStart != null) {
    const rel = (target - st.chunkStart) / 1000;
    for (let i = 0; i < video.seekable.length; i++) {
      if (rel >= video.seekable.start(i) && rel <= video.seekable.end(i)) {
        video.currentTime = rel;
        return;
      }
    }
  }
  play(target);
}

video.addEventListener("timeupdate", () => {
  if (st.chunkStart == null) return;
  st.position = st.chunkStart + video.currentTime * 1000;
  renderHead();
});
video.addEventListener("play", () => { $("#pb-play").textContent = "Pausa"; });
video.addEventListener("pause", () => { $("#pb-play").textContent = "Reproducir"; });
video.addEventListener("ended", () => {
  // continúa con el siguiente tramo (o el resto del mismo si se cortó en el límite de 1 h)
  const next = st.chunkStart + st.chunkDuration * 1000;
  if (spanAt(next + 500) || nextSpanAfter(next)) play(spanAt(next + 500) ? next : nextSpanAfter(next).start, { quiet: true });
});
video.addEventListener("error", () => {
  if (!video.getAttribute("src")) return;
  const code = video.error ? video.error.code : 0;
  console.warn("Error del vídeo grabado", code, video.error && video.error.message);
  showOverlay("No se pudo reproducir este tramo",
    "Puede que la grabación se haya borrado por retención o que el servidor no responda. Prueba otro momento.");
});
video.addEventListener("waiting", () => { $("#pb-clock").classList.add("muted"); });
video.addEventListener("playing", () => { $("#pb-clock").classList.remove("muted"); });

$("#pb-play").addEventListener("click", () => {
  if (!video.getAttribute("src")) {
    if (st.spans.length) play(st.position ?? st.spans[0].start);
    return;
  }
  if (video.paused) video.play().catch((err) => console.warn(err));
  else video.pause();
});
$$("[data-seek]").forEach((b) => b.addEventListener("click", () => seekBy(Number(b.dataset.seek))));
$("#pb-rate").addEventListener("change", (e) => { video.playbackRate = Number(e.target.value) || 1; });

// ------------------------------------------------------------------ interacción con la línea de tiempo
function msFromEvent(ev) {
  const r = track.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
  return st.view.from + f * (st.view.to - st.view.from);
}

track.addEventListener("click", (ev) => play(msFromEvent(ev)));
track.addEventListener("mousemove", (ev) => {
  const ms = msFromEvent(ev);
  hover.hidden = false;
  hover.style.left = `${(frac(ms) * 100).toFixed(3)}%`;
  hover.textContent = fmtClock(ms) + (spanAt(ms) ? "" : " · sin grabación");
});
track.addEventListener("mouseleave", () => { hover.hidden = true; });
track.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const levels = [24, 6, 1];
  const i = levels.indexOf(st.hours);
  const ni = ev.deltaY < 0 ? Math.min(levels.length - 1, i + 1) : Math.max(0, i - 1);
  if (ni !== i) setView(levels[ni], msFromEvent(ev));
}, { passive: false });
$("#timeline").addEventListener("keydown", (ev) => {
  const step = ev.shiftKey ? 600 : 60;
  if (ev.key === "ArrowRight") { seekBy(step); ev.preventDefault(); }
  else if (ev.key === "ArrowLeft") { seekBy(-step); ev.preventDefault(); }
  else if (ev.key === " ") { $("#pb-play").click(); ev.preventDefault(); }
});
$("#pb-zoom").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-hours]");
  if (b) setView(Number(b.dataset.hours), zoomCenter());
});
/** Centro al cambiar de escala: la posición actual, si no lo último grabado, si no el centro de la vista. */
function zoomCenter() {
  if (st.position != null) return st.position;
  const last = st.spans.at(-1);
  if (last) return Math.max(last.start, last.end - 60000);
  return (st.view.from + st.view.to) / 2;
}

$("#tl-prev").addEventListener("click", () => setView(st.hours, (st.view.from + st.view.to) / 2 - (st.view.to - st.view.from)));
$("#tl-next").addEventListener("click", () => setView(st.hours, (st.view.from + st.view.to) / 2 + (st.view.to - st.view.from)));

// ------------------------------------------------------------------ selección
function resetPlayer() {
  video.pause();
  video.removeAttribute("src");
  video.load();
  st.chunkStart = null;
  st.position = null;
  $("#pb-play").disabled = true;
  $("#pb-clock").textContent = "--:--:--";
  $("#pb-position").textContent = "";
  showOverlay("Elige un momento en la línea de tiempo", "Los tramos de color son los periodos grabados.");
}

$("#pb-camera").addEventListener("change", async (e) => {
  st.cameraId = e.target.value;
  resetPlayer();
  updateClipLink();
  await loadTimeline();
});
$("#pb-date").addEventListener("change", async (e) => {
  const d = parseDay(e.target.value);
  if (!d) return;
  setDay(d);
  resetPlayer();
  setView(st.hours);
  updateClipLink();
  await loadTimeline();
});
$("#pb-reload").addEventListener("click", loadTimeline);

// ------------------------------------------------------------------ descarga de clip
function clipStartMs() {
  const v = $("#clip-start").value;
  const m = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(v);
  if (!m || !st.day) return null;
  return new Date(st.day.getFullYear(), st.day.getMonth(), st.day.getDate(), Number(m[1]), Number(m[2]), Number(m[3] || 0)).getTime();
}

function updateClipLink() {
  const a = $("#clip-download");
  const start = clipStartMs();
  const dur = Number($("#clip-duration").value) || 60;
  if (!st.cameraId || start == null || !isId(st.cameraId)) {
    a.setAttribute("aria-disabled", "true");
    a.href = "#";
    return;
  }
  a.removeAttribute("aria-disabled");
  a.href = `/api/recordings/${enc(st.cameraId)}/video?start=${enc(isoUtc(new Date(start)))}&duration=${dur}&format=mp4&download=1`;
}

function updateClipFromPosition(force = true) {
  if (st.position == null) return;
  if (!force && $("#clip-start").value) return;
  $("#clip-start").value = fmtClock(st.position);
  updateClipLink();
}

$("#clip-start").addEventListener("input", updateClipLink);
$("#clip-duration").addEventListener("change", updateClipLink);
$("#clip-from-position").addEventListener("click", () => updateClipFromPosition(true));
$("#clip-download").addEventListener("click", (ev) => {
  const start = clipStartMs();
  if (start == null) { ev.preventDefault(); return; }
  const end = start + (Number($("#clip-duration").value) || 60) * 1000;
  if (!st.spans.some((s) => s.start < end && s.end > start)) {
    ev.preventDefault();
    toast("No hay grabación en ese intervalo.", "bad");
  }
});

// ------------------------------------------------------------------ arranque
async function main() {
  try {
    st.me = await get("/api/auth/me");
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Sin conexión con el servidor");
    return;
  }
  mountShell("playback", st.me);
  const params = new URLSearchParams(location.search);
  setDay(parseDay(params.get("date")) || new Date());
  try {
    st.cameras = await get("/api/cameras");
  } catch (err) {
    toastError(err, "No se pudieron cargar las cámaras");
    return;
  }
  const sel = $("#pb-camera");
  if (!st.cameras.length) {
    sel.innerHTML = '<option value="">No hay cámaras</option>';
    sel.disabled = true;
    showOverlay("No hay cámaras dadas de alta", "Añade un equipo desde el panel de control.");
    setView(24);
    return;
  }
  sel.innerHTML = st.cameras.map((c) => {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.device_name ? `${c.name} · ${c.device_name}` : c.name;
    return o.outerHTML;
  }).join("");
  const wanted = params.get("camera");
  st.cameraId = st.cameras.some((c) => c.id === wanted) ? wanted : st.cameras[0].id;
  sel.value = st.cameraId;
  const t = Date.parse(params.get("t") || "");
  setView(24, Number.isFinite(t) ? t : undefined);
  updateClipLink();
  await loadTimeline();
  if (Number.isFinite(t)) play(t, { quiet: true });
  setInterval(() => {
    if (st.cameraId && Date.now() < st.dayEnd.getTime() && Date.now() >= st.day.getTime()) loadTimeline();
  }, TODAY_REFRESH_MS);
}

// Estado para diagnóstico y pruebas automáticas.
window.__vmsPlayback = {
  get spans() { return st.spans.map((s) => ({ start: new Date(s.start).toISOString(), end: new Date(s.end).toISOString() })); },
  get position() { return st.position == null ? null : new Date(st.position).toISOString(); },
  get view() { return { from: new Date(st.view.from).toISOString(), to: new Date(st.view.to).toISOString() }; },
  play: (iso) => play(Date.parse(iso)),
};

main();
