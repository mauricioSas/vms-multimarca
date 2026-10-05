// #onboarding-root: asistente de primer uso («Primeros pasos») y recorridos con Driver.js. Nunca en los muros.
// Dueño: B6. Contrato: CONTRATO §18.14 (pasos de la investigación §5.2).
//
// El asistente es una tarjeta encima del panel (no un diálogo que lo tape): mientras lo sigues puedes usar los
// botones y diálogos normales del panel. Aparece cuando no hay ningún equipo y el usuario es administrador; se
// puede cerrar y retomar desde «Ayuda» > «Primeros pasos». El progreso se guarda por usuario.
import { get, put, post, enc } from "./api.js";
import { toast, toastError, busy } from "./ui.js";
import { h, clear, me, statusPill, renderDiagnosis, openDiagnosis, fmtNum, onOpsEvent } from "./ops-common.js";
import "./help.js";
import { startTour } from "./tours.js";

const root = document.getElementById("onboarding-root");
let state = null;
let step = 0;

const STEPS = [
  { id: "bienvenida", title: "Bienvenida", render: renderWelcome },
  { id: "buscar", title: "Buscar en la red", render: renderSearch },
  { id: "credenciales", title: "Usuario y contraseña", render: renderCredentials },
  { id: "probar", title: "Probar la conexión", render: renderTest },
  { id: "canales", title: "Elegir canales", render: renderChannels },
  { id: "referencia", title: "Imagen de referencia", render: renderReference },
  { id: "muro", title: "Montar el muro", render: renderWall },
  { id: "grabacion", title: "Días de grabación", render: renderRecording },
  { id: "listo", title: "¡Listo!", render: renderDone },
];

async function saveState(patch) {
  try {
    state = await put("/api/onboarding/state", patch);
  } catch (err) {
    console.warn("No se pudo guardar el progreso del asistente", err);
  }
}

function p(text) { return h("p", {}, text); }

function click(selector) {
  const el = document.querySelector(selector);
  if (el) el.click();
}

async function devices() {
  try { return await get("/api/devices"); } catch { return []; }
}

async function cameras() {
  try { return await get("/api/cameras"); } catch { return []; }
}

// ------------------------------------------------------------------ pasos
function renderWelcome(body) {
  body.append(
    p("En unos 10 minutos vas a ver tus cámaras en los monitores de la tienda y vas a dejar la grabación en marcha."),
    h("ol", { class: "ob-list" },
      h("li", {}, "Buscar las cámaras o el grabador en la red."),
      h("li", {}, "Probar que el usuario y la contraseña funcionan."),
      h("li", {}, "Elegir qué cámaras quieres y fijar su imagen de referencia."),
      h("li", {}, "Ponerlas en los monitores y revisar cuántos días se graban.")),
    p("Mientras sigues los pasos puedes usar el panel con normalidad. Si te pierdes, pulsa «?» en cualquier sección."));
}

function renderSearch(body) {
  body.append(
    p("Un grabador (NVR o DVR) es la caja donde se conectan varias cámaras; una cámara IP va conectada directamente a la red. " +
      "Puedes dar de alta cualquiera de las dos."),
    h("div", { class: "ob-actions" },
      h("button", { type: "button", class: "btn btn-primary", onclick: () => click("#btn-discover") }, "Buscar en la red"),
      h("button", { type: "button", class: "btn", onclick: () => click("#btn-add-device") }, "Escribir la IP a mano")),
    h("p", { class: "muted small" }, "Si un equipo no aparece en la búsqueda, puede tener ONVIF desactivado: añádelo con su IP."));
}

function renderCredentials(body) {
  body.append(
    p("El VMS solo necesita VER el vídeo. Crea en el equipo un usuario de solo lectura para el VMS y no uses «admin»: " +
      "si alguien copiara la contraseña, no podría cambiar nada en tus cámaras."),
    h("details", {}, h("summary", {}, "Cómo crearlo en Hikvision"),
      h("ol", {}, h("li", {}, "Entra en la web del equipo con su IP y el usuario administrador."),
        h("li", {}, "Configuración > Sistema > Gestión de usuarios > Añadir."),
        h("li", {}, "Tipo «Operador» o «Usuario», con permiso de Vista en directo y Reproducción. Contraseña larga."))),
    h("details", {}, h("summary", {}, "Cómo crearlo en Dahua"),
      h("ol", {}, h("li", {}, "Entra en la web del equipo con su IP y el usuario administrador."),
        h("li", {}, "Sistema > Cuenta > Añadir usuario."),
        h("li", {}, "Grupo «user», con permiso de Vista en directo y Reproducción. Contraseña larga."))),
    h("p", { class: "muted small" }, "Ojo: tras 5 intentos fallidos, Hikvision y Dahua bloquean el usuario 30 minutos."));
}

async function renderTest(body) {
  const devs = await devices();
  if (!devs.length) {
    body.append(p("Todavía no hay ningún equipo. Vuelve al paso anterior y añade uno."),
      h("button", { type: "button", class: "btn", onclick: () => click("#btn-add-device") }, "Añadir equipo"));
    return;
  }
  body.append(p("Comprueba cada equipo. Si algo falla, el diagnóstico te dice el motivo en palabras sencillas y qué hacer."));
  const out = h("div", { class: "ob-diag" });
  const list = h("ul", { class: "ob-devices" }, devs.map((d) => h("li", {},
    h("strong", {}, d.name), " ",
    h("button", { type: "button", class: "btn btn-sm", onclick: async (e) => {
      await busy(e.currentTarget, async () => {
        clear(out).append(h("p", { class: "muted" }, "Comprobando…"));
        try {
          clear(out).append(renderDiagnosis(await post("/api/diagnostics/device", { device_id: d.id })));
        } catch (err) {
          clear(out).append(h("p", { class: "form-error" }, err.message));
        }
      });
    } }, "Comprobar"))));
  body.append(list, out);
}

async function renderChannels(body) {
  const devs = await devices();
  const nvrs = devs.filter((d) => d.kind === "nvr" || d.kind === "dvr" || d.kind === "xvr");
  body.append(p("En un grabador, cada cámara es un «canal». Marca solo las que quieras ver y grabar."),
    p("Si el aviso dice que el subflujo va en H.265, cámbialo a H.264 en la cámara: los muros lo muestran mejor."));
  if (nvrs.length) {
    body.append(h("p", {}, "Pulsa «Importar canales» en la fila del grabador, en la lista de equipos."),
      h("button", { type: "button", class: "btn", onclick: () => document.getElementById("sec-devices")?.scrollIntoView({ behavior: "smooth" }) },
        "Ir a Equipos"));
  }
  const cams = await cameras();
  body.append(h("p", { class: "muted small" }, `Ahora mismo hay ${cams.length} ${cams.length === 1 ? "cámara" : "cámaras"} dadas de alta.`));
}

async function renderReference(body) {
  const cams = await cameras();
  body.append(p("La imagen de referencia enseña al sistema cómo debe verse cada cámara: así sabrá avisarte si alguien la tapa, la mueve o se desenfoca. " +
    "Tarda unos 3 minutos por cámara y mejor con la tienda vacía."));
  if (!cams.length) {
    body.append(p("Aún no hay cámaras. Termina los pasos anteriores."));
    return;
  }
  let health = [];
  try { health = await get("/api/camera-health"); } catch { health = []; }
  const byId = Object.fromEntries(health.map((x) => [x.camera_id, x]));
  const list = h("ul", { class: "ob-devices" });
  for (const c of cams) {
    const hasRef = byId[c.id] && byId[c.id].references && byId[c.id].references.day;
    const status = h("span", {}, hasRef ? statusPill("ok", "Referencia fijada") : statusPill("unknown", "Sin referencia"));
    const btn = h("button", { type: "button", class: "btn btn-sm", dataset: { camera: c.id }, onclick: async () => {
      try {
        const r = await post(`/api/camera-health/${enc(c.id)}/reference`, { kind: "day" });
        btn.disabled = true;
        clear(status).append(statusPill("unknown", "Tomando imágenes…"));
        pollJob(r.job_id, status, btn);
      } catch (err) {
        toastError(err, "No se pudo fijar la referencia");
      }
    } }, hasRef ? "Volver a fijar" : "Fijar referencia");
    list.append(h("li", {}, h("strong", {}, c.name), " ", status, " ", btn));
  }
  body.append(list);
}

async function pollJob(jobId, statusNode, btn) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let job;
    try { job = await get(`/api/camera-health/jobs/${enc(jobId)}`); } catch { return; }
    if (job.state === "running") {
      clear(statusNode).append(statusPill("unknown", `Tomando imágenes ${job.frames}/${job.total}…`));
      continue;
    }
    clear(statusNode).append(job.state === "done" ? statusPill(job.person_warning ? "warning" : "ok", job.message_es)
      : statusPill("critical", job.message_es));
    btn.disabled = false;
    btn.textContent = "Volver a fijar";
    return;
  }
}

function renderWall(body) {
  body.append(p("Elige el monitor, cuántas cámaras quieres a la vez (1, 4, 9 o 16) y arrastra cada cámara a una celda. Después pulsa «Guardar monitor»."),
    p("Con «Abrir muro» se abre la pantalla de ese monitor para colocarla en la pantalla de la tienda."),
    h("div", { class: "ob-actions" },
      h("button", { type: "button", class: "btn btn-primary", onclick: () => document.getElementById("sec-monitors")?.scrollIntoView({ behavior: "smooth" }) },
        "Ir a Monitores"),
      h("button", { type: "button", class: "btn", onclick: () => startTour("monitores") }, "Enséñamelo (recorrido)")));
}

async function renderRecording(body) {
  let ret = null;
  let fc = null;
  try { [ret, fc] = await Promise.all([get("/api/settings/retention"), get("/api/retention-forecast")]); } catch (err) {
    body.append(h("p", { class: "form-error" }, err.message));
    return;
  }
  const input = h("input", { class: "input mono ob-days", type: "number", min: "1", max: "3650", value: String(ret.days),
                             "aria-label": "Días que se conservan las grabaciones" });
  const warn = h("p", { class: "ob-warn", role: "note" });
  const updateWarn = () => {
    warn.textContent = Number(input.value) > 30
      ? "Ojo: la ley (art. 22.3 LOPDGDD) obliga a borrar las grabaciones en un mes como máximo, salvo lo que acredite un hecho concreto. Para eso usa los tramos protegidos."
      : "";
  };
  input.addEventListener("input", updateWarn);
  updateWarn();
  body.append(
    p("Las grabaciones se borran solas cuando pasan estos días. Lo normal en una tienda son 30 días como máximo."),
    h("div", { class: "ob-row" }, h("label", {}, "Conservar ", input, " días"),
      h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: async (e) => {
        await busy(e.currentTarget, async () => {
          try {
            await put("/api/settings/retention", { days: Number(input.value), disk_guard_percent: ret.disk_guard_percent });
            toast("Días de grabación guardados", "ok");
          } catch (err) { toastError(err, "No se pudieron guardar los días"); }
        });
      } }, "Guardar")),
    warn,
    h("div", { class: `banner ${fc.status === "ok" ? "info" : "bad"}`, role: "status" }, fc.message_es),
    fc.forecast_days < 9999 ? h("p", { class: "muted small" }, `Previsión con lo que se graba hoy: ${fmtNum(fc.forecast_days)} días.`) : null);
}

function renderDone(body) {
  body.append(p("Ya está todo en marcha: las cámaras se graban y el sistema vigila su imagen, su hora y el disco."),
    p("Te recomendamos dar de alta también los avisos por correo o webhook en «Estado del sistema» > «Avisos»."),
    h("div", { class: "ob-actions" },
      h("button", { type: "button", class: "btn btn-primary", onclick: async () => {
        await saveState({ wizard_completed: true, wizard_step: STEPS.length - 1 });
        startTour("reproduccion");
      } }, "Sí, recorrido de 1 minuto por la reproducción"),
      h("button", { type: "button", class: "btn", onclick: async () => {
        await saveState({ wizard_completed: true });
        hideWizard();
      } }, "Terminar")));
}

// ------------------------------------------------------------------ marco del asistente
function hideWizard() {
  clear(root);
  root.hidden = true;
}

async function showWizard(at = state?.wizard_step || 0) {
  step = Math.max(0, Math.min(STEPS.length - 1, at));
  root.hidden = false;
  clear(root);
  const s = STEPS[step];
  const body = h("div", { class: "ob-body" });
  const progress = h("ol", { class: "ob-progress", "aria-label": "Pasos" }, STEPS.map((x, i) =>
    h("li", { class: i === step ? "current" : i < step ? "done" : "", "aria-current": i === step ? "step" : null },
      h("span", { class: "sr-only" }, i < step ? "Hecho: " : ""), x.title)));
  const card = h("section", { class: "card ob-card", "aria-labelledby": "ob-title", dataset: { step: s.id } },
    h("div", { class: "ob-head" },
      h("div", {}, h("div", { class: "kicker" }, `Primeros pasos · paso ${step + 1} de ${STEPS.length}`),
        h("h2", { id: "ob-title", tabindex: "-1" }, s.title)),
      h("div", { class: "ob-head-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => { await saveState({ wizard_step: step }); hideWizard(); } },
          "Cerrar"),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: async () => { await saveState({ wizard_completed: true }); hideWizard(); } },
          "No volver a mostrar"))),
    progress, body,
    h("div", { class: "ob-foot" },
      h("button", { type: "button", class: "btn", id: "ob-prev", disabled: step === 0, onclick: () => go(step - 1) }, "Anterior"),
      step < STEPS.length - 1
        ? h("button", { type: "button", class: "btn btn-primary", id: "ob-next", onclick: () => go(step + 1) }, "Siguiente")
        : null));
  root.append(card);
  await s.render(body);
}

async function go(i) {
  await saveState({ wizard_step: i });
  await showWizard(i);
  root.querySelector("#ob-title")?.focus?.();
}

// ------------------------------------------------------------------ «¿Por qué no conecta?» en la lista de equipos
function enhanceDeviceRows() {
  const tbody = document.querySelector("#devices-table tbody");
  if (!tbody) return;
  const add = () => {
    for (const tr of tbody.querySelectorAll("tr[data-device]")) {
      const actions = tr.querySelector(".row-actions");
      if (!actions || actions.querySelector("[data-ops='diagnose']")) continue;
      const name = tr.querySelector("td strong")?.textContent || "";
      actions.prepend(h("button", { type: "button", class: "btn btn-sm", dataset: { ops: "diagnose" },
        onclick: () => openDiagnosis({ deviceId: tr.dataset.device, name }) }, "¿Por qué no conecta?"));
    }
  };
  add();
  new MutationObserver(add).observe(tbody, { childList: true });
}

// ------------------------------------------------------------------ avisos (notice) en el panel, nunca en los muros
function noticeToasts() {
  onOpsEvent("notice", (n) => {
    const kind = n.severity === "critical" ? "bad" : n.severity === "warning" ? "info" : "ok";
    toast(n.count > 1 ? `${n.title_es} (${n.count})` : n.title_es, kind, 8000);
  });
}

async function main() {
  if (!root || location.pathname.startsWith("/wall")) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk) return;
  noticeToasts();
  if (u.role !== "admin") return;
  enhanceDeviceRows();
  try {
    state = await get("/api/onboarding/state");
  } catch (err) {
    console.warn("Estado del asistente no disponible", err);
    return;
  }
  document.addEventListener("vms:open-wizard", () => showWizard(state?.wizard_step || 0));
  if (state.show_wizard) await showWizard(state.wizard_step || 0);
}

window.__vmsOnboarding = { showWizard, hideWizard, get step() { return step; } };
main();
