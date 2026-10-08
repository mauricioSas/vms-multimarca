// #power-alerts y #power-root: energía del PC (batería, suspensión y tapa) en /status.
// Contrato: CONTRATO §6.8, campo `power` de GET /api/status (solo en Windows; si no viene, no se pinta nada).
// No pide nada por su cuenta: status.js lanza «vms:status» cada vez que lee /api/status. Solo lectura: los
// avisos dicen qué cambiar en Windows; la página no cambia nada.
import { $, esc } from "./ui.js";

const LID = { nothing: "No hacer nada", sleep: "Suspender", hibernate: "Hibernar", shutdown: "Apagar" };
let last = null;   // lo último pintado: sin cambios no se repinta (los avisos no se vuelven a anunciar)

function duration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts = [];
  if (h) parts.push(`${h} h`);
  if (m) parts.push(`${m} min`);
  if (s && !h) parts.push(`${s} s`);
  return parts.join(" ") || "0 s";
}

function idle(sleepS, hibernateS, warned) {
  if (sleepS == null) return '<span class="muted">No se pudo leer</span>';
  const text = sleepS ? `Se suspende tras ${duration(sleepS)} sin uso` : "No se suspende nunca";
  const cls = warned ? "warn" : sleepS ? "off" : "ok";
  return `<span class="pill ${cls}">${esc(text)}</span>${hibernateS ? esc(` · hiberna tras ${duration(hibernateS)}`) : ""}`;
}

function lid(action, warned) {
  if (!action) return '<span class="muted">No se pudo leer</span>';
  return `<span class="pill ${warned ? "warn" : action === "nothing" ? "ok" : "off"}">${esc(LID[action] || action)}</span>`;
}

function supply(p) {
  if (p.on_battery === true) {
    const left = p.battery_percent != null ? ` · queda un ${p.battery_percent} %` : "";
    return `<span class="pill warn">Con batería</span>${esc(left)}`;
  }
  if (p.on_battery === false) {
    const charge = p.has_battery && p.battery_percent != null ? ` · batería al ${p.battery_percent} %` : "";
    return `<span class="pill ok">Enchufado a la corriente</span>${esc(charge)}`;
  }
  return '<span class="muted">No se sabe</span>';
}

function render(s) {
  const alerts = $("#power-alerts");
  const root = $("#power-root");
  if (!alerts || !root) return;
  const p = s && s.power;
  const key = JSON.stringify(p ?? null);
  if (key === last) return;
  last = key;
  if (!p) {
    alerts.hidden = true;
    root.hidden = true;
    return;
  }
  const warnings = Array.isArray(p.warnings) ? p.warnings : [];
  const codes = new Set(warnings.map((w) => w.code));
  alerts.innerHTML = warnings.map((w) => `<div class="banner" role="alert">${esc(w.message_es)}</div>`).join("");
  alerts.hidden = !warnings.length;
  const rows = [
    ["Alimentación", supply(p)],
    ["Con corriente", idle(p.sleep_ac_s, p.hibernate_ac_s, codes.has("idle_sleep_ac"))],
  ];
  if (p.has_battery) rows.push(["Con batería", idle(p.sleep_dc_s, p.hibernate_dc_s, codes.has("on_battery"))]);
  if (p.lid_present) {
    rows.push(["Al cerrar la tapa con corriente", lid(p.lid_action_ac, codes.has("lid_ac"))],
      ["Al cerrar la tapa con batería", lid(p.lid_action_dc, false)]);
  }
  root.innerHTML = `
    <section class="section" aria-labelledby="h-st-power">
      <div class="section-head"><h2 id="h-st-power">Energía del PC</h2></div>
      <div class="card card-pad">
        <dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
        <p class="muted small" style="margin:14px 0 0">Mientras el PC está suspendido, hibernado o apagado no se
        graba ninguna cámara. Esta página solo lee la configuración de energía de Windows: no cambia nada.</p>
      </div>
    </section>`;
  root.hidden = false;
}

document.addEventListener("vms:status", (e) => render(e.detail));

export { render };
