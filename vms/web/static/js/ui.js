// Utilidades de interfaz compartidas por las páginas del panel.
import { logout } from "./api.js";

/** Escapa texto para insertarlo en HTML. Todo dato del servidor pasa por aquí. */
export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ICONS = {
  panel: '<path d="M3 4h7v7H3zM14 4h7v4h-7zM14 11h7v9h-7zM3 14h7v6H3z"/>',
  wall: '<path d="M2 5h20v12H2zM8 21h8M12 17v4"/>',
  playback: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4l3 2"/>',
  analytics: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  status: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  logout: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
};

export function icon(name, cls = "ico") {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

const NAV = [
  { href: "/", label: "Equipos y monitores", icon: "panel", key: "panel" },
  { href: "/playback", label: "Reproducción", icon: "playback", key: "playback" },
  { href: "/analytics", label: "Analítica", icon: "analytics", key: "analytics", admin: true },
  { href: "/status", label: "Estado del sistema", icon: "status", key: "status" },
];

/** Pinta la barra lateral común y devuelve el contenedor principal. */
export function mountShell(active, me) {
  const shell = document.querySelector(".shell");
  const aside = document.getElementById("sidebar");
  const isAdmin = me && me.role === "admin";
  const links = NAV.filter((n) => !n.admin || isAdmin).map((n) =>
    `<a href="${n.href}" ${n.key === active ? 'aria-current="page"' : ""}>${icon(n.icon)}<span>${esc(n.label)}</span></a>`
  ).join("");
  const walls = [1, 2, 3, 4].map((m) =>
    `<a href="/wall/${m}" target="vms-wall-${m}" rel="noopener">${icon("wall")}<span>Monitor ${m}</span></a>`
  ).join("");
  aside.innerHTML = `
    <div class="brand"><div class="brand-mark" aria-hidden="true"></div>
      <div><div class="brand-name">VMS</div><div class="brand-sub">Multimarca</div></div></div>
    <nav class="nav" aria-label="Secciones">
      ${links}
      <div class="nav-label">Muros</div>
      ${walls}
    </nav>
    <div class="sidebar-foot">
      <div class="user-chip">
        <span><b>${esc(me?.username || "")}</b><br><span class="muted small">${me?.role === "admin" ? "Administrador" : "Operador"}</span></span>
        <button type="button" class="btn btn-ghost btn-sm" id="logout-btn" title="Cerrar sesión">${icon("logout")}<span class="sr-only">Cerrar sesión</span></button>
      </div>
    </div>`;
  document.getElementById("logout-btn").addEventListener("click", logout);
  const toggle = document.getElementById("menu-toggle");
  if (toggle) {
    toggle.innerHTML = icon("menu") + '<span class="sr-only">Menú</span>';
    toggle.addEventListener("click", () => {
      const open = shell.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", String(open));
    });
  }
  document.body.classList.toggle("is-admin", isAdmin);
}

// ------------------------------------------------------------------ avisos
let toastBox = null;
export function toast(message, kind = "info", ms = 4500) {
  if (!toastBox) {
    toastBox = document.createElement("div");
    toastBox.className = "toasts";
    toastBox.setAttribute("role", "status");
    toastBox.setAttribute("aria-live", "polite");
    document.body.append(toastBox);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  toastBox.append(el);
  setTimeout(() => el.remove(), ms);
}

export function errorText(err) {
  if (!err) return "Error desconocido";
  if (err.name === "ApiError") {
    const fields = err.fields || [];
    if (fields.length) {
      return err.message + ": " + fields.map((f) => `${(f.loc || []).filter((x) => x !== "body").join(".")} — ${f.msg}`).join("; ");
    }
    return err.message;
  }
  return err.message || String(err);
}

export function toastError(err, prefix = "") {
  console.error(prefix, err);
  toast((prefix ? prefix + ": " : "") + errorText(err), "bad", 7000);
}

/** Marca un botón como ocupado mientras dura la promesa. */
export async function busy(button, fn) {
  if (!button) return fn();
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

/** Diálogo de confirmación accesible (usa <dialog>). */
export function confirmDialog(title, message, { okText = "Aceptar", danger = false } = {}) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "modal";
    dlg.style.width = "min(460px, calc(100vw - 32px))";
    dlg.innerHTML = `
      <div class="modal-head"><h2>${esc(title)}</h2></div>
      <div class="modal-body"><p style="margin:0">${esc(message)}</p></div>
      <div class="modal-foot"><div class="right">
        <button type="button" class="btn" value="no">Cancelar</button>
        <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" value="yes">${esc(okText)}</button>
      </div></div>`;
    document.body.append(dlg);
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    dlg.querySelector('[value="no"]').addEventListener("click", () => done(false));
    dlg.querySelector('[value="yes"]').addEventListener("click", () => done(true));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    dlg.showModal();
    dlg.querySelector('[value="yes"]').focus();
  });
}

/** Marca en el formulario los campos con error de validación de la API (details.fields). */
export function showFieldErrors(form, err) {
  clearFieldErrors(form);
  const fields = err && err.fields ? err.fields : [];
  let firstEl = null;
  for (const f of fields) {
    const loc = (f.loc || []).filter((x) => x !== "body");
    const name = loc[0];
    if (!name) continue;
    const input = form.querySelector(`[name="${CSS.escape(String(name))}"]`);
    if (!input) continue;
    input.setAttribute("aria-invalid", "true");
    const msg = document.createElement("small");
    msg.className = "field-error";
    msg.textContent = f.msg;
    input.closest(".field")?.append(msg);
    firstEl ||= input;
  }
  firstEl?.focus();
  return fields.length > 0;
}

export function clearFieldErrors(form) {
  form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
  form.querySelectorAll(".field-error").forEach((el) => el.remove());
}

// ------------------------------------------------------------------ formatos
export const VENDOR_LABELS = { hikvision: "Hikvision", dahua: "Dahua", onvif: "ONVIF", generic: "Genérico" };

export function fmtBytes(n) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  n = Number(n);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(Number(seconds) || 0));
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${s} s`;
  return `${s} s`;
}

export function pad2(n) { return String(n).padStart(2, "0"); }

export function fmtTime(date, withSeconds = true) {
  const t = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  return withSeconds ? `${t}:${pad2(date.getSeconds())}` : t;
}

export function fmtDateTime(value) {
  if (!value) return "—";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)}/${d.getFullYear()} ${fmtTime(d)}`;
}

/** yyyy-mm-dd de una fecha local (para <input type="date">). */
export function localDateValue(d = new Date()) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** ISO 8601 UTC con Z, sin milisegundos si son 0. */
export function isoUtc(date) {
  return date.toISOString().replace(".000Z", "Z");
}
