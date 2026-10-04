// Estado del sistema: motor, cámaras, grabación, disco y analítica (GET /api/status cada 5 s).
import { ApiError, get, subscribeEvents } from "./api.js";
import { $, esc, mountShell, fmtBytes, fmtDuration, fmtDateTime, fmtTime, toastError } from "./ui.js";

const REFRESH_MS = 5000;
let me = null;
let timer = null;
let inFlight = false;
let rateTimer = null;
let prevBytes = new Map();   // camera_id → [bytes, ms]

function setText(id, text) { $(id).textContent = text; }

function overall(s, health) {
  const box = $("#st-overall");
  const cams = s.cameras || [];
  const offline = cams.filter((c) => !c.online);
  const diskBad = s.disk && s.disk.percent >= 90;
  const a = s.analytics || {};
  const analyticsBad = a.running && a.stale;
  let cls = "info";
  let text = "Todo funciona con normalidad.";
  if (!s.engine || !s.engine.running) {
    cls = "bad";
    text = "El motor de vídeo está detenido: no hay vista en vivo ni grabación. Se reinicia solo; si no vuelve, revisa el registro.";
  } else if (offline.length || diskBad || analyticsBad) {
    cls = "";
    const parts = [];
    if (offline.length) parts.push(`${offline.length} ${offline.length === 1 ? "cámara sin vídeo" : "cámaras sin vídeo"}`);
    if (diskBad) parts.push("disco casi lleno");
    if (analyticsBad) parts.push("la analítica no informa");
    text = `Atención: ${parts.join(", ")}.`;
  }
  box.className = `banner ${cls}`.trim();
  box.textContent = text + (health && health.version ? ` · Versión ${health.version}` : "");
}

function render(s, health) {
  overall(s, health);
  const eng = s.engine || {};
  setText("#st-engine", eng.running ? "En marcha" : "Detenido");
  $("#st-engine").style.color = eng.running ? "var(--live)" : "var(--danger)";
  const hint = [];
  if (eng.version) hint.push(`MediaMTX ${eng.version}`);
  if (eng.started_at) hint.push(`desde ${fmtDateTime(eng.started_at)}`);
  if (eng.restarts) hint.push(`${eng.restarts} reinicios`);
  if (eng.last_error) hint.push(`último error: ${eng.last_error}`);
  setText("#st-engine-hint", hint.join(" · "));

  const cams = s.cameras || [];
  const online = cams.filter((c) => c.online).length;
  const rec = cams.filter((c) => c.recording).length;
  $("#st-cams").innerHTML = `${online}<small> / ${cams.length}</small>`;
  setText("#st-cams-hint", cams.length - online ? `${cams.length - online} sin vídeo` : "todas con vídeo");
  $("#st-rec").innerHTML = `${rec}<small> / ${cams.length}</small>`;

  const d = s.disk;
  if (d) {
    $("#st-disk").innerHTML = `${Number(d.percent).toFixed(0)}<small> %</small>`;
    const meter = $("#st-disk-meter");
    meter.className = `meter ${d.percent >= 90 ? "bad" : d.percent >= 80 ? "warn" : ""}`;
    meter.firstElementChild.style.width = `${Math.min(100, d.percent)}%`;
    meter.setAttribute("aria-valuenow", String(Math.round(d.percent)));
    setText("#st-disk-hint", `${fmtBytes(d.free)} libres de ${fmtBytes(d.total)}`);
  }

  const a = s.analytics || {};
  if (!a.running) {
    setText("#st-analytics", "Inactiva");
    $("#st-analytics").style.color = "var(--muted)";
    setText("#st-analytics-hint", "El servicio de analítica no está en marcha en esta sede.");
  } else {
    setText("#st-analytics", a.stale ? "Sin informar" : "En marcha");
    $("#st-analytics").style.color = a.stale ? "var(--warn)" : "var(--live)";
    const n = (a.cameras || []).length;
    const db = a.db || {};
    const waiting = (db.spool_pending || 0) + (db.memory_pending || 0);
    const pend = waiting ? ` · ${waiting} lotes de conteos pendientes de enviar` : "";
    const disk = db.disk_error ? " · DISCO LLENO: los conteos esperan en memoria" : "";
    setText("#st-analytics-hint", `${n} ${n === 1 ? "cámara" : "cámaras"} analizadas${pend}${disk}`);
    if (db.disk_error) $("#st-analytics").style.color = "var(--danger)";
  }

  const now = Date.now();
  const tbody = $("#st-cams-table tbody");
  if (!cams.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No hay cámaras dadas de alta.</td></tr>';
  } else {
    tbody.innerHTML = cams.map((c) => {
      const prev = prevBytes.get(c.camera_id);
      let rate = "";
      if (prev && now > prev[1] && c.bytes_received >= prev[0]) {
        const bps = ((c.bytes_received - prev[0]) * 8) / ((now - prev[1]) / 1000);
        rate = ` · ${(bps / 1e6).toFixed(2)} Mbit/s`;
      }
      prevBytes.set(c.camera_id, [c.bytes_received, now]);
      return `<tr>
        <td><strong>${esc(c.name)}</strong><div class="muted small mono">${esc(c.camera_id)}</div></td>
        <td>${c.online ? '<span class="pill ok">En vivo</span>' : '<span class="pill bad">Sin vídeo</span>'}</td>
        <td>${c.recording ? '<span class="pill ok">Grabando</span>' : '<span class="pill off">No graba</span>'}</td>
        <td>${Number(c.readers || 0)}</td>
        <td class="nowrap">${fmtBytes(c.bytes_received)}${rate}</td>
        <td class="small">${c.last_error ? esc(c.last_error) : '<span class="muted">—</span>'}</td>
      </tr>`;
    }).join("");
  }

  const sys = [
    ["Almacén de contraseñas", s.credential_backend === "keyring" ? "Almacén de credenciales del sistema" : s.credential_backend === "file" ? "Archivo cifrado" : (s.credential_backend || "—")],
    ["Carpeta de grabaciones", d ? d.path : "—"],
    ["Tiempo en marcha", health && health.uptime_s != null ? fmtDuration(health.uptime_s) : "—"],
    ["Estado general", health ? { ok: "Correcto", degraded: "Con avisos", down: "Detenido" }[health.status] || health.status : "—"],
  ];
  $("#st-system").innerHTML = sys.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

  const warn = $("#st-config-warning");
  warn.hidden = !s.config_warning;
  warn.textContent = s.config_warning || "";
  setText("#st-updated", `Actualizado a las ${fmtTime(new Date())}`);
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  try {
    const [s, health] = await Promise.all([get("/api/status"), get("/api/health", { auth: false }).catch(() => null)]);
    render(s, health);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    const box = $("#st-overall");
    box.className = "banner bad";
    box.textContent = `No se pudo leer el estado: ${err.message}. Se reintenta automáticamente.`;
  } finally {
    inFlight = false;
  }
}

async function main() {
  try {
    me = await get("/api/auth/me");
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Sin conexión con el servidor");
    return;
  }
  mountShell("status", me);
  $("#st-refresh").addEventListener("click", refresh);
  await refresh();
  timer = setInterval(refresh, REFRESH_MS);
  subscribeEvents({
    config: () => { clearTimeout(rateTimer); rateTimer = setTimeout(refresh, 500); },
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearInterval(timer); timer = null; }
    else if (!timer) { refresh(); timer = setInterval(refresh, REFRESH_MS); }
  });
}

main();
