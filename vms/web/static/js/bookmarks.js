// #bookmarks-root (playback.html): marcadores y «Proteger» (bloqueo de retención con motivo y caducidad).
// Crear: operador con permiso de marcador; proteger: administrador o permiso de exportar; cambiar o borrar:
// administrador (se audita). Dueño: B6. Contrato: CONTRATO §18.6.
import { get, post, patch, del, enc, isId } from "./api.js";
import { toast, toastError, busy, confirmDialog } from "./ui.js";
import { h, clear, me, section, emptyState, fmtWhen, statusPill, modal, onOpsEvent } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("bookmarks-root");
let user = null;
let list = null;

function cameraId() {
  const v = document.getElementById("pb-camera")?.value || "";
  return isId(v) ? v : "";
}

function dayRange() {
  const v = window.__vmsPlayback?.view;
  const from = v ? new Date(v.from) : new Date();
  const start = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  return [start, new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1)];
}

function hm(iso) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

async function load() {
  if (!list) return;
  const cam = cameraId();
  clear(list);
  if (!cam) return;
  const [s, e] = dayRange();
  let items = [];
  try {
    items = await get(`/api/bookmarks?camera_id=${enc(cam)}&from=${enc(s.toISOString())}&to=${enc(e.toISOString())}`);
  } catch (err) {
    list.append(h("p", { class: "form-error" }, err.message));
    return;
  }
  if (!items.length) {
    list.append(emptyState({ title: "No hay marcadores en este día",
      text: "Un marcador apunta un momento importante. «Proteger» evita que la retención borre ese tramo.",
      action: { label: "Añadir marcador aquí", onClick: () => openCreate() } }));
    return;
  }
  list.append(h("div", { class: "card table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Momento", "Nota", "Protección"].map((x) => h("th", { scope: "col" }, x)),
      h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Acciones")))),
    h("tbody", {}, items.map(row)))));
}

function row(bm) {
  const prot = bm.protected
    ? h("div", {}, statusPill("ok", `Protegido hasta ${new Date(bm.protect_until).toLocaleDateString("es-ES")}`),
      h("div", { class: "muted small" }, bm.protect_reason))
    : bm.released_at ? h("span", { class: "muted small" }, `Ya no protegido (${bm.release_reason})`) : h("span", { class: "muted" }, "—");
  const actions = h("div", { class: "row-actions" },
    h("button", { type: "button", class: "btn btn-sm", onclick: () => window.__vmsPlayback?.play(bm.start) }, "Ir"));
  if (user.role === "admin") {
    actions.append(bm.protected
      ? h("button", { type: "button", class: "btn btn-sm", onclick: () => unprotect(bm) }, "Dejar de proteger")
      : h("button", { type: "button", class: "btn btn-sm", onclick: () => protect(bm) }, "Proteger"),
    h("button", { type: "button", class: "btn btn-sm btn-danger", onclick: () => remove(bm) }, "Borrar"));
  }
  return h("tr", { dataset: { bookmark: bm.id } },
    h("td", { class: "mono" }, hm(bm.start), bm.end ? ` – ${hm(bm.end)}` : ""),
    h("td", {}, bm.note || "—", bm.case_ref ? h("div", { class: "muted small" }, `Caso ${bm.case_ref}`) : null,
      h("div", { class: "muted small" }, `${bm.created_by} · ${fmtWhen(bm.created_at)}`)),
    h("td", {}, prot), h("td", {}, actions));
}

function reasonDialog(title, label, okText, withDays) {
  return new Promise((resolve) => {
    const m = modal(title);
    const reason = h("input", { class: "input", maxlength: "300", required: true, "aria-label": label });
    const days = h("input", { class: "input mono", type: "number", min: "1", max: "3650", value: "90", "aria-label": "Días" });
    m.body.append(h("div", { class: "field" }, h("label", {}, label, reason)),
      withDays ? h("div", { class: "field" }, h("label", {}, "Conservar durante (días) ", days)) : null,
      h("p", { class: "muted small" }, "Queda anotado en el registro de accesos con tu usuario."));
    m.foot.append(h("div", { class: "right" },
      h("button", { type: "button", class: "btn", onclick: () => { m.close(); resolve(null); } }, "Cancelar"),
      h("button", { type: "button", class: "btn btn-primary", onclick: () => {
        if (reason.value.trim().length < 3) { reason.setAttribute("aria-invalid", "true"); reason.focus(); return; }
        m.close();
        resolve({ reason: reason.value.trim(), days: Number(days.value) || 90 });
      } }, okText)));
    reason.focus();
  });
}

async function protect(bm) {
  const r = await reasonDialog("Proteger tramo", "Motivo (obligatorio)", "Proteger", true);
  if (!r) return;
  try {
    await patch(`/api/bookmarks/${enc(bm.id)}`, { protect: true, protect_reason: r.reason, protect_days: r.days });
    toast("Tramo protegido: la retención no lo borrará hasta su caducidad", "ok");
    load();
  } catch (err) { toastError(err, "No se pudo proteger"); }
}

async function unprotect(bm) {
  const r = await reasonDialog("Dejar de proteger", "Motivo (obligatorio)", "Dejar de proteger", false);
  if (!r) return;
  try {
    await patch(`/api/bookmarks/${enc(bm.id)}`, { protect: false, protect_reason: r.reason });
    toast("El tramo ya no está protegido", "ok");
    load();
  } catch (err) { toastError(err, "No se pudo cambiar"); }
}

async function remove(bm) {
  if (!(await confirmDialog("Borrar marcador", bm.protected
    ? "El marcador está protegido: al borrarlo, la copia protegida también se borra." : "Se borrará el marcador.",
    { okText: "Borrar", danger: true }))) return;
  try {
    await del(`/api/bookmarks/${enc(bm.id)}`);
    load();
  } catch (err) { toastError(err, "No se pudo borrar"); }
}

function openCreate() {
  const cam = cameraId();
  const pos = window.__vmsPlayback?.position;
  if (!cam) { toast("Elige primero una cámara", "bad"); return; }
  if (!pos) { toast("Pulsa primero en la línea de tiempo el momento que quieres marcar", "bad"); return; }
  const m = modal("Añadir marcador");
  const note = h("input", { class: "input", maxlength: "500", placeholder: "p. ej. Discusión en caja 2", "aria-label": "Nota" });
  const dur = h("select", { class: "input", "aria-label": "Duración" },
    [[0, "Solo este momento"], [60, "1 minuto"], [300, "5 minutos"], [900, "15 minutos"], [3600, "1 hora"]].map(([v, t]) =>
      h("option", { value: String(v), selected: v === 300 }, t)));
  const caseRef = h("input", { class: "input", maxlength: "64", "aria-label": "N.º de caso o atestado" });
  const prot = h("input", { type: "checkbox" });
  const reason = h("input", { class: "input", maxlength: "300", disabled: true, "aria-label": "Motivo de la protección" });
  const days = h("input", { class: "input mono", type: "number", min: "1", max: "3650", value: "90", disabled: true, "aria-label": "Días de protección" });
  prot.addEventListener("change", () => { reason.disabled = days.disabled = !prot.checked; if (prot.checked) reason.focus(); });
  const err = h("p", { class: "form-error", role: "alert", hidden: true });
  m.body.append(h("p", {}, `Momento: ${new Date(pos).toLocaleString("es-ES")}`),
    h("div", { class: "form-grid" },
      h("div", { class: "field span-2" }, h("label", {}, "Nota ", note)),
      h("div", { class: "field" }, h("label", {}, "Duración del tramo ", dur)),
      h("div", { class: "field" }, h("label", {}, "N.º de caso (opcional) ", caseRef)),
      h("div", { class: "field span-2" }, h("label", { class: "check" }, prot,
        " Proteger: que la retención no borre este tramo (necesita motivo; queda registrado)")),
      h("div", { class: "field" }, h("label", {}, "Motivo ", reason)),
      h("div", { class: "field" }, h("label", {}, "Días ", days))), err);
  m.foot.append(h("div", { class: "right" },
    h("button", { type: "button", class: "btn", onclick: m.close }, "Cancelar"),
    h("button", { type: "button", class: "btn btn-primary", onclick: async (e) => {
      const start = new Date(pos);
      const secs = Number(dur.value);
      const body = { camera_id: cam, start: start.toISOString(), end: secs ? new Date(start.getTime() + secs * 1000).toISOString() : null,
                     note: note.value.trim(), case_ref: caseRef.value.trim(), protect: prot.checked,
                     protect_reason: reason.value.trim(), protect_days: Number(days.value) || 90 };
      await busy(e.currentTarget, async () => {
        try {
          await post("/api/bookmarks", body);
          toast(prot.checked ? "Marcador creado y tramo protegido" : "Marcador creado", "ok");
          m.close();
          load();
        } catch (ex) { err.textContent = ex.message; err.hidden = false; }
      });
    } }, "Guardar marcador")));
  note.focus();
}

async function main() {
  if (!root) return;
  try { user = await me(); } catch { return; }
  if (!user || user.kiosk) return;
  const body = section(root, { id: "h-ops-bookmarks", title: "Marcadores y tramos protegidos",
    actions: [h("button", { type: "button", class: "btn btn-sm btn-primary", id: "bm-add", onclick: openCreate }, "Añadir marcador aquí")] });
  list = h("div", { class: "ops-bookmarks" });
  body.append(list);
  document.getElementById("pb-camera")?.addEventListener("change", () => setTimeout(load, 50));
  document.getElementById("pb-date")?.addEventListener("change", () => setTimeout(load, 50));
  onOpsEvent("bookmark", (ev) => { if (ev.camera_id === cameraId()) load(); });
  setTimeout(load, 300);
}

main();
