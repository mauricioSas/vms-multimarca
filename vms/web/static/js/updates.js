// #updates-root: estado de la actualización de la sede (versión, canal, último resultado). Dueño: B4.
// Contrato: CONTRATO §15.6 (GET /api/updates/status, rol operador). Solo lectura: buscar actualizaciones o
// volver a la versión anterior se hace con permisos de administrador del equipo (bandeja del visor o
// «vmsctl update …» elevado) o desde el panel central, nunca desde esta página.
import { ApiError, get } from "./api.js";
import { $, esc, fmtDateTime } from "./ui.js";

const REFRESH_MS = 15000;

const RESULTS = {
  update_ok: ["ok", "Actualizado"],
  rollback_ok: ["warn", "Vuelta atrás hecha"],
  update_failed: ["bad", "Falló y se volvió a la versión anterior"],
  metadata_expired: ["bad", "Información de versiones caducada"],
  clock_skew: ["bad", "Reloj del equipo desfasado"],
  no_update: ["ok", "Al día"],
  waiting_window: ["warn", "Esperando la ventana de mantenimiento"],
  reboot_pending: ["warn", "Windows tiene un reinicio pendiente"],
  disk_full: ["bad", "Sin espacio en disco"],
  held: ["warn", "Retenida desde el panel"],
  min_from: ["warn", "Hace falta el instalador completo"],
  error: ["bad", "Error al comprobar"],
  none: ["off", "Sin comprobar todavía"],
};

const STATES = {
  idle: "En reposo", good: "Terminada", downloaded: "Preparando", backed_up: "Copia de seguridad hecha",
  stopping: "Parando servicios", switched: "Cambiando de versión", migrated: "Migrando la configuración",
  started: "Arrancando servicios", verifying: "Comprobando que todo funciona", rolling_back: "Volviendo atrás",
  rolled_back: "Se volvió a la versión anterior", unknown: "Desconocido",
};

function pill(result) {
  const [cls, text] = RESULTS[result] || ["off", result || "—"];
  return `<span class="pill ${cls}">${esc(text)}</span>`;
}

function row(label, value) {
  return `<dt>${esc(label)}</dt><dd>${value}</dd>`;
}

function render(s) {
  const root = $("#updates-root");
  if (!root) return;
  const skew = typeof s.clock_skew_s === "number" ? `${s.clock_skew_s > 0 ? "+" : ""}${s.clock_skew_s.toFixed(1)} s` : "—";
  const rows = [
    row("Versión en marcha", esc(s.running || s.installed || "—")),
    row("Versión instalada", esc(s.installed || "—")),
    row("Canal", esc(s.channel || "—") + (s.hold ? ' <span class="pill warn">Retenida</span>' : "")),
    row("Último resultado", pill(s.last_result)),
    row("Estado", esc(STATES[s.state] || s.state || "—")),
    row("Última comprobación", esc(fmtDateTime(s.last_check))),
    row("Versión disponible", esc(s.available || "—")),
    row("Información de versiones válida hasta", esc(fmtDateTime(s.metadata_expires))),
    row("Diferencia de reloj con el servidor", esc(skew)),
  ];
  if (s.reboot_pending) rows.push(row("Windows", '<span class="pill warn">Reinicio pendiente</span>'));
  const msg = s.message_es ? `<p class="upd-msg ${["update_failed", "metadata_expired", "clock_skew", "error", "disk_full"].includes(s.last_result) ? "bad" : ""}">${esc(s.message_es)}</p>` : "";
  root.innerHTML = `
    <section class="section" aria-labelledby="h-st-updates">
      <div class="section-head"><h2 id="h-st-updates">Actualizaciones</h2></div>
      <div class="card card-pad">
        ${msg}
        <dl class="kv">${rows.join("")}</dl>
        <p class="muted small upd-help">Las actualizaciones se aplican solas en la ventana de mantenimiento y, si algo
        falla, el equipo vuelve solo a la versión anterior. Para buscar ahora o volver atrás hacen falta permisos
        de administrador del equipo (icono del visor en la bandeja) o el panel central.</p>
      </div>
    </section>`;
  root.hidden = false;
}

async function refresh() {
  try {
    render(await get("/api/updates/status"));
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      const root = $("#updates-root");
      if (root) root.hidden = true;
      return;
    }
    console.warn("No se pudo leer el estado de las actualizaciones", err);
  }
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

export { render, refresh };
