// Panel de control: equipos, cámaras y asignación de cámaras a los 4 monitores.
import { ApiError, get, post, put, patch, del, enc, subscribeEvents } from "./api.js";
import {
  $, $$, esc, mountShell, toast, toastError, busy, confirmDialog, showFieldErrors, clearFieldErrors,
  errorText, VENDOR_LABELS,
} from "./ui.js";

// Rutas por canal de cada fabricante (mismo criterio que vms/core/rtsp.py).
export function presetPaths(vendor, channel) {
  const n = Math.max(1, Math.min(512, Number(channel) || 1));
  if (vendor === "hikvision") return [`/Streaming/Channels/${n}01`, `/Streaming/Channels/${n}02`];
  if (vendor === "dahua") return [`/cam/realmonitor?channel=${n}&subtype=0`, `/cam/realmonitor?channel=${n}&subtype=1`];
  return null;
}

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

// ================================================================== equipos
function deviceStatus(d) {
  if (!d.enabled) return '<span class="pill off">Desactivado</span>';
  if (d.online === true) return '<span class="pill ok">En línea</span>';
  if (d.online === false) return '<span class="pill bad">Sin conexión</span>';
  return '<span class="pill off">Sin datos</span>';
}

function renderDevices() {
  const tbody = $("#devices-table tbody");
  $("#devices-count").textContent = state.devices.length ? `(${state.devices.length})` : "";
  if (!state.devices.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty"><strong>Todavía no hay equipos</strong>
      Añade un grabador o una cámara con «Añadir equipo», o usa «Buscar en la red».</td></tr>`;
    return;
  }
  tbody.innerHTML = state.devices.map((d) => `
    <tr data-device="${esc(d.id)}">
      <td><strong>${esc(d.name)}</strong>${d.model ? `<div class="muted small">${esc(d.model)}</div>` : ""}</td>
      <td><span class="vendor-tag ${esc(d.vendor)}">${esc(VENDOR_LABELS[d.vendor] || d.vendor)}</span></td>
      <td class="mono">${esc(d.host)}<span class="muted">:${esc(d.http_port)}</span></td>
      <td>${d.kind === "nvr" ? "Grabador" : "Cámara IP"}</td>
      <td>${(d.cameras || []).length}</td>
      <td>${deviceStatus(d)}</td>
      <td><div class="row-actions admin-only">
        <button type="button" class="btn btn-sm" data-act="test">Probar</button>
        ${d.kind === "nvr" || d.vendor === "onvif" ? '<button type="button" class="btn btn-sm" data-act="channels">Importar canales</button>' : ""}
        <button type="button" class="btn btn-sm" data-act="edit">Editar</button>
        <button type="button" class="btn btn-sm btn-danger" data-act="delete">Borrar</button>
      </div></td>
    </tr>`).join("");
}

$("#devices-table").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.device;
  const dev = state.devices.find((d) => d.id === id);
  if (!dev) return;
  const act = btn.dataset.act;
  if (act === "edit") openDeviceDialog(dev);
  else if (act === "channels") openChannelsDialog(dev);
  else if (act === "test") {
    await busy(btn, async () => {
      try {
        const r = await post(`/api/devices/${enc(id)}/test`);
        toast(r.ok ? `«${dev.name}»: ${r.message || "conexión correcta"}` : `«${dev.name}»: ${r.message || "falló la prueba"}`,
          r.ok ? "ok" : "bad", 7000);
      } catch (err) {
        toastError(err, "No se pudo probar el equipo");
      }
    });
  } else if (act === "delete") {
    const n = (dev.cameras || []).length;
    const ok = await confirmDialog("Borrar equipo",
      `Se borrará «${dev.name}»${n ? ` y sus ${n} cámaras (también de los monitores y de la analítica)` : ""}. ` +
      "Las grabaciones existentes no se borran.", { okText: "Borrar", danger: true });
    if (!ok) return;
    try {
      await del(`/api/devices/${enc(id)}`);
      toast(`Equipo «${dev.name}» borrado`, "ok");
      await Promise.all([loadDevices(), loadCameras(), loadWalls({ keepDraft: true })]);
    } catch (err) {
      toastError(err, "No se pudo borrar el equipo");
    }
  }
});

// ------------------------------------------------------------------ diálogo de equipo
const deviceDialog = $("#device-dialog");
const deviceForm = $("#device-form");

function setFormError(el, text) {
  el.textContent = text || "";
  el.hidden = !text;
}

function updatePathsPreview() {
  const vendor = deviceForm.vendor.value;
  const kind = deviceForm.kind.value;
  const box = $("#paths-preview");
  const preset1 = presetPaths(vendor, 1);
  const manual = vendor === "generic" && !state.editingDevice;
  $$("[data-manual-path]", deviceForm).forEach((el) => { el.hidden = !manual; });
  if (preset1) {
    const preset2 = presetPaths(vendor, 2);
    box.innerHTML = `<dl style="margin:0">
      <dt>Canal 1 · principal (se graba)</dt><dd>${esc(preset1[0])}</dd>
      <dt>Canal 1 · subflujo (vista en vivo y analítica)</dt><dd>${esc(preset1[1])}</dd>
      ${kind === "nvr" ? `<dt>Canal 2 · principal</dt><dd>${esc(preset2[0])}</dd>` : ""}
    </dl><small class="muted">Se rellenan solas según el canal. Puedes cambiarlas por cámara si el equipo usa otras.</small>`;
  } else if (vendor === "onvif") {
    box.innerHTML = `<span class="muted">Las rutas se piden al equipo por ONVIF (GetStreamUri) al probar la conexión o
      al importar canales. Pulsa «Probar conexión» para verlas.</span>`;
  } else {
    box.innerHTML = `<span class="muted">Escribe la ruta RTSP tal como va después del puerto, por ejemplo
      <span class="mono">/stream1</span>. Sin «rtsp://», sin IP y sin usuario.</span>`;
  }
}

function deviceFormBody({ forTest = false } = {}) {
  const f = deviceForm;
  const body = {
    name: f.name.value.trim() || (forTest ? "prueba" : ""),
    vendor: f.vendor.value,
    kind: f.kind.value,
    host: f.host.value.trim(),
    http_port: Number(f.http_port.value) || 80,
    rtsp_port: Number(f.rtsp_port.value) || 554,
    onvif_port: f.onvif_port.value ? Number(f.onvif_port.value) : null,
    https: f.https.checked,
    username: f.username.value.trim(),
    notes: f.notes.value,
  };
  const pw = f.password.value;
  if (pw) body.password = pw;
  return body;
}

function openDeviceDialog(dev = null, prefill = {}) {
  state.editingDevice = dev;
  state.testResult = null;
  deviceForm.reset();
  clearFieldErrors(deviceForm);
  setFormError($("#device-form-error"), "");
  const res = $("#device-test-result");
  res.hidden = true;
  res.innerHTML = "";
  $("#device-dialog-title").textContent = dev ? `Editar «${dev.name}»` : "Añadir equipo";
  const src = dev || prefill;
  const f = deviceForm;
  if (src.name) f.name.value = src.name;
  if (src.vendor) f.vendor.value = src.vendor;
  if (src.kind) f.kind.value = src.kind;
  if (src.host) f.host.value = src.host;
  if (src.http_port) f.http_port.value = src.http_port;
  if (src.rtsp_port) f.rtsp_port.value = src.rtsp_port;
  if (src.onvif_port) f.onvif_port.value = src.onvif_port;
  if (src.https) f.https.checked = true;
  if (src.username !== undefined) f.username.value = src.username;
  if (src.notes) f.notes.value = src.notes;
  $("#dev-password-hint").textContent = dev
    ? (dev.has_password
      ? "Hay una contraseña guardada. Déjalo vacío para no cambiarla (si cambias la IP o los puertos, vuelve a escribirla)."
      : "Este equipo no tiene contraseña guardada.")
    : "Se guarda cifrada en este equipo; nunca se muestra.";
  $("#dev-clear-pw-wrap").hidden = !(dev && dev.has_password);
  $("#btn-device-save").textContent = dev ? "Guardar cambios" : "Guardar equipo";
  updatePathsPreview();
  deviceDialog.showModal();
  f.name.focus();
}

deviceForm.vendor.addEventListener("change", () => {
  const v = deviceForm.vendor.value;
  // puertos típicos por fabricante (solo si el usuario no los cambió)
  if (v === "onvif" && !deviceForm.onvif_port.value) deviceForm.onvif_port.placeholder = "80 (o 8000/8899)";
  updatePathsPreview();
});
deviceForm.kind.addEventListener("change", updatePathsPreview);

function renderTestResult(r) {
  const box = $("#device-test-result");
  box.hidden = false;
  box.className = `result-box ${r.ok ? "ok" : "bad"}`;
  const info = r.info || {};
  const parts = [];
  parts.push(`<strong>${r.ok ? "Conexión correcta" : "La prueba falló"}</strong>`);
  if (r.message) parts.push(`<div>${esc(r.message)}</div>`);
  const checks = [
    ["Equipo accesible", r.reachable],
    ["Usuario y contraseña", r.auth_ok],
    ["Vídeo RTSP", r.rtsp_ok],
  ].map(([label, v]) => `<span class="pill ${v === true ? "ok" : v === false ? "bad" : "off"}">${label}</span>`).join(" ");
  parts.push(`<div style="margin:8px 0;display:flex;gap:6px;flex-wrap:wrap">${checks}</div>`);
  if (info.model || info.serial) {
    parts.push(`<div class="small muted">${esc(VENDOR_LABELS[info.vendor] || info.vendor || "")} ${esc(info.model || "")}
      ${info.serial ? `· n.º de serie <span class="mono">${esc(info.serial)}</span>` : ""}
      ${info.firmware ? `· firmware ${esc(info.firmware)}` : ""}</div>`);
  }
  const chans = r.channels || [];
  if (chans.length && !state.editingDevice) {
    parts.push(`<div style="margin-top:10px"><strong>Canales a dar de alta como cámaras</strong></div>
      <div class="channel-list" id="test-channels">${chans.map((c) => channelItem(c, false, c.online !== false)).join("")}</div>`);
  }
  box.innerHTML = parts.join("");
}

function channelItem(c, already, checked) {
  const codec = c.sub_codec && c.sub_codec !== "H.264"
    ? `<div class="meta" style="color:var(--warn)">Subflujo ${esc(c.sub_codec)}: cámbialo a H.264 en el equipo para verlo en el navegador</div>` : "";
  const meta = [c.main_resolution, c.main_codec, c.ip_address].filter(Boolean).map(esc).join(" · ");
  return `<label class="channel-item">
    <input type="checkbox" value="${Number(c.channel)}" ${checked && !already ? "checked" : ""} ${already ? "disabled" : ""}>
    <span><strong>${Number(c.channel)}. ${esc(c.name || `Canal ${c.channel}`)}</strong>
      <div class="meta">${already ? "Ya añadido · " : ""}${c.online === false ? "Sin vídeo · " : ""}${meta}</div>${codec}</span>
  </label>`;
}

$("#btn-device-test").addEventListener("click", async (ev) => {
  const body = deviceFormBody({ forTest: true });
  setFormError($("#device-form-error"), "");
  if (!body.host) {
    setFormError($("#device-form-error"), "Escribe la IP o el nombre del equipo para probarlo.");
    deviceForm.host.focus();
    return;
  }
  await busy(ev.currentTarget, async () => {
    try {
      // en edición sin contraseña nueva, se prueba con la guardada
      const dev = state.editingDevice;
      const r = dev && !body.password ? await post(`/api/devices/${enc(dev.id)}/test`) : await post("/api/devices/test", body);
      state.testResult = r;
      renderTestResult(r);
    } catch (err) {
      if (!showFieldErrors(deviceForm, err)) setFormError($("#device-form-error"), errorText(err));
    }
  });
});

deviceForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearFieldErrors(deviceForm);
  setFormError($("#device-form-error"), "");
  const body = deviceFormBody();
  if (!body.name) {
    setFormError($("#device-form-error"), "Ponle un nombre al equipo.");
    deviceForm.name.focus();
    return;
  }
  if (!body.host) {
    setFormError($("#device-form-error"), "Escribe la IP o el nombre del equipo.");
    deviceForm.host.focus();
    return;
  }
  const saveBtn = $("#btn-device-save");
  await busy(saveBtn, async () => {
    try {
      const dev = state.editingDevice;
      if (dev) {
        const upd = { ...body };
        if (!upd.password) upd.password = $("#dev-clear-pw").checked ? "" : null;
        await patch(`/api/devices/${enc(dev.id)}`, upd);
        toast(`Equipo «${body.name}» actualizado`, "ok");
      } else {
        const manual = body.vendor === "generic";
        const testChans = $$("#test-channels input:checked").map((i) => Number(i.value));
        if (!manual) {
          if (state.testResult && (state.testResult.channels || []).length) body.import_channels = testChans;
          else body.import_channels = body.kind === "camera" ? [1] : "all";
        }
        const created = await post("/api/devices", body);
        const importError = created && (created.import_error || (created.details && created.details.import_error));
        if (manual) {
          const mainPath = deviceForm.main_path.value.trim();
          const subPath = deviceForm.sub_path.value.trim();
          if (mainPath) {
            await post("/api/cameras", {
              name: body.name, device_id: created.id, channel: 1,
              main_path: mainPath, sub_path: subPath || null, has_sub: !!subPath,
            });
          }
        }
        const nCams = created && created.cameras ? created.cameras.length : 0;
        toast(`Equipo «${body.name}» añadido${nCams ? ` con ${nCams} ${nCams === 1 ? "cámara" : "cámaras"}` : ""}`, "ok");
        if (importError) toast(`No se pudieron importar los canales: ${importError}`, "bad", 9000);
      }
      deviceDialog.close();
      await Promise.all([loadDevices(), loadCameras()]);
    } catch (err) {
      if (!showFieldErrors(deviceForm, err)) setFormError($("#device-form-error"), errorText(err));
    }
  });
});

// ------------------------------------------------------------------ importar canales
const channelsDialog = $("#channels-dialog");

async function openChannelsDialog(dev) {
  state.channelsDevice = dev;
  $("#channels-title").textContent = `Importar canales de «${dev.name}»`;
  $("#channels-intro").textContent = "Consultando los canales del equipo…";
  $("#channels-list").innerHTML = "";
  $("#btn-channels-import").disabled = true;
  channelsDialog.showModal();
  try {
    const chans = await get(`/api/devices/${enc(dev.id)}/channels`, { timeoutMs: 45000 });
    const existing = new Set(state.cameras.filter((c) => c.device_id === dev.id).map((c) => c.channel));
    $("#channels-intro").textContent = chans.length
      ? `${chans.length} canales encontrados. Marca los que quieras dar de alta como cámaras.`
      : "El equipo no informó de ningún canal.";
    $("#channels-list").innerHTML = chans.map((c) => channelItem(c, existing.has(c.channel), c.online !== false)).join("");
    $("#btn-channels-import").disabled = !chans.length;
  } catch (err) {
    $("#channels-intro").textContent = `No se pudieron leer los canales: ${errorText(err)}`;
  }
}

$("#channels-all").addEventListener("click", () => $$("#channels-list input:not(:disabled)").forEach((i) => { i.checked = true; }));
$("#channels-none").addEventListener("click", () => $$("#channels-list input").forEach((i) => { i.checked = false; }));
$("#btn-channels-import").addEventListener("click", async (ev) => {
  const dev = state.channelsDevice;
  const channels = $$("#channels-list input:checked:not(:disabled)").map((i) => Number(i.value));
  if (!channels.length) {
    toast("Marca al menos un canal", "bad");
    return;
  }
  await busy(ev.currentTarget, async () => {
    try {
      const created = await post(`/api/devices/${enc(dev.id)}/channels/import`, { channels }, { timeoutMs: 45000 });
      toast(`${created.length} ${created.length === 1 ? "cámara añadida" : "cámaras añadidas"}`, "ok");
      channelsDialog.close();
      await Promise.all([loadDevices(), loadCameras()]);
    } catch (err) {
      toastError(err, "No se pudieron importar los canales");
    }
  });
});

// ------------------------------------------------------------------ búsqueda en la red
const discoverDialog = $("#discover-dialog");
$("#btn-discover").addEventListener("click", () => discoverDialog.showModal());
$("#btn-discover-run").addEventListener("click", async (ev) => {
  const tbody = $("#discover-table tbody");
  const timeout = Number($("#discover-timeout").value) || 3;
  tbody.innerHTML = `<tr><td colspan="5" class="empty">Buscando durante ${timeout} s…</td></tr>`;
  await busy(ev.currentTarget, async () => {
    try {
      const res = await post("/api/discovery/scan", { timeout_s: timeout }, { timeoutMs: (timeout + 15) * 1000 });
      const list = res.devices || [];
      if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty"><strong>No se encontró ningún equipo</strong>
          Comprueba que el PC está en la misma red y que el equipo tiene ONVIF activado.</td></tr>`;
        return;
      }
      tbody.innerHTML = list.map((d, i) => `<tr>
        <td class="mono">${esc(d.host)}:${esc(d.http_port)}</td>
        <td><span class="vendor-tag ${esc(d.vendor_guess)}">${esc(VENDOR_LABELS[d.vendor_guess] || d.vendor_guess)}</span></td>
        <td>${esc(d.model || "—")}</td><td>${esc(d.name || "—")}</td>
        <td>${d.already_added ? '<span class="pill ok">Ya añadido</span>'
          : `<button type="button" class="btn btn-sm btn-primary" data-add="${i}">Añadir</button>`}</td></tr>`).join("");
      tbody.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => {
        const d = list[Number(b.dataset.add)];
        discoverDialog.close();
        openDeviceDialog(null, {
          name: d.name || d.model || d.host, vendor: d.vendor_guess, host: d.host, http_port: d.http_port,
          kind: /nvr|dvr|xvr/i.test(`${d.model} ${d.name}`) ? "nvr" : "camera",
        });
      }));
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">${esc(errorText(err))}</td></tr>`;
    }
  });
});

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
      <td>${esc(c.device_name || "")} <span class="vendor-tag ${esc(c.vendor || "")}">${esc(VENDOR_LABELS[c.vendor] || c.vendor || "")}</span></td>
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

function updateCameraPlaceholders() {
  const dev = state.devices.find((d) => d.id === cameraForm.device_id.value);
  const preset = dev ? presetPaths(dev.vendor, cameraForm.channel.value) : null;
  cameraForm.main_path.placeholder = preset ? preset[0] : "/stream1";
  cameraForm.sub_path.placeholder = preset ? preset[1] : "/stream2";
}

function openCameraDialog(cam = null) {
  state.editingCamera = cam;
  cameraForm.reset();
  clearFieldErrors(cameraForm);
  setFormError($("#camera-form-error"), "");
  $("#camera-dialog-title").textContent = cam ? `Editar «${cam.name}»` : "Añadir cámara manual";
  const sel = cameraForm.device_id;
  sel.innerHTML = state.devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} (${esc(VENDOR_LABELS[d.vendor] || d.vendor)})</option>`).join("");
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
