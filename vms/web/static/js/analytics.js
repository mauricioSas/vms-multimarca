// Analítica: dibujar sobre el snapshot de una cámara la línea de puerta (con sentido de
// entrada) y polígonos de zona de cola, con sus umbrales de aviso. Guarda vía /api/analytics.
//
// Ratón: herramienta «línea» = dos clics (inicio y fin). Herramienta «zona» = un clic por
// vértice; se cierra con clic en el primer vértice, doble clic o Intro. Escape cancela,
// Retroceso quita el último vértice. Con una regla seleccionada se arrastran sus vértices o
// la regla entera.

import { ApiError, get, post, put, del, enc } from "./api.js";
import { $, $$, esc, mountShell, toast, toastError, busy, confirmDialog, errorText } from "./ui.js";
import {
  normPoint, entryDirectionPx, distToSegment, pointInPolygon, centroid, isSelfIntersecting, clamp01,
} from "./geometry.js";

const HANDLE_PX = 9;
const CLOSE_PX = 14;
const COLORS = {
  line: "#f2a516",
  lineIdle: "rgba(255, 214, 140, .75)",
  zoneFill: "rgba(90, 174, 255, .22)",
  zoneFillSel: "rgba(90, 174, 255, .32)",
  zoneStroke: "#5aaeff",
  arrow: "#f2a516",
  handle: "#ffffff",
};

const img = $("#an-image");
const canvas = $("#an-canvas");
const ctx = canvas.getContext("2d");
const stage = $("#an-stage");
const form = $("#rule-form");

const st = {
  me: null,
  cameras: [],
  cameraId: "",
  rules: [],
  selectedId: null,     // id de la regla seleccionada o "new"
  draft: null,          // copia editable de la regla seleccionada
  dirty: false,
  tool: null,           // "line" | "zone" | null
  drawing: null,        // puntos en curso durante el dibujo
  hoverPt: null,
  drag: null,           // {kind: "vertex"|"move", index, origin, base}
  imageUrl: null,
  occupancy: {},
};

// ------------------------------------------------------------------ coordenadas
function sizeCanvas() {
  const r = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * dpr));
  const h = Math.max(1, Math.round(r.height * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return { w: r.width, h: r.height, dpr };
}

function toNorm(ev) {
  const r = canvas.getBoundingClientRect();
  return [clamp01((ev.clientX - r.left) / r.width), clamp01((ev.clientY - r.top) / r.height)];
}

function toPx(p, w, h) {
  return [p[0] * w, p[1] * h];
}

function pxDist(a, b) {
  const r = canvas.getBoundingClientRect();
  return Math.hypot((a[0] - b[0]) * r.width, (a[1] - b[1]) * r.height);
}

/** Distancia en píxeles de pantalla de p al segmento ab (coordenadas normalizadas). */
function segDistPx(p, a, b) {
  const r = canvas.getBoundingClientRect();
  const s = (q) => [q[0] * r.width, q[1] * r.height];
  return distToSegment(s(p), s(a), s(b));
}

// ------------------------------------------------------------------ dibujo
function drawArrow(x, y, dx, dy, len) {
  const ex = x + dx * len;
  const ey = y + dy * len;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(ex, ey);
  ctx.stroke();
  const ah = Math.max(8, len * 0.32);
  const px = -dy;
  const py = dx;
  ctx.beginPath();
  ctx.moveTo(ex, ey);
  ctx.lineTo(ex - dx * ah + px * ah * 0.55, ey - dy * ah + py * ah * 0.55);
  ctx.lineTo(ex - dx * ah - px * ah * 0.55, ey - dy * ah - py * ah * 0.55);
  ctx.closePath();
  ctx.fill();
}

function label(text, x, y, color) {
  ctx.font = "600 13px Bahnschrift, 'Segoe UI', system-ui, sans-serif";
  const pad = 5;
  const w = ctx.measureText(text).width + pad * 2;
  ctx.fillStyle = "rgba(0,0,0,.72)";
  ctx.fillRect(x - w / 2, y - 11, w, 22);
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, x, y + 1);
}

function drawHandle(x, y, filled = false) {
  ctx.beginPath();
  ctx.arc(x, y, HANDLE_PX / 1.6, 0, Math.PI * 2);
  ctx.fillStyle = filled ? COLORS.line : "#000";
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = COLORS.handle;
  ctx.stroke();
}

function drawLineRule(rule, selected, w, h) {
  const [x1, y1] = toPx(rule.start, w, h);
  const [x2, y2] = toPx(rule.end, w, h);
  ctx.lineCap = "round";
  ctx.lineWidth = selected ? 4 : 3;
  ctx.strokeStyle = selected ? COLORS.line : COLORS.lineIdle;
  ctx.setLineDash(rule.enabled === false ? [10, 8] : []);
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.setLineDash([]);
  const [dx, dy] = entryDirectionPx(rule.start, rule.end, w, h, !!rule.invert);
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  ctx.strokeStyle = COLORS.arrow;
  ctx.fillStyle = COLORS.arrow;
  ctx.lineWidth = 3;
  drawArrow(mx, my, dx, dy, Math.max(28, Math.min(w, h) * 0.08));
  label("ENTRADA", mx + dx * (Math.max(28, Math.min(w, h) * 0.08) + 22), my + dy * (Math.max(28, Math.min(w, h) * 0.08) + 22), COLORS.arrow);
  label(rule.name || "Línea", mx - dx * 22, my - dy * 22, "#fff");
  if (selected) {
    drawHandle(x1, y1, true);
    drawHandle(x2, y2);
  }
}

function drawZoneRule(rule, selected, w, h) {
  const pts = rule.polygon.map((p) => toPx(p, w, h));
  if (pts.length < 2) return;
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
  ctx.fillStyle = selected ? COLORS.zoneFillSel : COLORS.zoneFill;
  ctx.fill();
  ctx.lineWidth = selected ? 3 : 2;
  ctx.strokeStyle = COLORS.zoneStroke;
  ctx.setLineDash(rule.enabled === false ? [10, 8] : []);
  ctx.stroke();
  ctx.setLineDash([]);
  const [cx, cy] = toPx(centroid(rule.polygon), w, h);
  const occ = st.occupancy[rule.id];
  label(`${rule.name || "Zona"}${occ != null ? ` · ${occ} ahora` : ""} · aviso ≥ ${rule.alert_threshold}`, cx, cy, "#cfe6ff");
  if (selected) pts.forEach(([x, y]) => drawHandle(x, y));
}

function render() {
  const { w, h, dpr } = sizeCanvas();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  for (const r of st.rules) {
    if (r.id === st.selectedId) continue;
    if (r.kind === "line") drawLineRule(r, false, w, h);
    else drawZoneRule(r, false, w, h);
  }
  if (st.draft && !st.drawing) {
    if (st.draft.kind === "line" && st.draft.start) drawLineRule(st.draft, true, w, h);
    else if (st.draft.kind === "zone" && st.draft.polygon && st.draft.polygon.length >= 3) drawZoneRule(st.draft, true, w, h);
  }
  if (st.drawing) {
    const pts = [...st.drawing];
    if (st.hoverPt) pts.push(st.hoverPt);
    ctx.lineWidth = 3;
    ctx.strokeStyle = st.tool === "line" ? COLORS.line : COLORS.zoneStroke;
    ctx.setLineDash([8, 6]);
    ctx.beginPath();
    pts.map((p) => toPx(p, w, h)).forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
    ctx.setLineDash([]);
    st.drawing.forEach((p, i) => {
      const [x, y] = toPx(p, w, h);
      drawHandle(x, y, i === 0);
    });
    if (st.tool === "line" && st.drawing.length === 1 && st.hoverPt && pxDist(st.drawing[0], st.hoverPt) > 10) {
      const tmp = { start: st.drawing[0], end: st.hoverPt };
      const [dx, dy] = entryDirectionPx(tmp.start, tmp.end, w, h, false);
      const [x1, y1] = toPx(tmp.start, w, h);
      const [x2, y2] = toPx(tmp.end, w, h);
      ctx.fillStyle = COLORS.arrow;
      ctx.strokeStyle = COLORS.arrow;
      drawArrow((x1 + x2) / 2, (y1 + y2) / 2, dx, dy, 30);
    }
  }
}

// ------------------------------------------------------------------ herramientas
function setHint(text) {
  $("#an-hint").textContent = text;
}

function setTool(tool) {
  st.tool = tool;
  st.drawing = tool ? [] : null;
  st.hoverPt = null;
  $("#tool-line").setAttribute("aria-pressed", String(tool === "line"));
  $("#tool-zone").setAttribute("aria-pressed", String(tool === "zone"));
  canvas.classList.toggle("drawing", !!tool);
  if (tool === "line") setHint("Haz clic en un extremo de la puerta y después en el otro.");
  else if (tool === "zone") setHint("Haz clic en cada esquina de la zona. Cierra con clic en el primer punto, doble clic o Intro. Retroceso borra el último punto.");
  else setHint(st.draft ? "Arrastra los puntos para ajustar la regla, o arrastra la regla entera." : "Elige una herramienta o selecciona una regla para moverla.");
  render();
}

function startNew(kind) {
  if (!st.cameraId) {
    toast("Elige primero una cámara", "bad");
    return;
  }
  const n = st.rules.filter((r) => r.kind === kind).length + 1;
  st.selectedId = "new";
  st.draft = kind === "line"
    ? { kind, camera_id: st.cameraId, name: n > 1 ? `Puerta ${n}` : "Puerta", enabled: true, invert: false }
    : { kind, camera_id: st.cameraId, name: n > 1 ? `Cola de cajas ${n}` : "Cola de cajas", enabled: true,
      alert_threshold: 5, alert_min_seconds: 60, alert_cooldown_seconds: 600, clear_below: null };
  st.dirty = true;
  fillForm();
  form.hidden = true;  // el formulario aparece al terminar de dibujar
  renderRuleList();
  setTool(kind);
  canvas.focus();
}

function finishDrawing() {
  const pts = st.drawing || [];
  if (st.tool === "line") {
    if (pts.length < 2) return;
    if (pxDist(pts[0], pts[1]) < 12) {
      toast("La línea es demasiado corta", "bad");
      st.drawing = [pts[0]];
      return;
    }
    st.draft.start = normPoint(pts[0]);
    st.draft.end = normPoint(pts[1]);
  } else if (st.tool === "zone") {
    if (pts.length < 3) {
      toast("Una zona necesita al menos 3 puntos", "bad");
      return;
    }
    st.draft.polygon = pts.slice(0, 32).map(normPoint);
    if (isSelfIntersecting(st.draft.polygon)) toast("Ojo: los lados de la zona se cruzan. Revisa los puntos.", "bad", 6000);
  }
  st.dirty = true;
  setTool(null);
  fillForm();
  form.hidden = false;
  $("#rule-name").focus();
}

function cancelDrawing() {
  const wasNew = st.selectedId === "new";
  setTool(null);
  if (wasNew && !(st.draft && (st.draft.start || st.draft.polygon))) selectRule(null);
}

// ------------------------------------------------------------------ eventos del lienzo
function hitTest(p) {
  // vértices de la regla seleccionada
  if (st.draft) {
    const pts = st.draft.kind === "line" ? [st.draft.start, st.draft.end].filter(Boolean) : (st.draft.polygon || []);
    for (let i = 0; i < pts.length; i++) {
      if (pxDist(pts[i], p) <= HANDLE_PX + 3) return { kind: "vertex", index: i };
    }
    if (st.draft.kind === "line" && st.draft.start && st.draft.end && segDistPx(p, st.draft.start, st.draft.end) < 10) {
      return { kind: "move" };
    }
    if (st.draft.kind === "zone" && st.draft.polygon && pointInPolygon(p, st.draft.polygon)) return { kind: "move" };
  }
  // otras reglas: seleccionar
  for (const r of [...st.rules].reverse()) {
    if (r.kind === "line" && segDistPx(p, r.start, r.end) < 10) return { kind: "select", id: r.id };
    if (r.kind === "zone" && pointInPolygon(p, r.polygon)) return { kind: "select", id: r.id };
  }
  return null;
}

canvas.addEventListener("pointerdown", (ev) => {
  if (ev.button !== 0) return;
  const p = toNorm(ev);
  if (st.tool) {
    if (st.tool === "zone" && st.drawing.length >= 3 && pxDist(st.drawing[0], p) <= CLOSE_PX) {
      finishDrawing();
      return;
    }
    st.drawing.push(p);
    if (st.tool === "line" && st.drawing.length === 2) finishDrawing();
    else if (st.tool === "zone" && st.drawing.length >= 32) finishDrawing();
    render();
    return;
  }
  const hit = hitTest(p);
  if (!hit) return;
  if (hit.kind === "select") {
    trySelect(hit.id);
    return;
  }
  canvas.setPointerCapture(ev.pointerId);
  st.drag = { ...hit, origin: p, base: JSON.parse(JSON.stringify(st.draft)) };
  canvas.classList.add("grabbing");
});

canvas.addEventListener("pointermove", (ev) => {
  const p = toNorm(ev);
  if (st.tool) {
    st.hoverPt = p;
    render();
    return;
  }
  if (st.drag) {
    const d = st.drag;
    const dx = p[0] - d.origin[0];
    const dy = p[1] - d.origin[1];
    if (st.draft.kind === "line") {
      if (d.kind === "vertex") st.draft[d.index === 0 ? "start" : "end"] = normPoint(p);
      else {
        const pts = [d.base.start, d.base.end];
        const sx = Math.min(...pts.map((q) => 1 - q[0]), Math.max(dx, -Math.min(...pts.map((q) => q[0]))));
        const sy = Math.min(...pts.map((q) => 1 - q[1]), Math.max(dy, -Math.min(...pts.map((q) => q[1]))));
        st.draft.start = normPoint([d.base.start[0] + sx, d.base.start[1] + sy]);
        st.draft.end = normPoint([d.base.end[0] + sx, d.base.end[1] + sy]);
      }
    } else if (d.kind === "vertex") {
      st.draft.polygon[d.index] = normPoint(p);
    } else {
      const pts = d.base.polygon;
      const sx = Math.min(...pts.map((q) => 1 - q[0]), Math.max(dx, -Math.min(...pts.map((q) => q[0]))));
      const sy = Math.min(...pts.map((q) => 1 - q[1]), Math.max(dy, -Math.min(...pts.map((q) => q[1]))));
      st.draft.polygon = pts.map((q) => normPoint([q[0] + sx, q[1] + sy]));
    }
    st.dirty = true;
    render();
    return;
  }
  const hit = hitTest(p);
  canvas.classList.toggle("grab", !!hit);
});

function endDrag(ev) {
  if (!st.drag) return;
  st.drag = null;
  canvas.classList.remove("grabbing");
  try { canvas.releasePointerCapture(ev.pointerId); } catch { /* ya liberado */ }
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
canvas.addEventListener("pointerleave", () => {
  if (st.tool) { st.hoverPt = null; render(); }
});
canvas.addEventListener("dblclick", () => {
  if (st.tool === "zone" && st.drawing.length >= 3) finishDrawing();
});
canvas.addEventListener("keydown", (ev) => {
  if (!st.tool) return;
  if (ev.key === "Escape") { cancelDrawing(); ev.preventDefault(); }
  else if (ev.key === "Enter" && st.tool === "zone") { finishDrawing(); ev.preventDefault(); }
  else if (ev.key === "Backspace") { st.drawing.pop(); render(); ev.preventDefault(); }
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && st.tool && document.activeElement !== canvas) cancelDrawing();
});

$("#tool-line").addEventListener("click", () => startNew("line"));
$("#tool-zone").addEventListener("click", () => startNew("zone"));

// ------------------------------------------------------------------ lista y editor de reglas
function renderRuleList() {
  const ul = $("#an-rules");
  $("#an-rules-count").textContent = st.rules.length ? `${st.rules.length}` : "";
  if (!st.rules.length && st.selectedId !== "new") {
    ul.innerHTML = '<li class="empty muted small">Sin reglas. Dibuja una línea de puerta o una zona de cola.</li>';
    return;
  }
  const items = st.rules.map((r) => `<li><button type="button" data-rule="${esc(r.id)}" aria-current="${r.id === st.selectedId}">
    <span class="swatch ${r.kind}"></span><span>${esc(r.name)}</span>
    <span class="meta">${r.kind === "line" ? "Puerta" : `Cola · ≥${Number(r.alert_threshold)}`}${r.enabled ? "" : " · inactiva"}</span></button></li>`);
  if (st.selectedId === "new" && st.draft) {
    items.push(`<li><button type="button" aria-current="true"><span class="swatch ${st.draft.kind}"></span>
      <span>${esc(st.draft.name)}</span><span class="meta">nueva</span></button></li>`);
  }
  ul.innerHTML = items.join("");
}

$("#an-rules").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-rule]");
  if (b) trySelect(b.dataset.rule);
});

async function trySelect(id) {
  if (id === st.selectedId) return;
  if (st.dirty) {
    const ok = await confirmDialog("Cambios sin guardar", "La regla actual tiene cambios sin guardar. ¿Descartarlos?",
      { okText: "Descartar", danger: true });
    if (!ok) return;
  }
  selectRule(id);
}

function selectRule(id) {
  setTool(null);
  st.selectedId = id;
  const r = st.rules.find((x) => x.id === id);
  st.draft = r ? JSON.parse(JSON.stringify(r)) : null;
  st.dirty = false;
  form.hidden = !st.draft;
  if (st.draft) fillForm();
  renderRuleList();
  setTool(null);
}

function fillForm() {
  const d = st.draft;
  if (!d) return;
  $("#rule-title").textContent = st.selectedId === "new" ? "Nueva regla" : `Regla «${d.name}»`;
  $("#rule-kind").textContent = d.kind === "line" ? "Línea de puerta" : "Zona de cola";
  $$("[data-kind]", form).forEach((el) => { el.hidden = el.dataset.kind !== d.kind; });
  form.name.value = d.name || "";
  form.enabled.checked = d.enabled !== false;
  form.invert.checked = !!d.invert;
  if (d.kind === "zone") {
    form.alert_threshold.value = d.alert_threshold ?? 5;
    form.alert_min_seconds.value = d.alert_min_seconds ?? 60;
    form.alert_cooldown_seconds.value = d.alert_cooldown_seconds ?? 600;
    form.clear_below.value = d.clear_below ?? "";
  }
  $("#rule-delete").hidden = st.selectedId === "new";
  const occ = st.occupancy[st.selectedId];
  const pill = $("#rule-occupancy");
  pill.hidden = occ == null;
  if (occ != null) pill.textContent = `${occ} personas ahora`;
  setError("");
}

function setError(text) {
  const el = $("#rule-error");
  el.textContent = text;
  el.hidden = !text;
}

form.addEventListener("input", (ev) => {
  const d = st.draft;
  if (!d) return;
  const t = ev.target;
  if (t.name === "name") d.name = t.value;
  else if (t.name === "enabled") d.enabled = t.checked;
  else if (t.name === "invert") d.invert = t.checked;
  else if (["alert_threshold", "alert_min_seconds", "alert_cooldown_seconds"].includes(t.name)) d[t.name] = Number(t.value);
  else if (t.name === "clear_below") d.clear_below = t.value === "" ? null : Number(t.value);
  st.dirty = true;
  if (t.name !== "name") render();
  else renderRuleList();
});

function ruleBody(d) {
  const base = { kind: d.kind, camera_id: d.camera_id, name: (d.name || "").trim(), enabled: d.enabled !== false };
  if (d.kind === "line") return { ...base, start: d.start, end: d.end, invert: !!d.invert };
  return {
    ...base, polygon: d.polygon,
    alert_threshold: Number(d.alert_threshold), alert_min_seconds: Number(d.alert_min_seconds),
    alert_cooldown_seconds: Number(d.alert_cooldown_seconds),
    clear_below: d.clear_below === null || d.clear_below === undefined || d.clear_below === "" ? null : Number(d.clear_below),
  };
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const d = st.draft;
  if (!d) return;
  const body = ruleBody(d);
  if (!body.name) { setError("Ponle un nombre a la regla."); form.name.focus(); return; }
  if (d.kind === "line" && !(d.start && d.end)) { setError("Dibuja la línea sobre la imagen."); return; }
  if (d.kind === "zone" && !(d.polygon && d.polygon.length >= 3)) { setError("Dibuja la zona sobre la imagen."); return; }
  if (d.kind === "zone" && body.clear_below !== null && body.clear_below >= body.alert_threshold) {
    setError("El fin del aviso debe ser menor que el número de personas que lo activa.");
    return;
  }
  await busy($("#rule-save"), async () => {
    try {
      const saved = st.selectedId === "new"
        ? await post("/api/analytics/rules", body)
        : await put(`/api/analytics/rules/${enc(st.selectedId)}`, body);
      toast(`Regla «${saved.name}» guardada`, "ok");
      await loadRules();
      st.dirty = false;
      selectRule(saved.id);
    } catch (err) {
      setError(errorText(err));
    }
  });
});

$("#rule-cancel").addEventListener("click", () => {
  st.dirty = false;
  selectRule(st.selectedId === "new" ? null : st.selectedId);
});
$("#rule-redraw").addEventListener("click", () => {
  if (!st.draft) return;
  if (st.draft.kind === "line") { delete st.draft.start; delete st.draft.end; }
  else delete st.draft.polygon;
  st.dirty = true;
  setTool(st.draft.kind);
  canvas.focus();
});
$("#rule-delete").addEventListener("click", async (ev) => {
  const id = st.selectedId;
  const r = st.rules.find((x) => x.id === id);
  if (!r) return;
  const ok = await confirmDialog("Borrar regla", `Se borrará «${r.name}». Los conteos ya guardados se conservan.`,
    { okText: "Borrar", danger: true });
  if (!ok) return;
  await busy(ev.currentTarget, async () => {
    try {
      await del(`/api/analytics/rules/${enc(id)}`);
      toast("Regla borrada", "ok");
      st.dirty = false;
      await loadRules();
      selectRule(null);
    } catch (err) {
      toastError(err, "No se pudo borrar la regla");
    }
  });
});

// ------------------------------------------------------------------ datos
async function loadRules() {
  st.rules = st.cameraId ? await get(`/api/analytics/rules?camera_id=${enc(st.cameraId)}`) : [];
  renderRuleList();
  render();
}

async function loadCameraSettings() {
  let list = [];
  try {
    list = await get("/api/analytics/cameras");
  } catch (err) {
    toastError(err, "No se pudieron leer los ajustes de analítica");
  }
  const a = list.find((x) => x.camera_id === st.cameraId) || { enabled: false, fps: 2, detector: "rfdetr-nano", confidence: 0.5 };
  const f = $("#an-cam-form");
  f.enabled.checked = !!a.enabled;
  f.fps.value = a.fps;
  f.detector.value = a.detector;
  f.confidence.value = a.confidence;
}

async function loadOccupancy() {
  try {
    const s = await get("/api/analytics/status");
    const cam = (s.cameras || []).find((c) => c.camera_id === st.cameraId);
    st.occupancy = (cam && cam.occupancy) || {};
  } catch (err) {
    st.occupancy = {};
    if (!(err instanceof ApiError && err.status === 401)) console.warn("Estado de analítica no disponible", err);
  }
}

let snapshotCtrl = null;
async function loadSnapshot() {
  if (!st.cameraId) return;
  const msg = $("#an-stage-msg");
  msg.hidden = false;
  msg.textContent = "Pidiendo imagen a la cámara…";
  snapshotCtrl?.abort();
  snapshotCtrl = new AbortController();
  try {
    const res = await fetch(`/api/cameras/${enc(st.cameraId)}/snapshot?stream=sub`, {
      credentials: "same-origin", cache: "no-store", signal: snapshotCtrl.signal, headers: { "X-Requested-With": "vms" },
    });
    if (!res.ok) {
      let text = `Error ${res.status}`;
      try { text = (await res.json()).error.message; } catch { /* sin JSON */ }
      throw new Error(text);
    }
    const blob = await res.blob();
    if (st.imageUrl) URL.revokeObjectURL(st.imageUrl);
    st.imageUrl = URL.createObjectURL(blob);   // solo en memoria del navegador
    img.src = st.imageUrl;
    await img.decode().catch(() => {});
    if (img.naturalWidth && img.naturalHeight) stage.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
    msg.hidden = true;
  } catch (err) {
    if (err.name === "AbortError") return;
    img.removeAttribute("src");
    msg.hidden = false;
    msg.textContent = `No se pudo obtener la imagen: ${err.message}. Puedes dibujar igualmente; la proporción será 16:9.`;
  }
  render();
}

async function selectCamera(id) {
  st.cameraId = id;
  st.dirty = false;
  selectRule(null);
  history.replaceState(null, "", `/analytics?camera=${enc(id)}`);
  await Promise.all([loadRules(), loadCameraSettings(), loadOccupancy()]);
  render();
  loadSnapshot();
}

$("#an-camera").addEventListener("change", async (ev) => {
  if (st.dirty) {
    const ok = await confirmDialog("Cambios sin guardar", "Hay una regla sin guardar. ¿Descartarla?", { okText: "Descartar", danger: true });
    if (!ok) { ev.target.value = st.cameraId; return; }
  }
  selectCamera(ev.target.value);
});
$("#an-snapshot").addEventListener("click", loadSnapshot);

$("#an-cam-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const body = {
    camera_id: st.cameraId, enabled: f.enabled.checked, fps: Number(f.fps.value), detector: f.detector.value,
    confidence: Number(f.confidence.value), stream: "sub",
  };
  await busy($("#an-cam-save"), async () => {
    try {
      await put(`/api/analytics/cameras/${enc(st.cameraId)}`, body);
      toast("Ajustes de analítica guardados", "ok");
    } catch (err) {
      toastError(err, "No se pudieron guardar los ajustes");
    }
  });
});

window.addEventListener("resize", () => render());
new ResizeObserver(() => render()).observe(stage);
window.addEventListener("beforeunload", (ev) => {
  if (st.dirty) { ev.preventDefault(); ev.returnValue = ""; }
  if (st.imageUrl) URL.revokeObjectURL(st.imageUrl);
});

async function main() {
  try {
    st.me = await get("/api/auth/me");
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Sin conexión con el servidor");
    return;
  }
  mountShell("analytics", st.me);
  if (st.me.role !== "admin") {
    $(".an-layout").innerHTML = '<div class="banner">Solo un administrador puede configurar la analítica.</div>';
    return;
  }
  try {
    st.cameras = await get("/api/cameras");
  } catch (err) {
    toastError(err, "No se pudieron cargar las cámaras");
    return;
  }
  const sel = $("#an-camera");
  if (!st.cameras.length) {
    sel.innerHTML = '<option value="">No hay cámaras</option>';
    sel.disabled = true;
    $("#an-stage-msg").hidden = false;
    $("#an-stage-msg").textContent = "Añade cámaras desde el panel de control.";
    return;
  }
  sel.innerHTML = st.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}${c.device_name ? ` · ${esc(c.device_name)}` : ""}</option>`).join("");
  const wanted = new URLSearchParams(location.search).get("camera");
  const id = st.cameras.some((c) => c.id === wanted) ? wanted : st.cameras[0].id;
  sel.value = id;
  await selectCamera(id);
  setInterval(async () => { await loadOccupancy(); if (!st.drag) render(); }, 10000);
}

// Estado para diagnóstico y pruebas automáticas.
window.__vmsAnalytics = {
  get rules() { return JSON.parse(JSON.stringify(st.rules)); },
  get draft() { return st.draft ? JSON.parse(JSON.stringify(st.draft)) : null; },
  get tool() { return st.tool; },
};

main();
