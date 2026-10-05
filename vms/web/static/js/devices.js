// Equipos: lista, alta y edición, prueba de conexión, importar canales, «Corregir códec», búsqueda en la red
// y cambios de IP. Dueño: B5.
//
// Las marcas, sus puertos, avisos, madurez y rutas RTSP salen del registro de drivers del servidor
// (GET /api/vendors y /api/vendors/{id}/paths): aquí no se repite ningún preset (PLAN-V2 §3.1, CONTRATO §16.4).
// El formulario de alta usa #device-form-root para la información de la marca elegida.
import { get, post, patch, del, enc } from "./api.js";
import {
  $, $$, esc, toast, toastError, busy, confirmDialog, showFieldErrors, clearFieldErrors,
  errorText, VENDOR_LABELS,
} from "./ui.js";

// Contexto compartido con panel.js: { state, loadDevices, loadCameras, loadWalls }.
let ctx = null;
export function bindDevices(context) {
  ctx = context;
}

// ================================================================== registro de marcas (servidor)
const vendors = { list: [], byId: {}, loaded: false, failed: false, pending: null };
const MATURITY = {
  verified: { text: "Verificado con hardware", pill: "ok" },
  fixtures: { text: "Probado con respuestas reales", pill: "ok" },
  community: { text: "Según documentación pública", pill: "warn" },
  experimental: { text: "Experimental", pill: "bad" },
};
const KIND_LABELS = { nvr: "Grabador (NVR)", dvr: "Grabador DVR / híbrido", xvr: "Grabador XVR", camera: "Cámara IP" };
const MULTI_KINDS = new Set(["nvr", "dvr", "xvr"]);

/** Nombre para mostrar de una marca (del registro; si aún no ha cargado, la etiqueta básica o el id). */
export function vendorLabel(id) {
  return (vendors.byId[id] && vendors.byId[id].name) || VENDOR_LABELS[id] || id || "";
}

export function loadVendors({ retry = false } = {}) {
  if (vendors.loaded) return Promise.resolve(vendors.list);
  if (vendors.failed && !retry) return Promise.resolve([]);
  if (vendors.pending) return vendors.pending;
  vendors.pending = get("/api/vendors").then((list) => {
    vendors.list = list;
    vendors.byId = Object.fromEntries(list.map((v) => [v.id, v]));
    vendors.loaded = true;
    fillVendorSelect();
    if (deviceDialog.open) onVendorChange({ keepPorts: true });
    return list;
  }).catch(() => {
    vendors.failed = true;    // sin reintentos en bucle: se vuelve a pedir al abrir el diálogo de alta
    vendors.pending = null;
    return [];
  });
  return vendors.pending;
}

function fillVendorSelect() {
  const sel = deviceForm.vendor;
  const current = sel.value;
  const groups = [
    ["Con API (modelo, canales y estado)", (v) => v.capabilities.includes("api_channels") && v.id !== "onvif"],
    ["Perfiles por marca (RTSP y ONVIF)", (v) => isProfile(v)],
    ["Otras marcas", (v) => v.id === "onvif" || v.id === "generic"],
  ];
  sel.innerHTML = groups.map(([label, test]) => {
    const items = vendors.list.filter(test);
    if (!items.length) return "";
    return `<optgroup label="${esc(label)}">${items.map((v) =>
      `<option value="${esc(v.id)}">${esc(v.name)}${v.maturity === "experimental" ? " (experimental)" : ""}</option>`).join("")}</optgroup>`;
  }).join("");
  if (current && vendors.byId[current]) sel.value = current;
}

function isProfile(v) {
  return v.id !== "onvif" && v.id !== "generic" && !v.capabilities.includes("api_channels");
}

// ================================================================== equipos
function deviceStatus(d) {
  if (!d.enabled) return '<span class="pill off">Desactivado</span>';
  if (d.online === true) return '<span class="pill ok">En línea</span>';
  if (d.online === false) return '<span class="pill bad">Sin conexión</span>';
  return '<span class="pill off">Sin datos</span>';
}

export function renderDevices() {
  const tbody = $("#devices-table tbody");
  $("#devices-count").textContent = ctx.state.devices.length ? `(${ctx.state.devices.length})` : "";
  // una sola carga del registro; si falla (servidor antiguo), el formulario sigue con las marcas básicas
  if (ctx.state.me && ctx.state.me.role === "admin" && !vendors.loaded && !vendors.pending && !vendors.failed) {
    loadVendors().then(() => { if (vendors.loaded) renderDevices(); });
  }
  if (!ctx.state.devices.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty"><strong>Todavía no hay equipos</strong>
      Añade un grabador o una cámara con «Añadir equipo», o usa «Buscar en la red».</td></tr>`;
    return;
  }
  tbody.innerHTML = ctx.state.devices.map((d) => `
    <tr data-device="${esc(d.id)}">
      <td><strong>${esc(d.name)}</strong>${d.model ? `<div class="muted small">${esc(d.model)}</div>` : ""}</td>
      <td><span class="vendor-tag ${esc(d.vendor)}">${esc(vendorLabel(d.vendor))}</span></td>
      <td class="mono">${esc(d.host)}<span class="muted">:${esc(d.http_port)}</span></td>
      <td>${MULTI_KINDS.has(d.kind) ? "Grabador" : "Cámara IP"}</td>
      <td>${(d.cameras || []).length}</td>
      <td>${deviceStatus(d)}</td>
      <td><div class="row-actions admin-only">
        <button type="button" class="btn btn-sm" data-act="test">Probar</button>
        ${MULTI_KINDS.has(d.kind) || hasApi(d.vendor) ? '<button type="button" class="btn btn-sm" data-act="channels">Canales</button>' : ""}
        <button type="button" class="btn btn-sm" data-act="edit">Editar</button>
        <button type="button" class="btn btn-sm btn-danger" data-act="delete">Borrar</button>
      </div></td>
    </tr>`).join("");
}

function hasApi(vendorId) {
  const v = vendors.byId[vendorId];
  if (!v) return vendorId === "onvif";
  return v.capabilities.includes("api_channels") || v.capabilities.includes("onvif");
}

$("#devices-table").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.device;
  const dev = ctx.state.devices.find((d) => d.id === id);
  if (!dev) return;
  const act = btn.dataset.act;
  if (act === "edit") openDeviceDialog(dev);
  else if (act === "channels") openChannelsDialog(dev);
  else if (act === "test") {
    await busy(btn, async () => {
      try {
        const r = await post(`/api/devices/${enc(id)}/test`);
        const extra = (r.warnings || []).length ? ` ${r.warnings[0]}` : "";
        toast(r.ok ? `«${dev.name}»: ${r.message || "conexión correcta"}${extra}` : `«${dev.name}»: ${r.message || "falló la prueba"}`,
          r.ok ? "ok" : "bad", 8000);
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
      await Promise.all([ctx.loadDevices(), ctx.loadCameras(), ctx.loadWalls({ keepDraft: true })]);
    } catch (err) {
      toastError(err, "No se pudo borrar el equipo");
    }
  }
});

// ------------------------------------------------------------------ diálogo de equipo
const deviceDialog = $("#device-dialog");
const deviceForm = $("#device-form");
const formRoot = $("#device-form-root");
let portsTouched = false;
["http_port", "rtsp_port", "onvif_port"].forEach((n) => deviceForm[n].addEventListener("input", () => { portsTouched = true; }));

export function setFormError(el, text) {
  el.textContent = text || "";
  el.hidden = !text;
}

function selectedVendor() {
  return vendors.byId[deviceForm.vendor.value] || null;
}

function fillKinds(v) {
  const sel = deviceForm.kind;
  const current = sel.value;
  const kinds = v ? v.kinds : ["nvr", "camera"];
  const order = ["nvr", "dvr", "xvr", "camera"];
  sel.innerHTML = order.filter((k) => kinds.includes(k)).map((k) => `<option value="${k}">${esc(KIND_LABELS[k])}</option>`).join("");
  if (kinds.includes(current)) sel.value = current;
}

function renderVendorInfo(v) {
  if (!v) {
    formRoot.hidden = true;
    formRoot.innerHTML = "";
    return;
  }
  const m = MATURITY[v.maturity] || MATURITY.experimental;
  const icon = v.maturity === "verified" || v.maturity === "fixtures" ? "✔" : v.maturity === "experimental" ? "⚠" : "ⓘ";
  const list = (items) => items.map((t) => `<li>${esc(t)}</li>`).join("");
  const lock = v.lockout ? `<li>Tras ${v.lockout.attempts} intentos fallidos el equipo bloquea el usuario ${v.lockout.minutes} minutos: la prueba manda la contraseña una sola vez.</li>` : "";
  const editing = ctx.state.editingDevice;
  formRoot.innerHTML = `
    <div class="result-box" style="margin:0 0 12px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <strong>${esc(v.name)}</strong>
        <span class="pill ${m.pill}" data-maturity="${esc(v.maturity)}"><span aria-hidden="true">${icon}</span> ${esc(m.text)}</span>
        ${v.brands.length > 1 ? `<span class="muted small">También: ${esc(v.brands.slice(1).join(", "))}</span>` : ""}
      </div>
      ${v.setup_hints_es.length ? `<div class="small" style="margin-top:8px"><strong>Antes de empezar</strong><ul style="margin:4px 0 0 18px;padding:0">${list(v.setup_hints_es)}</ul></div>` : ""}
      ${v.notes_es.length || lock ? `<div class="small muted" style="margin-top:8px"><ul style="margin:0 0 0 18px;padding:0">${list(v.notes_es)}${lock}</ul></div>` : ""}
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:10px">
        <label class="check small"><input type="checkbox" name="allow_basic" id="dev-allow-basic" ${editing && editing.allow_basic ? "checked" : ""}>
          Permitir autenticación Basic (la contraseña viaja sin cifrar; solo si el equipo no admite Digest)</label>
        <label class="check small"><input type="checkbox" name="follow_ip" id="dev-follow-ip" ${editing && editing.follow_ip ? "checked" : ""}>
          Seguir la IP automáticamente si el equipo cambia de dirección (misma serie o MAC)</label>
      </div>
    </div>`;
  formRoot.hidden = false;
}

function onVendorChange({ keepPorts = false } = {}) {
  const v = selectedVendor();
  fillKinds(v);
  renderVendorInfo(v);
  if (v && !keepPorts && !portsTouched && !ctx.state.editingDevice) {
    const p = v.default_ports || {};
    if (p.http) deviceForm.http_port.value = p.http;
    if (p.rtsp) deviceForm.rtsp_port.value = p.rtsp;
    deviceForm.onvif_port.placeholder = p.onvif && p.onvif !== p.http ? `${p.onvif}` : "igual que HTTP";
  }
  updatePathsPreview();
}

function updatePathsPreview() {
  const v = selectedVendor();
  const vendor = deviceForm.vendor.value;
  const kind = deviceForm.kind.value;
  const box = $("#paths-preview");
  const manual = (v ? v.manual_path : vendor === "generic") && !ctx.state.editingDevice;
  $$("[data-manual-path]", deviceForm).forEach((el) => { el.hidden = !manual; });
  const ex = v ? v.preset_examples : [];
  if (ex.length) {
    box.innerHTML = `<dl style="margin:0">
      <dt>Canal 1 · principal (se graba)</dt><dd>${esc(ex[0].main)}</dd>
      ${ex[0].sub ? `<dt>Canal 1 · subflujo (vista en vivo y analítica)</dt><dd>${esc(ex[0].sub)}</dd>` : ""}
      ${MULTI_KINDS.has(kind) && ex[1] ? `<dt>Canal 2 · principal</dt><dd>${esc(ex[1].main)}</dd>` : ""}
    </dl><small class="muted">Se rellenan solas según el canal. Puedes cambiarlas por cámara si el equipo usa otras.</small>`;
  } else if (vendor === "onvif" || (v && v.capabilities.includes("onvif"))) {
    box.innerHTML = `<span class="muted">Las rutas se piden al equipo por ONVIF (GetStreamUri) al probar la conexión o
      al importar canales. Pulsa «Probar conexión» para verlas${manual ? ", o escríbelas abajo" : ""}.</span>`;
  } else if (!v && vendors.failed && vendor !== "generic") {
    box.innerHTML = `<span class="muted">No se pudo leer el registro de marcas del servidor: las rutas se calculan al guardar.</span>`;
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
  const basic = $("#dev-allow-basic");
  const follow = $("#dev-follow-ip");
  if (basic) body.allow_basic = basic.checked;
  if (follow) body.follow_ip = follow.checked;
  const pw = f.password.value;
  if (pw) body.password = pw;
  return body;
}

// Si «Probar conexión» dijo que la contraseña es mala (o el usuario está bloqueado), «Guardar» no vuelve a
// mandarla al equipo para importar canales mientras no cambien la contraseña, el usuario o la dirección:
// sería un segundo intento con la misma contraseña (los equipos bloquean el usuario tras 3-5).
const AUTH_FIELDS = new Set(["password", "username", "host", "http_port", "rtsp_port", "onvif_port", "https", "vendor"]);
deviceForm.addEventListener("input", (ev) => {
  if (AUTH_FIELDS.has(ev.target.name)) ctx.state.authRefused = false;
});
deviceForm.addEventListener("change", (ev) => {
  if (AUTH_FIELDS.has(ev.target.name)) ctx.state.authRefused = false;
});

export function openDeviceDialog(dev = null, prefill = {}) {
  ctx.state.editingDevice = dev;
  ctx.state.testResult = null;
  ctx.state.authRefused = false;
  portsTouched = false;
  deviceForm.reset();
  clearFieldErrors(deviceForm);
  setFormError($("#device-form-error"), "");
  const res = $("#device-test-result");
  res.hidden = true;
  res.innerHTML = "";
  $("#device-dialog-title").textContent = dev ? `Editar «${dev.name}»` : "Añadir equipo";
  const src = dev || prefill;
  const f = deviceForm;
  if (vendors.loaded) fillVendorSelect();
  if (src.vendor) f.vendor.value = src.vendor;
  fillKinds(selectedVendor());
  if (src.name) f.name.value = src.name;
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
  onVendorChange({ keepPorts: !!(dev || src.http_port || src.rtsp_port) });
  deviceDialog.showModal();
  f.name.focus();
  if (!vendors.loaded && !vendors.pending) loadVendors({ retry: true });
}

deviceForm.vendor.addEventListener("change", () => onVendorChange());
deviceForm.kind.addEventListener("change", updatePathsPreview);

function renderTestResult(r) {
  const box = $("#device-test-result");
  box.hidden = false;
  box.className = `result-box ${r.ok ? "ok" : "bad"}`;
  const info = r.info || {};
  const parts = [];
  parts.push(`<strong>${r.ok ? "Conexión correcta" : r.locked ? "Usuario bloqueado en el equipo" : "La prueba falló"}</strong>`);
  if (r.message) parts.push(`<div>${esc(r.message)}</div>`);
  const checks = [
    ["Equipo accesible", r.reachable],
    ["Usuario y contraseña", r.auth_ok],
    ["Vídeo RTSP", r.rtsp_ok],
  ].map(([label, v]) => `<span class="pill ${v === true ? "ok" : v === false ? "bad" : "off"}">${label}</span>`).join(" ");
  parts.push(`<div style="margin:8px 0;display:flex;gap:6px;flex-wrap:wrap">${checks}</div>`);
  if (info.model || info.serial) {
    // Con «ONVIF (otras marcas)» el driver no dice la marca: se enseña la que dice el equipo (la auditoría la usa).
    const maker = info.manufacturer && info.manufacturer.toLowerCase() !== String(vendorLabel(info.vendor)).toLowerCase()
      ? ` (fabricante: ${esc(info.manufacturer)})` : "";
    parts.push(`<div class="small muted">${esc(vendorLabel(info.vendor))}${maker} ${esc(info.model || "")}
      ${info.serial ? `· n.º de serie <span class="mono">${esc(info.serial)}</span>` : ""}
      ${info.firmware ? `· firmware ${esc(info.firmware)}${info.firmware_date ? ` (${esc(info.firmware_date)})` : ""}` : ""}</div>`);
  }
  if ((r.warnings || []).length) {
    parts.push(`<ul class="small" style="margin:8px 0 0 18px;padding:0;color:var(--warn)">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`);
  }
  const chans = r.channels || [];
  if (chans.length && !ctx.state.editingDevice) {
    parts.push(`<div style="margin-top:10px"><strong>Canales a dar de alta como cámaras</strong></div>
      <div class="channel-list" id="test-channels">${chans.map((c) => channelItem(c, false, c.online !== false, null)).join("")}</div>`);
  } else if (chans.length && ctx.state.editingDevice) {
    parts.push(`<div class="channel-list" style="margin-top:10px">${chans.map((c) => channelItem(c, true, false, ctx.state.editingDevice)).join("")}</div>`);
  }
  box.innerHTML = parts.join("");
}

function canFixCodec(dev) {
  const v = dev && vendors.byId[dev.vendor];
  return !!(v && v.capabilities.includes("api_codec_fix"));
}

function channelItem(c, already, checked, dev) {
  const badSub = c.has_sub !== false && c.sub_codec && c.sub_codec !== "H.264";
  const fix = badSub && dev && canFixCodec(dev)
    ? ` <button type="button" class="btn btn-sm" data-codec-fix="${Number(c.channel)}" data-device="${esc(dev.id)}">Corregir códec</button>` : "";
  const codec = badSub
    ? `<div class="meta" style="color:var(--warn)">Subflujo ${esc(c.sub_codec)}: tiene que ir en H.264 para verse en el navegador${fix ? "" : " (cámbialo en el equipo)"}.${fix}</div>` : "";
  const meta = [c.main_resolution, c.main_codec, c.analog === true ? "analógica" : null, c.ip_address].filter(Boolean).map(esc).join(" · ");
  return `<label class="channel-item">
    <input type="checkbox" value="${Number(c.channel)}" ${checked && !already ? "checked" : ""} ${already ? "disabled" : ""}>
    <span><strong>${Number(c.channel)}. ${esc(c.name || `Canal ${c.channel}`)}</strong>
      <div class="meta">${already ? "Ya añadido · " : ""}${c.online === false ? "Sin vídeo · " : ""}${meta}</div>${codec}</span>
  </label>`;
}

// «Corregir códec»: confirmación explícita, copia en el servidor, auditoría y «Deshacer» durante 30 días.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-codec-fix]");
  if (!btn) return;
  ev.preventDefault();
  const devId = btn.dataset.device;
  const channel = Number(btn.dataset.codecFix);
  const dev = ctx.state.devices.find((d) => d.id === devId);
  const ok = await confirmDialog("Corregir códec",
    `Se cambiará el subflujo del canal ${channel} de «${dev ? dev.name : devId}» a H.264 en el propio equipo. ` +
    "Antes se guarda una copia de su configuración y podrás deshacerlo durante 30 días. El cambio queda registrado.",
    { okText: "Cambiar a H.264" });
  if (!ok) return;
  await busy(btn, async () => {
    try {
      const rec = await post(`/api/devices/${enc(devId)}/codec-fix`, { channel, stream: "sub", codec: "H.264", confirm: true });
      toast(`Subflujo del canal ${channel} cambiado a H.264 (antes ${rec.previous_codec || "?"}). Puedes deshacerlo en «Canales».`, "ok", 8000);
      btn.closest(".meta").textContent = "Subflujo cambiado a H.264.";
    } catch (err) {
      toastError(err, "No se pudo cambiar el códec");
    }
  });
});

async function renderCodecHistory(dev) {
  const box = $("#codec-history");
  if (!box) return;
  if (!canFixCodec(dev)) { box.innerHTML = ""; return; }
  try {
    const items = (await get(`/api/devices/${enc(dev.id)}/codec-fix`)).filter((r) => r.undo_available);
    box.innerHTML = items.length ? `<div class="small" style="margin-top:12px"><strong>Cambios de códec (últimos 30 días)</strong>
      ${items.map((r) => `<div style="display:flex;gap:8px;align-items:center;margin-top:4px">Canal ${Number(r.channel)}:
        ${esc(r.previous_codec || "?")} → ${esc(r.new_codec)} · ${esc(r.user)}
        <button type="button" class="btn btn-sm" data-codec-undo="${esc(r.backup_id)}">Deshacer</button></div>`).join("")}</div>` : "";
  } catch (err) {
    box.innerHTML = "";
  }
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-codec-undo]");
  if (!btn) return;
  const dev = ctx.state.channelsDevice;
  if (!dev) return;
  const ok = await confirmDialog("Deshacer el cambio de códec",
    "Se repondrá en el equipo la configuración que tenía antes del cambio.", { okText: "Deshacer" });
  if (!ok) return;
  await busy(btn, async () => {
    try {
      await post(`/api/devices/${enc(dev.id)}/codec-fix/undo`, { backup_id: btn.dataset.codecUndo });
      toast("Configuración anterior repuesta en el equipo", "ok");
      await renderCodecHistory(dev);
    } catch (err) {
      toastError(err, "No se pudo deshacer");
    }
  });
});

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
      const dev = ctx.state.editingDevice;
      const r = dev && !body.password ? await post(`/api/devices/${enc(dev.id)}/test`, undefined, { timeoutMs: 45000 })
        : await post("/api/devices/test", body, { timeoutMs: 45000 });
      ctx.state.testResult = r;
      ctx.state.authRefused = !!r && (r.auth_ok === false || r.locked === true);
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
      const dev = ctx.state.editingDevice;
      if (dev) {
        const upd = { ...body };
        if (!upd.password) upd.password = $("#dev-clear-pw").checked ? "" : null;
        await patch(`/api/devices/${enc(dev.id)}`, upd);
        toast(`Equipo «${body.name}» actualizado`, "ok");
      } else {
        const v = selectedVendor();
        const manual = v ? v.manual_path : body.vendor === "generic";
        const testChans = $$("#test-channels input:checked").map((i) => Number(i.value));
        const tested = ctx.state.testResult && (ctx.state.testResult.channels || []).length;
        const refused = !!ctx.state.authRefused;
        if (refused) {
          // se guarda sin tocar el equipo; los canales se importan después con la contraseña corregida
        } else if (!manual || tested) {
          if (tested) body.import_channels = testChans;
          else body.import_channels = body.kind === "camera" ? [1] : "all";
        }
        const created = await post("/api/devices", body, { timeoutMs: 45000 });
        const importError = created && (created.import_error || (created.details && created.details.import_error));
        if (manual && !tested) {
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
        if (refused) {
          toast("Equipo guardado sin cámaras: la prueba rechazó la contraseña (o el usuario está bloqueado). "
            + "Corrígela en «Editar» y después importa los canales.", "bad", 12000);
        }
      }
      deviceDialog.close();
      await Promise.all([ctx.loadDevices(), ctx.loadCameras()]);
    } catch (err) {
      if (!showFieldErrors(deviceForm, err)) setFormError($("#device-form-error"), errorText(err));
    }
  });
});

// ------------------------------------------------------------------ importar canales
const channelsDialog = $("#channels-dialog");
const codecHistory = document.createElement("div");
codecHistory.id = "codec-history";
$("#channels-list").after(codecHistory);

async function openChannelsDialog(dev) {
  ctx.state.channelsDevice = dev;
  $("#channels-title").textContent = `Canales de «${dev.name}»`;
  $("#channels-intro").textContent = "Consultando los canales del equipo…";
  $("#channels-list").innerHTML = "";
  codecHistory.innerHTML = "";
  $("#btn-channels-import").disabled = true;
  channelsDialog.showModal();
  if (!vendors.loaded) await loadVendors();
  try {
    const chans = await get(`/api/devices/${enc(dev.id)}/channels`, { timeoutMs: 45000 });
    const existing = new Set(ctx.state.cameras.filter((c) => c.device_id === dev.id).map((c) => c.channel));
    $("#channels-intro").textContent = chans.length
      ? `${chans.length} canales encontrados. Marca los que quieras dar de alta como cámaras.`
      : "El equipo no informó de ningún canal.";
    $("#channels-list").innerHTML = chans.map((c) => channelItem(c, existing.has(c.channel), c.online !== false, dev)).join("");
    $("#btn-channels-import").disabled = !chans.length;
  } catch (err) {
    $("#channels-intro").textContent = `No se pudieron leer los canales: ${errorText(err)}`;
  }
  await renderCodecHistory(dev);
}

$("#channels-all").addEventListener("click", () => $$("#channels-list input:not(:disabled)").forEach((i) => { i.checked = true; }));
$("#channels-none").addEventListener("click", () => $$("#channels-list input").forEach((i) => { i.checked = false; }));
$("#btn-channels-import").addEventListener("click", async (ev) => {
  const dev = ctx.state.channelsDevice;
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
      await Promise.all([ctx.loadDevices(), ctx.loadCameras()]);
    } catch (err) {
      toastError(err, "No se pudieron importar los canales");
    }
  });
});

// ------------------------------------------------------------------ búsqueda en la red y cambios de IP
const discoverDialog = $("#discover-dialog");
$("#btn-discover").addEventListener("click", () => {
  if (!vendors.loaded) loadVendors();
  discoverDialog.showModal();
});
const ipCheckBtn = document.createElement("button");
ipCheckBtn.type = "button";
ipCheckBtn.className = "btn";
ipCheckBtn.id = "btn-ip-check";
ipCheckBtn.textContent = "Buscar cambios de IP";
ipCheckBtn.title = "Encuentra equipos ya dados de alta que ahora están en otra IP (misma serie o MAC)";
$("#btn-discover-run").after(ipCheckBtn);

const SOURCE_LABELS = { wsd: "ONVIF", sadp: "SADP", dhip: "DHIP" };

// Tipo probable a partir del modelo («XVR…», «…DVR…», «NVR…»); el instalador puede cambiarlo.
function guessKind(d) {
  const text = `${d.model || ""} ${d.name || ""}`;
  if (/xvr/i.test(text)) return "xvr";
  if (/dvr|hvr/i.test(text)) return "dvr";
  if (/nvr/i.test(text)) return "nvr";
  return "camera";
}

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
        <td><span class="vendor-tag ${esc(d.vendor_guess)}">${esc(vendorLabel(d.vendor_guess))}</span>
          ${(d.sources || []).length ? `<div class="muted small">${esc(d.sources.map((s) => SOURCE_LABELS[s] || s).join(", "))}</div>` : ""}</td>
        <td>${esc(d.model || "—")}</td><td>${esc(d.name || "—")}</td>
        <td>${d.already_added ? '<span class="pill ok">Ya añadido</span>'
          : `<button type="button" class="btn btn-sm btn-primary" data-add="${i}">Añadir</button>`}</td></tr>`).join("");
      tbody.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => {
        const d = list[Number(b.dataset.add)];
        discoverDialog.close();
        openDeviceDialog(null, {
          name: d.name || d.model || d.host, vendor: d.vendor_guess, host: d.host, http_port: d.http_port,
          kind: guessKind(d),
        });
      }));
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">${esc(errorText(err))}</td></tr>`;
    }
  });
});

ipCheckBtn.addEventListener("click", async (ev) => {
  const tbody = $("#discover-table tbody");
  const timeout = Number($("#discover-timeout").value) || 3;
  tbody.innerHTML = `<tr><td colspan="5" class="empty">Buscando equipos que cambiaron de IP durante ${timeout} s…</td></tr>`;
  await busy(ev.currentTarget, async () => {
    try {
      const res = await post("/api/devices/ip-check", { timeout_s: timeout }, { timeoutMs: (timeout + 15) * 1000 });
      const list = res.proposals || [];
      if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty"><strong>Ningún equipo ha cambiado de IP</strong>
          Para evitarlo, pon IP fija o reserva DHCP a cada equipo.</td></tr>`;
        return;
      }
      // La IP llega por un descubrimiento sin autenticar: si el servidor no ha podido comprobar con la API
      // que es el mismo equipo, pide volver a escribir la contraseña (no se reutiliza la guardada).
      const pwInput = (i, host) => `<input type="password" class="input move-pw" style="max-width:13rem;margin-bottom:.35rem" data-move-pw="${i}" autocomplete="new-password"
        placeholder="Contraseña del equipo" aria-label="Contraseña del equipo para ${esc(host)}">`;
      tbody.innerHTML = list.map((p, i) => `<tr>
        <td class="mono">${esc(p.old_host)} → ${esc(p.new_host)}</td>
        <td colspan="3">${esc(p.message_es)} <span class="muted small">(misma ${p.match === "serial" ? "serie" : "MAC"})</span></td>
        <td>${p.applied ? '<span class="pill ok">Actualizado solo</span>'
          : `${p.needs_password ? pwInput(i, p.new_host) : ""}
             <button type="button" class="btn btn-sm btn-primary" data-move="${i}">Actualizar</button>`}</td></tr>`).join("");
      tbody.querySelectorAll("[data-move]").forEach((b) => b.addEventListener("click", async () => {
        const i = Number(b.dataset.move);
        const p = list[i];
        const input = tbody.querySelector(`[data-move-pw="${i}"]`);
        if (input && !input.value) {
          toast("Escribe la contraseña del equipo para usar la IP nueva", "bad");
          input.focus();
          return;
        }
        await busy(b, async () => {
          try {
            const payload = { host: p.new_host };
            if (input) payload.password = input.value;
            await post(`/api/devices/${enc(p.device_id)}/move`, payload);
            toast(`«${p.device_name}» ahora usa ${p.new_host}`, "ok");
            if (input) input.remove();
            b.replaceWith(Object.assign(document.createElement("span"), { className: "pill ok", textContent: "Actualizado" }));
            await ctx.loadDevices();
          } catch (err) {
            if (!input && (err.fields || []).some((f) => (f.loc || []).includes("password"))) {
              b.insertAdjacentHTML("beforebegin", pwInput(i, p.new_host));
              tbody.querySelector(`[data-move-pw="${i}"]`).focus();
              toast(err.message, "bad", 9000);
            } else {
              toastError(err, "No se pudo actualizar la IP");
            }
          }
        });
      }));
      if (list.some((p) => p.applied)) await ctx.loadDevices();
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">${esc(errorText(err))}</td></tr>`;
    }
  });
});

// ------------------------------------------------------------------ rutas de una cámara (formulario de cámara)
const pathsCache = new Map();
/** Rutas del preset para un canal, pedidas al registro del servidor: [principal, subflujo] o null. */
export async function presetFor(vendor, channel, kind = "camera") {
  const n = Math.max(1, Math.min(512, Number(channel) || 1));
  const key = `${vendor}|${n}|${kind}`;
  if (!pathsCache.has(key)) {
    pathsCache.set(key, get(`/api/vendors/${enc(vendor)}/paths?channel=${n}&kind=${enc(kind)}`)
      .then((r) => (r && r.main ? [r.main, r.sub] : null))
      .catch(() => { pathsCache.delete(key); return null; }));
  }
  return pathsCache.get(key);
}
