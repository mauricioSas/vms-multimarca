// Panel de control: equipos, cámaras y asignación de cámaras a los 4 monitores.
import { ApiError, get, post, put, patch, del, enc, subscribeEvents } from "./api.js";
import {
  $, $$, esc, mountShell, toast, toastError, busy, confirmDialog, showFieldErrors, clearFieldErrors,
  errorText,
} from "./ui.js";
import { bindDevices, openDeviceDialog, presetFor, renderDevices, setFormError, vendorLabel } from "./devices.js";


const state = {
  me: null,
  devices: [],
  cameras: [],
  walls: [],
  monitor: 1,
  draft: null,          // WallLayout en edición del monitor seleccionado
  dirty: false,
  editingDevice: null,  // DeviceOut en edición o null (alta)
  testResult: null,     // DeviceTestResult de la última prueba del diálogo
  channelsDevice: null,
  editingCamera: null,
};

const isAdmin = () => state.me && state.me.role === "admin";

// ================================================================== carga de datos
async function loadDevices() {
  if (!isAdmin() && state.me.role !== "operator") return;
  state.devices = await get("/api/devices");
  renderDevices();
}

let camerasSignature = "";
async function loadCameras() {
  state.cameras = await get("/api/cameras");
  renderCameras();
  renderPalette();
  // el editor de monitores solo se repinta si cambian las cámaras (no por cambios de estado),
  // para no cerrar una lista desplegable que el usuario tenga abierta
  const sig = state.cameras.map((c) => `${c.id}:${c.name}`).join("|");
  if (sig !== camerasSignature) {
    camerasSignature = sig;
    renderScreen();
  }
}

async function loadWalls({ keepDraft = false } = {}) {
  state.walls = await get("/api/walls");
  if (!keepDraft || !state.dirty) selectMonitor(state.monitor, { force: true });
}

async function loadAll() {
  await Promise.all([loadDevices(), loadCameras(), loadWalls()]);
}

bindDevices({ state, loadDevices, loadCameras, loadWalls });


// ================================================================== cámaras
function camStatus(c) {
  const live = c.live || {};
  if (!c.enabled) return '<span class="pill off">Desactivada</span>';
  if (live.online === true) return '<span class="pill ok">En vivo</span>';
  if (live.online === false) return '<span class="pill bad">Sin vídeo</span>';
  return '<span class="pill off">Sin datos</span>';
}

function recStatus(c) {
  if (!c.record) return '<span class="pill off">No graba</span>';
  const live = c.live || {};
  if (live.recording) return '<span class="pill ok">Grabando</span>';
  return c.enabled ? '<span class="pill warn">Detenida</span>' : '<span class="pill off">—</span>';
}

function renderCameras() {
  const tbody = $("#cameras-table tbody");
  $("#cameras-count").textContent = state.cameras.length ? `(${state.cameras.length})` : "";
  if (!state.cameras.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty"><strong>Sin cámaras</strong>
      Al añadir un equipo, sus canales se dan de alta aquí.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.cameras.map((c) => `
    <tr data-camera="${esc(c.id)}">
      <td><strong>${esc(c.name)}</strong>
        ${c.live && c.live.codec_warning ? `<div class="small" style="color:var(--warn)">${esc(c.live.codec_warning)}</div>` : ""}</td>
      <td>${esc(c.device_name || "")} <span class="vendor-tag ${esc(c.vendor || "")}">${esc(vendorLabel(c.vendor))}</span></td>
      <td class="mono">${Number(c.channel)}</td>
      <td>${camStatus(c)}</td>
      <td>${recStatus(c)}</td>
      <td><div class="row-actions">
        <a class="btn btn-sm" href="/playback?camera=${enc(c.id)}">Grabaciones</a>
        <button type="button" class="btn btn-sm admin-only" data-act="edit">Editar</button>
        <button type="button" class="btn btn-sm btn-danger admin-only" data-act="delete">Borrar</button>
      </div></td>
    </tr>`).join("");
}

$("#cameras-table").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.camera;
  const cam = state.cameras.find((c) => c.id === id);
  if (!cam) return;
  if (btn.dataset.act === "edit") openCameraDialog(cam);
  else if (btn.dataset.act === "delete") {
    const ok = await confirmDialog("Borrar cámara",
      `Se borrará «${cam.name}» de la configuración, de los monitores y de la analítica. Las grabaciones existentes no se borran.`,
      { okText: "Borrar", danger: true });
    if (!ok) return;
    try {
      await del(`/api/cameras/${enc(id)}`);
      toast(`Cámara «${cam.name}» borrada`, "ok");
      await Promise.all([loadDevices(), loadCameras(), loadWalls({ keepDraft: true })]);
    } catch (err) {
      toastError(err, "No se pudo borrar la cámara");
    }
  }
});

const cameraDialog = $("#camera-dialog");
const cameraForm = $("#camera-form");

let placeholderSeq = 0;
async function updateCameraPlaceholders() {
  const seq = ++placeholderSeq;
  const dev = state.devices.find((d) => d.id === cameraForm.device_id.value);
  // rutas del registro de drivers del servidor (no se repiten presets en JS)
  const preset = dev ? await presetFor(dev.vendor, cameraForm.channel.value, dev.kind) : null;
  if (seq !== placeholderSeq) return;   // llegó una respuesta más nueva
  cameraForm.main_path.placeholder = preset ? preset[0] : "/stream1";
  cameraForm.sub_path.placeholder = preset && preset[1] ? preset[1] : "/stream2";
}

function openCameraDialog(cam = null) {
  state.editingCamera = cam;
  cameraForm.reset();
  clearFieldErrors(cameraForm);
  setFormError($("#camera-form-error"), "");
  $("#camera-dialog-title").textContent = cam ? `Editar «${cam.name}»` : "Añadir cámara manual";
  const sel = cameraForm.device_id;
  sel.innerHTML = state.devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} (${esc(vendorLabel(d.vendor))})</option>`).join("");
  sel.disabled = !!cam;
  if (!state.devices.length) {
    toast("Primero añade un equipo", "bad");
    return;
  }
  if (cam) {
    cameraForm.name.value = cam.name;
    sel.value = cam.device_id;
    cameraForm.channel.value = cam.channel;
    cameraForm.main_path.value = cam.main_path || "";
    cameraForm.sub_path.value = cam.sub_path || "";
    cameraForm.rtsp_transport.value = cam.rtsp_transport || "tcp";
    cameraForm.enabled.checked = !!cam.enabled;
    cameraForm.record.checked = !!cam.record;
    cameraForm.has_sub.checked = !!cam.has_sub;
  }
  updateCameraPlaceholders();
  cameraDialog.showModal();
  cameraForm.name.focus();
}

cameraForm.device_id.addEventListener("change", updateCameraPlaceholders);
cameraForm.channel.addEventListener("input", updateCameraPlaceholders);
$("#btn-add-camera").addEventListener("click", () => openCameraDialog(null));

cameraForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearFieldErrors(cameraForm);
  setFormError($("#camera-form-error"), "");
  const f = cameraForm;
  const body = {
    name: f.name.value.trim(),
    channel: Number(f.channel.value) || 1,
    main_path: f.main_path.value.trim() || null,
    sub_path: f.sub_path.value.trim() || null,
    rtsp_transport: f.rtsp_transport.value,
    enabled: f.enabled.checked,
    record: f.record.checked,
    has_sub: f.has_sub.checked,
  };
  if (!body.name) {
    setFormError($("#camera-form-error"), "Ponle un nombre a la cámara.");
    f.name.focus();
    return;
  }
  await busy($("#btn-camera-save"), async () => {
    try {
      const cam = state.editingCamera;
      if (cam) await patch(`/api/cameras/${enc(cam.id)}`, body);
      else await post("/api/cameras", { ...body, device_id: f.device_id.value });
      toast(`Cámara «${body.name}» guardada`, "ok");
      cameraDialog.close();
      await Promise.all([loadDevices(), loadCameras()]);
    } catch (err) {
      if (!showFieldErrors(cameraForm, err)) setFormError($("#camera-form-error"), errorText(err));
    }
  });
});

// ================================================================== monitores
function renderMonitorTabs() {
  const tabs = $("#monitor-tabs");
  tabs.innerHTML = [1, 2, 3, 4].map((m) => {
    const w = state.walls.find((x) => x.monitor === m);
    const assigned = w ? w.cells.slice(0, w.grid).filter(Boolean).length : 0;
    return `<button type="button" class="monitor-tab" role="tab" id="tab-monitor-${m}" data-monitor="${m}"
      aria-selected="${m === state.monitor}" aria-controls="monitor-panel">
      <span class="num">M${m}</span><span>${esc((w && w.name) || `Monitor ${m}`)}</span>
      <span class="muted small">${assigned}/${w ? w.grid : 4}</span></button>`;
  }).join("");
}

$("#monitor-tabs").addEventListener("click", async (ev) => {
  const tab = ev.target.closest("[data-monitor]");
  if (!tab) return;
  const m = Number(tab.dataset.monitor);
  if (m === state.monitor) return;
  if (state.dirty) {
    const ok = await confirmDialog("Cambios sin guardar",
      `El monitor ${state.monitor} tiene cambios sin guardar. ¿Descartarlos?`, { okText: "Descartar", danger: true });
    if (!ok) return;
  }
  selectMonitor(m, { force: true });
});

function selectMonitor(m, { force = false } = {}) {
  if (!force && m === state.monitor) return;
  state.monitor = m;
  const w = state.walls.find((x) => x.monitor === m) || { monitor: m, name: "", grid: 4, cells: Array(16).fill(null) };
  state.draft = { monitor: m, name: w.name || "", grid: w.grid, cells: [...w.cells] };
  while (state.draft.cells.length < 16) state.draft.cells.push(null);
  setDirty(false);
  $("#wall-name").value = state.draft.name;
  const link = $("#btn-open-wall");
  link.href = `/wall/${m}`;
  link.target = `vms-wall-${m}`;
  renderMonitorTabs();
  renderScreen();
}

function setDirty(v) {
  state.dirty = v;
  $("#wall-dirty").hidden = !v;
}

let paletteSignature = "";
function dotClass(c) {
  return c.live && c.live.online === true ? "on" : c.live && c.live.online === false ? "off" : "";
}

function renderPalette() {
  const list = $("#cam-palette-list");
  if (!state.cameras.length) {
    paletteSignature = "";
    list.innerHTML = '<p class="muted small" style="margin:4px">No hay cámaras todavía.</p>';
    return;
  }
  const sig = state.cameras.map((c) => `${c.id}:${c.name}:${c.device_name || ""}`).join("|");
  if (sig === paletteSignature) {
    // mismas cámaras: solo se actualiza el indicador (no se recrean los elementos, así no se
    // interrumpe un arrastre en curso)
    for (const c of state.cameras) {
      const dot = list.querySelector(`[data-camera="${CSS.escape(c.id)}"] .dot`);
      if (dot) dot.className = `dot ${dotClass(c)}`;
    }
    return;
  }
  paletteSignature = sig;
  list.innerHTML = state.cameras.map((c) => `<div class="cam-chip" draggable="true" data-camera="${esc(c.id)}" title="Arrastra a una celda">
      <span class="dot ${dotClass(c)}"></span><span>${esc(c.name)}</span><small>${esc(c.device_name || "")}</small></div>`).join("");
}

$("#cam-palette-list").addEventListener("dragstart", (ev) => {
  const chip = ev.target.closest("[data-camera]");
  if (!chip) return;
  ev.dataTransfer.setData("text/x-vms-camera", chip.dataset.camera);
  ev.dataTransfer.setData("text/plain", chip.dataset.camera);
  ev.dataTransfer.effectAllowed = "copy";
});

function renderScreen() {
  const d = state.draft;
  if (!d) return;
  const screen = $("#monitor-screen");
  const cols = Math.round(Math.sqrt(d.grid));
  screen.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  screen.style.gridTemplateRows = `repeat(${cols}, minmax(0, 1fr))`;
  $$("#grid-buttons button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.grid) === d.grid)));
  const options = ['<option value="">— Vacía —</option>']
    .concat(state.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`)).join("");
  const html = [];
  for (let i = 0; i < d.grid; i++) {
    const id = d.cells[i];
    const cam = id ? state.cameras.find((c) => c.id === id) : null;
    html.push(`<div class="slot ${cam ? "filled" : ""}" data-slot="${i}">
      <span class="slot-num">${i + 1}</span>
      ${cam ? `<span class="slot-name">${esc(cam.name)}</span>` : ""}
      <label class="sr-only" for="slot-select-${i}">Cámara de la celda ${i + 1}</label>
      <select class="input" id="slot-select-${i}" data-slot-select="${i}">${options}</select>
    </div>`);
  }
  screen.innerHTML = html.join("");
  for (let i = 0; i < d.grid; i++) {
    const sel = screen.querySelector(`[data-slot-select="${i}"]`);
    sel.value = d.cells[i] && state.cameras.some((c) => c.id === d.cells[i]) ? d.cells[i] : "";
  }
}

function assignCell(index, cameraId) {
  const d = state.draft;
  if (!d || index < 0 || index >= 16) return;
  d.cells[index] = cameraId || null;
  setDirty(true);
  renderScreen();
  renderMonitorTabs();
}

const screenEl = $("#monitor-screen");
screenEl.addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-slot-select]");
  if (sel) assignCell(Number(sel.dataset.slotSelect), sel.value);
});
screenEl.addEventListener("dragover", (ev) => {
  const slot = ev.target.closest("[data-slot]");
  if (!slot) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "copy";
  $$(".slot.drop-target", screenEl).forEach((s) => s !== slot && s.classList.remove("drop-target"));
  slot.classList.add("drop-target");
});
screenEl.addEventListener("dragleave", (ev) => {
  const slot = ev.target.closest("[data-slot]");
  if (slot && !slot.contains(ev.relatedTarget)) slot.classList.remove("drop-target");
});
screenEl.addEventListener("drop", (ev) => {
  const slot = ev.target.closest("[data-slot]");
  if (!slot) return;
  ev.preventDefault();
  slot.classList.remove("drop-target");
  const id = ev.dataTransfer.getData("text/x-vms-camera") || ev.dataTransfer.getData("text/plain");
  if (state.cameras.some((c) => c.id === id)) assignCell(Number(slot.dataset.slot), id);
});

$("#grid-buttons").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-grid]");
  if (!b || !state.draft) return;
  state.draft.grid = Number(b.dataset.grid);
  setDirty(true);
  renderScreen();
});
$("#wall-name").addEventListener("input", (ev) => {
  if (!state.draft) return;
  state.draft.name = ev.target.value;
  setDirty(true);
});
$("#btn-clear-wall").addEventListener("click", () => {
  if (!state.draft) return;
  state.draft.cells = Array(16).fill(null);
  setDirty(true);
  renderScreen();
});
$("#btn-save-wall").addEventListener("click", async (ev) => {
  const d = state.draft;
  if (!d) return;
  await busy(ev.currentTarget, async () => {
    try {
      const saved = await put(`/api/walls/${d.monitor}`, { name: d.name.trim(), grid: d.grid, cells: d.cells });
      const i = state.walls.findIndex((w) => w.monitor === d.monitor);
      if (i >= 0) state.walls[i] = saved; else state.walls.push(saved);
      selectMonitor(d.monitor, { force: true });
      toast(`Monitor ${d.monitor} guardado. El muro se actualiza solo.`, "ok");
    } catch (err) {
      toastError(err, "No se pudo guardar el monitor");
    }
  });
});

window.addEventListener("beforeunload", (ev) => {
  if (state.dirty) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

// ================================================================== tiempo real
let refreshTimer = null;
function scheduleRefresh(scope) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    try {
      if (scope === "walls") await loadWalls({ keepDraft: true });
      else if (scope === "devices" || scope === "cameras") await Promise.all([loadDevices(), loadCameras()]);
    } catch (err) {
      console.warn("No se pudo refrescar tras un cambio", err);
    }
  }, 400);
}

function onStatus(data) {
  if (!data || !Array.isArray(data.cameras)) return;
  let changed = false;
  for (const s of data.cameras) {
    const cam = state.cameras.find((c) => c.id === s.camera_id);
    if (!cam) continue;
    cam.live = cam.live || {};
    if (cam.live.online !== s.online || cam.live.recording !== s.recording) {
      cam.live.online = s.online;
      cam.live.recording = s.recording;
      changed = true;
    }
  }
  if (changed) {
    renderCameras();
    renderPalette();
  }
}

// ================================================================== arranque
$$("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
$("#btn-add-device").addEventListener("click", () => openDeviceDialog(null));

async function main() {
  try {
    state.me = await get("/api/auth/me");
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Sin conexión con el servidor");
    return;
  }
  if (state.me.kiosk) {
    location.replace("/wall/1");
    return;
  }
  mountShell("panel", state.me);
  try {
    await loadAll();
  } catch (err) {
    toastError(err, "No se pudieron cargar los datos");
  }
  try {
    const status = await get("/api/status");
    if (status && status.config_warning) {
      const b = $("#config-warning");
      b.textContent = status.config_warning;
      b.hidden = false;
    }
  } catch (err) {
    console.warn("No se pudo leer /api/status", err);
  }
  subscribeEvents({
    config: (d) => d && scheduleRefresh(d.scope),
    status: onStatus,
  });
}

main();
