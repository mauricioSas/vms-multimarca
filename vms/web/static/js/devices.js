// Equipos: lista, alta y edición, prueba de conexión, importar canales y búsqueda en la red.
// Separado de panel.js en la fase 0 de la v2 (cambio mecánico, sin cambios de comportamiento).
// Dueño: B5. B5 construye aquí el formulario de alta a partir de GET /api/vendors (contenedor
// #device-form-root de index.html) y elimina presetPaths (los presets pasan a vivir solo en el registro).
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

// Rutas por canal de cada fabricante (mismo criterio que vms/core/rtsp.py).
export function presetPaths(vendor, channel) {
  const n = Math.max(1, Math.min(512, Number(channel) || 1));
  if (vendor === "hikvision") return [`/Streaming/Channels/${n}01`, `/Streaming/Channels/${n}02`];
  if (vendor === "dahua") return [`/cam/realmonitor?channel=${n}&subtype=0`, `/cam/realmonitor?channel=${n}&subtype=1`];
  return null;
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
  if (!ctx.state.devices.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty"><strong>Todavía no hay equipos</strong>
      Añade un grabador o una cámara con «Añadir equipo», o usa «Buscar en la red».</td></tr>`;
    return;
  }
  tbody.innerHTML = ctx.state.devices.map((d) => `
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
  const dev = ctx.state.devices.find((d) => d.id === id);
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
      await Promise.all([ctx.loadDevices(), ctx.loadCameras(), ctx.loadWalls({ keepDraft: true })]);
    } catch (err) {
      toastError(err, "No se pudo borrar el equipo");
    }
  }
});

// ------------------------------------------------------------------ diálogo de equipo
const deviceDialog = $("#device-dialog");
const deviceForm = $("#device-form");

export function setFormError(el, text) {
  el.textContent = text || "";
  el.hidden = !text;
}

function updatePathsPreview() {
  const vendor = deviceForm.vendor.value;
  const kind = deviceForm.kind.value;
  const box = $("#paths-preview");
  const preset1 = presetPaths(vendor, 1);
  const manual = vendor === "generic" && !ctx.state.editingDevice;
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

export function openDeviceDialog(dev = null, prefill = {}) {
  ctx.state.editingDevice = dev;
  ctx.state.testResult = null;
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
  if (chans.length && !ctx.state.editingDevice) {
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
      const dev = ctx.state.editingDevice;
      const r = dev && !body.password ? await post(`/api/devices/${enc(dev.id)}/test`) : await post("/api/devices/test", body);
      ctx.state.testResult = r;
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
        const manual = body.vendor === "generic";
        const testChans = $$("#test-channels input:checked").map((i) => Number(i.value));
        if (!manual) {
          if (ctx.state.testResult && (ctx.state.testResult.channels || []).length) body.import_channels = testChans;
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
      await Promise.all([ctx.loadDevices(), ctx.loadCameras()]);
    } catch (err) {
      if (!showFieldErrors(deviceForm, err)) setFormError($("#device-form-error"), errorText(err));
    }
  });
});

// ------------------------------------------------------------------ importar canales
const channelsDialog = $("#channels-dialog");

async function openChannelsDialog(dev) {
  ctx.state.channelsDevice = dev;
  $("#channels-title").textContent = `Importar canales de «${dev.name}»`;
  $("#channels-intro").textContent = "Consultando los canales del equipo…";
  $("#channels-list").innerHTML = "";
  $("#btn-channels-import").disabled = true;
  channelsDialog.showModal();
  try {
    const chans = await get(`/api/devices/${enc(dev.id)}/channels`, { timeoutMs: 45000 });
    const existing = new Set(ctx.state.cameras.filter((c) => c.device_id === dev.id).map((c) => c.channel));
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
