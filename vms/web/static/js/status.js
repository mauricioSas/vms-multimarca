// Estado del sistema: motor, cámaras, grabación, disco y analítica (GET /api/status cada 5 s).
import { ApiError, get, subscribeEvents } from "./api.js";
import { $, esc, mountShell, fmtBytes, fmtDuration, fmtDateTime, fmtTime, toastError } from "./ui.js";

const REFRESH_MS = 5000;
let me = null;
let timer = null;
let inFlight = false;
let rateTimer = null;
let prevBytes = new Map();   // camera_id → [bytes, ms]

// Motivos de EngineStatus.problem (CONTRATO §6.8) que no son una avería: el motor está arrancando.
const ENGINE_STARTING = new Set(["starting", "waiting_config"]);
const ENGINE_BROKEN = new Set(["port_in_use", "bad_config", "crash_loop", "missing_program"]);
// Solo falla la API: MediaMTX puede seguir grabando con mediamtx.yml (modo attach), pero no se puede comprobar.
const ENGINE_UNREACHABLE = new Map([["api_auth", "Sin conexión"], ["no_api", "No responde"]]);

function setText(id, text) { $(id).textContent = text; }

function engineProblem(eng) {
  return eng && !eng.running && eng.problem ? eng.problem : null;
}

function engineLabel(eng) {
  if (eng.running) return "En marcha";
  const p = engineProblem(eng);
  if (p && ENGINE_STARTING.has(p.code)) return "Arrancando";
  if (p && ENGINE_BROKEN.has(p.code)) return "No arranca";
  if (p && ENGINE_UNREACHABLE.has(p.code)) return ENGINE_UNREACHABLE.get(p.code);
  return "Detenido";
}

// Por qué no funciona el motor, con su «Qué hacer:». Puede citar una línea de engine.log: solo textContent.
function renderEngineProblem(eng) {
  const box = $("#st-engine-problem");
  const p = engineProblem(eng);
  box.hidden = !p;
  if (!p) return;
  box.className = ENGINE_STARTING.has(p.code) ? "banner" : "banner bad";
  setText("#st-engine-problem-detail", p.detail_es || "");
  setText("#st-engine-problem-action", p.action_es || "");
}

function overall(s, health) {
  const box = $("#st-overall");
  const cams = s.cameras || [];
  const offline = cams.filter((c) => !c.online);
  const notSaving = cams.filter((c) => c.online && c.recording_error);   // con vídeo, pero no se guarda
  const diskBad = s.disk && s.disk.percent >= 90;
  const a = s.analytics || {};
  const analyticsBad = a.running && a.stale;
  let cls = "info";
  let text = "Todo funciona con normalidad.";
  const p = engineProblem(s.engine);
  if (p && ENGINE_STARTING.has(p.code)) {
    cls = "";
    text = "El motor de vídeo está arrancando: en unos segundos vuelven la vista en vivo y la grabación.";
  } else if (p && ENGINE_UNREACHABLE.has(p.code)) {
    cls = "bad";
    text = "VMS Multimarca no puede comunicarse con el motor de vídeo: la vista en vivo no funciona y no se puede " +
      "comprobar si graba. Debajo tienes el motivo y qué hacer.";
  } else if (!s.engine || !s.engine.running) {
    cls = "bad";
    text = p
      ? "El motor de vídeo no funciona: no hay vista en vivo ni grabación. Debajo tienes el motivo y qué hacer."
      : "El motor de vídeo no funciona: no hay vista en vivo ni grabación. Qué hacer: espera un minuto; si no vuelve, reinicia el ordenador.";
  } else if (offline.length || diskBad || analyticsBad) {
    cls = "";
    const parts = [];
    if (offline.length) parts.push(`${offline.length} ${offline.length === 1 ? "cámara sin vídeo" : "cámaras sin vídeo"}`);
    if (diskBad) parts.push("disco casi lleno");
    if (analyticsBad) parts.push("la analítica no informa");
    text = `Atención: ${parts.join(", ")}.`;
  }
  if (s.engine && s.engine.running && notSaving.length) {
    // Lo peor para una tienda: «Grabando» sin guardar nada. Va en rojo y delante de lo demás.
    const n = notSaving.length;
    const rest = cls === "info" ? "" : ` ${text}`;
    cls = "bad";
    text = `${n} ${n === 1 ? "cámara tiene vídeo pero no graba" : "cámaras tienen vídeo pero no graban"}. Qué hacer: sigue lo que dice la columna «Último error» de la tabla «Cámaras».${rest}`;
  }
  box.className = `banner ${cls}`.trim();
  box.textContent = text + (health && health.version ? ` · Versión ${health.version}` : "");
}

function render(s, health) {
  overall(s, health);
  const eng = s.engine || {};
  const label = engineLabel(eng);
  setText("#st-engine", label);
  $("#st-engine").style.color = eng.running ? "var(--live)" : label === "Arrancando" ? "var(--warn)" : "var(--danger)";
  const hint = [];
  if (eng.version) hint.push(`MediaMTX ${eng.version}`);
  if (eng.started_at) hint.push(`desde ${fmtDateTime(eng.started_at)}`);
  if (eng.restarts) hint.push(`${eng.restarts} reinicios`);
  if (eng.crashes_10min) hint.push(`${eng.crashes_10min} ${eng.crashes_10min === 1 ? "caída" : "caídas"} en 10 min`);
  // Con `problem`, el motivo y el «Qué hacer:» van en su aviso: aquí no se repiten
  if (eng.last_error && !engineProblem(eng)) hint.push(`último error: ${eng.last_error}`);
  setText("#st-engine-hint", hint.join(" · "));
  renderEngineProblem(eng);

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
        <td>${c.recording ? '<span class="pill ok">Grabando</span>'
          : c.recording_error ? '<span class="pill bad">No graba</span>' : '<span class="pill off">No graba</span>'}</td>
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
    document.dispatchEvent(new CustomEvent("vms:status", { detail: s }));   // power.js y otros módulos
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
