// #health-root (status.html): salud de imagen por cámara (0-100, causas, «Antes / Ahora», referencia, zonas
// excluidas), hora de los equipos, previsión de días de grabación con simulador, informe del día y
// diagnóstico de equipos. Dueño: B6. Contrato: CONTRATO §18.2-§18.5 y §18.11.
import { get, post, put, enc, ApiError } from "./api.js";
import { toast, toastError, busy, localDateValue } from "./ui.js";
import { h, clear, me, section, statusPill, emptyState, fmtNum, fmtWhen, modal, onOpsEvent, openDiagnosis } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("health-root");
const CAUSE = {
  no_reference: "Falta fijar la imagen de referencia", no_snapshot: "No se pudo obtener imagen", black: "Imagen negra",
  covered: "Cámara tapada", blurred: "Desenfocada", moved: "Movida", rotated: "Girada", scene_changed: "Mira a otro sitio",
  frozen: "Imagen congelada", ir_stuck: "Filtro de infrarrojos atascado", ir_weak: "Falla la luz infrarroja",
  degraded: "Calidad degradada (suciedad o niebla)", backlight: "Contraluz", color_cast: "Dominante de color",
  noise: "Mucho ruido", camera_event: "La cámara avisó de sabotaje",
};
const ui = { admin: false, cameras: [], health: new Map(), tbody: null };

function causesText(h0) {
  const m = h0.last_check?.metrics || {};
  return (h0.causes || []).map((c) => {
    let t = CAUSE[c] || c;
    if (c === "blurred" && m.sharpness_rel != null) t += ` (${Math.round(m.sharpness_rel * 100)} % de nitidez)`;
    if (c === "moved" && m.shift_px != null) t += ` (${Math.round(m.shift_px)} px)`;
    if (c === "rotated" && m.rotation_deg != null) t += ` (${Math.abs(Math.round(m.rotation_deg))}°)`;
    return t;
  }).join(", ");
}

// ================================================================== salud de imagen
function healthRow(cam) {
  const x = ui.health.get(cam.id) || { status: "unknown", causes: ["no_reference"], references: {} };
  const refs = x.references || {};
  const refText = refs.day || refs.night
    ? [refs.day ? `día ${fmtWhen(refs.day)}` : null, refs.night ? `noche ${fmtWhen(refs.night)}` : null].filter(Boolean).join(" · ")
    : "Sin referencia";
  const actions = h("div", { class: "row-actions" });
  if (ui.admin) {
    actions.append(
      h("button", { type: "button", class: "btn btn-sm", onclick: (e) => checkNow(cam, e.currentTarget) }, "Comprobar ahora"),
      h("button", { type: "button", class: "btn btn-sm", onclick: () => referenceDialog(cam) }, "Fijar referencia"),
      h("button", { type: "button", class: "btn btn-sm", disabled: !(refs.day || refs.night), onclick: () => beforeNow(cam, refs) },
        "Antes / Ahora"),
      h("button", { type: "button", class: "btn btn-sm", disabled: !(refs.day || refs.night), onclick: () => masksDialog(cam, refs) },
        "Zonas excluidas"));
  }
  return h("tr", { dataset: { camera: cam.id } },
    h("td", {}, h("strong", {}, cam.name), h("div", { class: "muted small" }, cam.device_name || "")),
    h("td", {}, statusPill(x.status)),
    h("td", { class: "mono" }, x.score == null ? "—" : String(x.score)),
    h("td", {}, causesText(x) || (x.status === "ok" ? "Imagen correcta" : "—"),
      x.since ? h("div", { class: "muted small" }, `desde ${fmtWhen(x.since)}`) : null),
    h("td", { class: "small" }, refText),
    h("td", {}, actions));
}

function renderHealth() {
  clear(ui.tbody);
  if (!ui.cameras.length) {
    ui.tbody.append(h("tr", {}, h("td", { colspan: "6" }, emptyState({
      title: "No hay cámaras que vigilar",
      text: "Cuando des de alta cámaras, aquí verás su puntuación de imagen de 0 a 100 y si alguien las tapa o las mueve.",
      action: { label: "Añadir cámaras", href: "/" } }))));
    return;
  }
  for (const cam of ui.cameras) ui.tbody.append(healthRow(cam));
}

async function loadHealth() {
  const [cams, health] = await Promise.all([get("/api/cameras"), get("/api/camera-health")]);
  ui.cameras = cams;
  ui.health = new Map(health.map((x) => [x.camera_id, x]));
  renderHealth();
}

async function checkNow(cam, btn) {
  await busy(btn, async () => {
    try {
      const chk = await post(`/api/camera-health/${enc(cam.id)}/check`);
      toast(`«${cam.name}»: ${chk.score == null ? "sin datos" : `puntuación ${chk.score}`}${chk.causes.length ? ` · ${causesText(chk)}` : ""}`,
        chk.status === "ok" ? "ok" : chk.status === "unknown" ? "info" : "bad", 7000);
      ui.health.set(cam.id, await get(`/api/camera-health/${enc(cam.id)}`));
      renderHealth();
    } catch (err) { toastError(err, "No se pudo comprobar"); }
  });
}

function referenceDialog(cam) {
  const m = modal(`Fijar la imagen de referencia de «${cam.name}»`);
  m.body.append(
    h("p", {}, "Se toman unas 15 imágenes durante unos 3 minutos y se guarda la «media»: así desaparece la gente que pasa. " +
      "Hazlo con la cámara bien colocada y, a ser posible, con la tienda vacía."),
    h("p", { class: "muted small" }, "La de noche, con la luz infrarroja encendida (imagen en blanco y negro). Solo se guarda esta imagen de referencia."));
  const status = h("div", { role: "status", class: "ob-diag" });
  m.body.append(status);
  const start = (kind) => async (e) => {
    await busy(e.currentTarget, async () => {
      try {
        const r = await post(`/api/camera-health/${enc(cam.id)}/reference`, { kind });
        for (;;) {
          const job = await get(`/api/camera-health/jobs/${enc(r.job_id)}`);
          clear(status).append(job.state === "running"
            ? statusPill("unknown", `Tomando imágenes ${job.frames} de ${job.total}…`)
            : statusPill(job.state === "done" ? (job.person_warning ? "warning" : "ok") : "critical", job.message_es));
          if (job.state !== "running") break;
          await new Promise((res) => setTimeout(res, 1500));
        }
        await loadHealth();
      } catch (err) { toastError(err, "No se pudo fijar la referencia"); }
    });
  };
  m.foot.append(h("div", { class: "right" },
    h("button", { type: "button", class: "btn", onclick: start("night") }, "Fijar referencia de noche"),
    h("button", { type: "button", class: "btn btn-primary", onclick: start("day") }, "Fijar referencia de día")));
}

function beforeNow(cam, refs) {
  const kind = refs.day ? "day" : "night";
  const m = modal(`Antes / Ahora · ${cam.name}`, { wide: true });
  const now = `/api/camera-health/${enc(cam.id)}/now.jpg?t=${Date.now()}`;
  m.body.append(h("div", { class: "ops-compare" },
    h("figure", {}, h("img", { src: `/api/camera-health/${enc(cam.id)}/reference.jpg?kind=${kind}`, alt: "Imagen de referencia" }),
      h("figcaption", {}, `Antes: referencia de ${kind === "day" ? "día" : "noche"} (${fmtWhen(refs[kind])})`)),
    h("figure", {}, h("img", { src: now, alt: "Imagen actual" }), h("figcaption", {}, "Ahora (no se guarda)"))),
    h("p", { class: "muted small" }, "Ver estas imágenes queda anotado en el registro de accesos."));
}

function masksDialog(cam, refs) {
  const kind = refs.day ? "day" : "night";
  const m = modal(`Zonas excluidas · ${cam.name}`, { wide: true });
  let masks = [];
  const stage = h("div", { class: "ops-mask-stage" });
  const img = h("img", { src: `/api/camera-health/${enc(cam.id)}/reference.jpg?kind=${kind}`, alt: "Imagen de referencia", draggable: "false" });
  const layer = h("div", { class: "ops-mask-layer" });
  stage.append(img, layer);
  m.body.append(h("p", {}, "Arrastra para marcar rectángulos que NO se comparan: pantallas, puertas automáticas, el reloj sobreimpreso. " +
    "Haz clic en un rectángulo para quitarlo."), stage);
  const draw = () => {
    clear(layer);
    masks.forEach((poly, i) => {
      const xs = poly.map((p) => p[0]); const ys = poly.map((p) => p[1]);
      const r = h("button", { type: "button", class: "ops-mask-rect", title: "Quitar esta zona",
        onclick: (e) => { e.stopPropagation(); masks.splice(i, 1); draw(); } });
      Object.assign(r.style, { left: `${Math.min(...xs) * 100}%`, top: `${Math.min(...ys) * 100}%`,
        width: `${(Math.max(...xs) - Math.min(...xs)) * 100}%`, height: `${(Math.max(...ys) - Math.min(...ys)) * 100}%` });
      layer.append(r);
    });
  };
  get(`/api/camera-health/${enc(cam.id)}/config`).then((c) => { masks = c.masks || []; draw(); }).catch(() => draw());
  let start = null;
  const pos = (e) => {
    const b = stage.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), Math.min(1, Math.max(0, (e.clientY - b.top) / b.height))];
  };
  stage.addEventListener("pointerdown", (e) => { if (e.target === layer || e.target === img) { start = pos(e); e.preventDefault(); } });
  stage.addEventListener("pointerup", (e) => {
    if (!start) return;
    const end = pos(e);
    if (Math.abs(end[0] - start[0]) > 0.01 && Math.abs(end[1] - start[1]) > 0.01) {
      const [x0, x1] = [Math.min(start[0], end[0]), Math.max(start[0], end[0])];
      const [y0, y1] = [Math.min(start[1], end[1]), Math.max(start[1], end[1])];
      masks.push([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]);
      draw();
    }
    start = null;
  });
  m.foot.append(h("div", { class: "right" }, h("button", { type: "button", class: "btn btn-primary", onclick: async (e) => {
    await busy(e.currentTarget, async () => {
      try {
        await put(`/api/camera-health/${enc(cam.id)}/config`, { enabled: true, masks });
        toast("Zonas excluidas guardadas", "ok");
        m.close();
      } catch (err) { toastError(err, "No se pudieron guardar las zonas"); }
    });
  } }, "Guardar zonas")));
}

// ================================================================== hora
async function renderClock(body) {
  clear(body);
  let data;
  try { data = await get("/api/clock"); } catch (err) { body.append(h("p", { class: "form-error" }, err.message)); return; }
  const pc = data.pc;
  body.append(h("div", { class: "ops-kv" }, h("strong", {}, "Este PC: "),
    pc ? statusPill(pc.status, pc.status === "ok" ? "En hora" : undefined) : statusPill("unknown"), " ",
    h("span", {}, pc ? pc.message_es : "Todavía no se ha comprobado.")));
  if (!data.devices.length) {
    body.append(emptyState({ title: "Aún no se ha medido la hora de los equipos",
      text: "La hora de cada cámara y grabador se compara con la de este PC una vez por hora.",
      action: ui.admin ? { label: "Comprobar ahora", onClick: () => clockCheck(body) } : null }));
    return;
  }
  const devs = await get("/api/devices").catch(() => []);
  const names = Object.fromEntries(devs.map((d) => [d.id, d.name]));
  body.append(h("div", { class: "card table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Equipo"), h("th", { scope: "col" }, "Estado"),
      h("th", { scope: "col" }, "Diferencia"), h("th", { scope: "col" }, "Sincronización"), h("th", { scope: "col" }, "Detalle"))),
    h("tbody", {}, data.devices.map((d) => h("tr", {},
      h("td", {}, names[d.device_id] || d.device_id), h("td", {}, statusPill(d.status)),
      h("td", { class: "mono" }, d.skew_s == null ? "—" : `${d.skew_s > 0 ? "+" : ""}${fmtNum(d.skew_s, 1)} s`),
      h("td", {}, { ntp: "NTP (automática)", manual: "Manual", unknown: "—" }[d.time_mode] || "—"),
      h("td", { class: "small" }, d.message_es)))))));
}

async function clockCheck(body) {
  try {
    await post("/api/clock/check");
    await renderClock(body);
  } catch (err) { toastError(err, "No se pudo comprobar la hora"); }
}

// ================================================================== previsión
function forecastView(fc) {
  const target = fc.target_days;
  const days = fc.forecast_days >= 9999 ? null : fc.forecast_days;
  const pct = days == null ? 100 : Math.min(100, Math.round((days / Math.max(target, 1)) * 100));
  const meter = h("div", { class: "meter", role: "meter", "aria-valuemin": "0", "aria-valuemax": String(target),
    "aria-valuenow": String(days ?? target), "aria-label": "Días de grabación previstos frente al objetivo" },
  h("i", { class: fc.status === "ok" ? "" : "ops-meter-bad" }));
  meter.firstChild.style.width = `${pct}%`;
  return h("div", { class: "ops-forecast" },
    h("div", { class: "ops-big" }, statusPill(fc.status), " ",
      h("strong", {}, days == null ? "Sin datos aún" : `${fmtNum(days)} días`),
      h("span", { class: "muted" }, days == null ? ` · objetivo ${target} días` : ` previstos · objetivo ${target} días`)),
    meter,
    h("p", {}, fc.message_es),
    fc.rgpd_warning ? h("div", { class: "banner bad", role: "note" }, "El objetivo supera los 30 días que permite el art. 22.3 de la LOPDGDD salvo para acreditar un hecho concreto.") : null);
}

async function renderForecast(body) {
  clear(body);
  let fc;
  try { fc = await get("/api/retention-forecast"); } catch (err) { body.append(h("p", { class: "form-error" }, err.message)); return; }
  const view = h("div", {}, forecastView(fc));
  body.append(view);
  if (!ui.admin) return;
  const n = h("input", { class: "input mono", type: "number", min: "0", max: "256", value: "4", "aria-label": "Cámaras que añadir" });
  const mbps = h("input", { class: "input mono", type: "number", min: "0.1", max: "100", step: "0.5", value: "4", "aria-label": "Mbit/s de cada cámara" });
  const out = h("div", { role: "status" });
  body.append(h("div", { class: "card card-pad ops-sim" },
    h("h3", {}, "¿Y si añado cámaras?"),
    h("div", { class: "ops-row" }, h("label", { class: "field" }, h("span", {}, "Cámaras nuevas"), n),
      h("label", { class: "field" }, h("span", {}, "Calidad de cada una (Mbit/s)"), mbps),
      h("button", { type: "button", class: "btn", onclick: async (e) => {
        await busy(e.currentTarget, async () => {
          try {
            const sim = await post("/api/retention-forecast/simulate", { add_cameras: Number(n.value), bitrate_mbps: Number(mbps.value) });
            clear(out).append(forecastView(sim));
          } catch (err) { toastError(err, "No se pudo simular"); }
        });
      } }, "Simular")),
    h("p", { class: "muted small" }, "Orientativo: una cámara de 4 MP suele grabar entre 2 y 6 Mbit/s según la escena y el códec."),
    out));
}

// ================================================================== informe del día
async function renderReport(body, dateInput) {
  const out = body.querySelector(".ops-report-out") || body.appendChild(h("div", { class: "ops-report-out" }));
  clear(out);
  const day = dateInput.value;
  let rep;
  try { rep = await get(`/api/health-report${day ? `?date=${enc(day)}` : ""}`); } catch (err) {
    out.append(h("p", { class: "form-error" }, err.message));
    return;
  }
  out.append(h("div", { class: `banner ${rep.status === "ok" ? "info" : "bad"}`, role: "status" },
    rep.status === "ok" ? "Sin problemas en el día." : `${rep.problems.length} ${rep.problems.length === 1 ? "problema" : "problemas"} en el día:`),
    rep.problems.length ? h("ul", { class: "ops-problems" }, rep.problems.map((x) => h("li", {}, x))) : null);
  if (!rep.cameras.length) {
    out.append(emptyState({ title: "No hay cámaras en el informe", text: "El informe resume cada cámara activa del día." }));
    return;
  }
  out.append(h("div", { class: "card table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Cámara", "Grabado", "Sin grabar", "Peor imagen", "Hora", "Días guardados", "Protegidos"]
      .map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", {}, rep.cameras.map((c) => h("tr", {},
      h("td", {}, c.name), h("td", { class: "mono" }, `${fmtNum(c.online_ratio * 100, 1)} %`),
      h("td", { class: "mono" }, `${fmtNum(c.recording_gaps_min)} min`),
      h("td", {}, c.health_score_min == null ? "—" : `${c.health_score_min}${c.health_causes.length ? ` · ${c.health_causes.map((x) => CAUSE[x] || x).join(", ")}` : ""}`),
      h("td", { class: "mono" }, c.clock_skew_s == null ? "—" : `${fmtNum(c.clock_skew_s, 1)} s`),
      h("td", { class: "mono" }, c.retention_days_real == null ? "—" : fmtNum(c.retention_days_real, 1)),
      h("td", { class: "mono" }, String(c.protected_ranges))))))));
  const disk = rep.disk || {};
  out.append(h("p", { class: "muted small" }, `Disco: ${disk.percent != null ? `${fmtNum(disk.percent, 1)} % usado` : "—"}` +
    `${disk.free_gb != null ? ` · ${fmtNum(disk.free_gb)} GB libres` : ""}` +
    `${disk.smart_ok === 0 ? " · ✖ el disco avisa de fallos (SMART)" : disk.smart_ok === 1 ? " · ✔ SMART correcto" : ""}`));
}

// ================================================================== diagnóstico
async function renderDiag(body) {
  clear(body);
  const devs = await get("/api/devices").catch(() => []);
  if (!devs.length) {
    body.append(emptyState({ title: "No hay equipos", text: "Cuando des de alta equipos podrás comprobar aquí por qué no conectan.",
      action: { label: "Añadir equipo", href: "/" } }));
    return;
  }
  body.append(h("ul", { class: "ops-diag-list" }, devs.map((d) => h("li", {}, h("strong", {}, d.name), " ",
    h("span", { class: "muted small" }, d.host), " ",
    h("button", { type: "button", class: "btn btn-sm", onclick: () => openDiagnosis({ deviceId: d.id, name: d.name }) },
      "¿Por qué no conecta?")))));
}

// ================================================================== arranque
async function main() {
  if (!root) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk) return;
  ui.admin = u.role === "admin";

  const hb = section(root, { id: "h-ops-health", title: "Salud de la imagen",
    subtitle: "Puntuación de 0 a 100 frente a la imagen de referencia de cada cámara" });
  ui.tbody = h("tbody", {});
  hb.append(h("div", { class: "card table-wrap" }, h("table", { class: "table", id: "ops-health-table" },
    h("thead", {}, h("tr", {}, ["Cámara", "Estado", "Puntuación", "Qué pasa", "Referencia"].map((x) => h("th", { scope: "col" }, x)),
      h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Acciones")))), ui.tbody)));
  try { await loadHealth(); } catch (err) { if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Salud de imagen"); }
  onOpsEvent("health", async (ev) => {
    try {
      ui.health.set(ev.camera_id, await get(`/api/camera-health/${enc(ev.camera_id)}`));
      renderHealth();
    } catch { /* cámara fuera del ámbito o borrada */ }
  });

  const clockBody = h("div", {});
  const cb = section(root, { id: "h-ops-clock", title: "Hora de los equipos",
    actions: ui.admin ? [h("button", { type: "button", class: "btn btn-sm", onclick: (e) => busy(e.currentTarget, () => clockCheck(clockBody)) }, "Comprobar ahora")] : [] });
  cb.append(clockBody);
  renderClock(clockBody);

  const fb = section(root, { id: "h-ops-forecast", title: "Días de grabación", subtitle: "Con lo que graban las cámaras de verdad" });
  renderForecast(fb);

  const dateInput = h("input", { class: "input", type: "date", value: localDateValue(), "aria-label": "Día del informe" });
  const csv = h("a", { class: "btn btn-sm", href: "/api/health-report.csv", download: "" }, "Descargar CSV");
  dateInput.addEventListener("change", () => {
    csv.href = `/api/health-report.csv?date=${enc(dateInput.value)}`;
    renderReport(rb, dateInput);
  });
  const rb = section(root, { id: "h-ops-report", title: "Informe de salud del día",
    actions: [dateInput, csv, h("button", { type: "button", class: "btn btn-sm", onclick: () => window.print() }, "Imprimir")] });
  renderReport(rb, dateInput);

  if (ui.admin) {
    const db = section(root, { id: "h-ops-diagnose", title: "¿Por qué no conecta?",
      subtitle: "Revisión paso a paso de un equipo, con un solo intento de contraseña" });
    renderDiag(db);
  }
}

main();
