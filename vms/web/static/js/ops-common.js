// Utilidades compartidas por los módulos de B6 (salud, evidencias, avisos, ayuda y onboarding).
// Dueño: B6. Todo dato del servidor se inserta como texto (nunca innerHTML con datos).
import { get, post, enc, subscribeEvents } from "./api.js";
import { toastError } from "./ui.js";

let mePromise = null;
/** Sesión actual (una sola petición por página). */
export function me() {
  if (!mePromise) mePromise = get("/api/auth/me").catch((err) => { mePromise = null; throw err; });
  return mePromise;
}

/** Crea un elemento: h("button", {class: "btn", onclick: fn}, "texto", nodo). Los textos se escapan siempre. */
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/**
 * Sección de B6 dentro de un contenedor de la página (CONTRATO §18.9): lo muestra y devuelve el cuerpo.
 * `id` = id del título (la ayuda «?» se engancha a él).
 */
export function section(root, { id, title, subtitle = "", actions = [] }) {
  root.hidden = false;
  const head = h("div", { class: "section-head" },
    h("div", { class: "ops-title" }, h("h2", { id }, title), subtitle ? h("span", { class: "muted small" }, subtitle) : null),
    actions.length ? h("div", { class: "ops-actions" }, actions) : null);
  const body = h("div", { class: "ops-body" });
  const sec = h("section", { class: "section ops-section", "aria-labelledby": id }, head, body);
  root.append(sec);
  return body;
}

// ------------------------------------------------------------------ estados (icono + texto, nunca solo color)
const STATUS = {
  ok: { cls: "ok", icon: "✔", text: "Correcto" },
  warning: { cls: "warn", icon: "⚠", text: "Aviso" },
  critical: { cls: "bad", icon: "✖", text: "Grave" },
  unknown: { cls: "off", icon: "?", text: "Sin datos" },
};
export function statusPill(status, text) {
  const s = STATUS[status] || STATUS.unknown;
  return h("span", { class: `pill plain ops-pill ${s.cls}`, dataset: { status } },
    h("span", { "aria-hidden": "true" }, s.icon), " ", text || s.text);
}

/** Estado vacío con las 3 pautas: qué pasa, qué aparecerá aquí y el camino directo. */
export function emptyState({ title, text, action }) {
  const box = h("div", { class: "empty ops-empty" }, h("strong", {}, title), h("span", {}, text));
  if (action) {
    const btn = action.href
      ? h("a", { class: "btn btn-sm btn-primary", href: action.href }, action.label)
      : h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: action.onClick }, action.label);
    box.append(h("div", { class: "ops-empty-action" }, btn));
  }
  return box;
}

export function fmtNum(n, digits = 0) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return "—";
  return Number(n).toLocaleString("es-ES", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
}

/** Diálogo modal sencillo construido con nodos. Devuelve {dlg, body, foot, close}. */
export function modal(title, { wide = false } = {}) {
  const dlg = h("dialog", { class: `modal ops-modal${wide ? " ops-modal-wide" : ""}`, "aria-label": title });
  const close = () => { dlg.close(); dlg.remove(); };
  const body = h("div", { class: "modal-body" });
  const foot = h("div", { class: "modal-foot" });
  dlg.append(h("div", { class: "modal-head" }, h("h2", {}, title),
    h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: close }, "Cerrar")), body, foot);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
  return { dlg, body, foot, close };
}

// ------------------------------------------------------------------ eventos SSE de B6
const OPS_EVENTS = ["health", "bookmark", "evidence", "notice"];
/**
 * Escucha los eventos de B6 (`health`, `bookmark`, `evidence`, `notice`) por la conexión única de la página
 * (`subscribeEvents` de api.js): una segunda EventSource por página agotaba el cupo de conexiones del navegador.
 * Nunca en los muros (CONTRATO §18.9). Devuelve la función para dejar de escuchar.
 */
export function onOpsEvent(name, fn) {
  if (!OPS_EVENTS.includes(name) || typeof EventSource === "undefined" || location.pathname.startsWith("/wall")) {
    return () => {};
  }
  const sub = subscribeEvents({
    [name]: (data) => {
      try { fn(data); } catch (err) { console.warn("Evento", name, err); }
    },
  });
  return () => sub.close();
}

// ------------------------------------------------------------------ «¿Por qué no conecta?»
const STEP_NAMES = {
  ping: "El equipo responde en la red", tcp_http: "Puerto web", tcp_rtsp: "Puerto de vídeo (RTSP)",
  http_response: "La web contesta", auth: "Usuario y contraseña", lockout: "Bloqueo del usuario",
  rtsp_describe: "El vídeo responde", codec: "Formato del vídeo (códec)", clock: "Hora del equipo",
  engine: "Motor de vídeo del VMS", path_ready: "Llega el vídeo al VMS",
};

export function renderDiagnosis(result) {
  const list = h("ol", { class: "ops-diag" });
  for (const s of result.steps) {
    const status = s.ok === true ? "ok" : s.ok === false ? "critical" : "unknown";
    list.append(h("li", { class: `ops-diag-step ${status}` },
      statusPill(status, s.ok === true ? "Bien" : s.ok === false ? "Falla" : "Sin comprobar"),
      h("div", {}, h("strong", {}, STEP_NAMES[s.code] || s.code), h("div", {}, s.detail_es),
        s.action_es ? h("div", { class: "ops-diag-action" }, "Qué hacer: ", s.action_es) : null)));
  }
  return h("div", { class: "ops-diag-box" },
    h("div", { class: `banner ${result.steps.some((s) => s.ok === false) ? "bad" : "info"}`, role: "status" },
      result.summary_es),
    list,
    result.llm_used ? h("p", { class: "muted small" }, "Resumen redactado automáticamente a partir de las comprobaciones.") : null);
}

/** Abre el diagnóstico de un equipo guardado o de una cámara. */
export async function openDiagnosis({ deviceId = null, cameraId = null, name = "" }) {
  const m = modal(`¿Por qué no conecta${name ? ` «${name}»` : ""}?`, { wide: true });
  m.body.append(h("p", { class: "muted" },
    "Revisando la red, los puertos, la contraseña (un solo intento, para no bloquear el usuario), el vídeo y la hora…"));
  try {
    const res = cameraId
      ? await post(`/api/diagnostics/camera/${enc(cameraId)}`)
      : await post("/api/diagnostics/device", { device_id: deviceId });
    clear(m.body).append(renderDiagnosis(res));
  } catch (err) {
    clear(m.body).append(h("p", { class: "form-error" }, err.message || "No se pudo hacer el diagnóstico"));
    toastError(err, "Diagnóstico");
  }
  return m;
}
