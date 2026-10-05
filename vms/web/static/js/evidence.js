// #evidence-root (playback.html): exportación de evidencias con motivo obligatorio y progreso por SSE.
// El paquete lleva los segmentos originales, el MP4 unido sin recodificar, manifiesto firmado (Ed25519), acta
// y visor portátil con marca de agua superpuesta. Dueño: B6. Contrato: CONTRATO §18.7.
import { get, post, del, enc, isId } from "./api.js";
import { toast, toastError, busy, confirmDialog, fmtBytes } from "./ui.js";
import { h, clear, me, section, emptyState, fmtWhen, statusPill, onOpsEvent } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("evidence-root");
let user = null;
let listBox = null;
const progress = new Map();

function pad(n) { return String(n).padStart(2, "0"); }
function localInput(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const STATE = { queued: ["unknown", "En cola"], running: ["unknown", "Preparando…"], done: ["ok", "Listo"], failed: ["critical", "Falló"] };

async function loadList() {
  if (!listBox) return;
  let items = [];
  try { items = await get("/api/evidence/exports"); } catch (err) {
    clear(listBox).append(h("p", { class: "form-error" }, err.message));
    return;
  }
  clear(listBox);
  if (!items.length) {
    listBox.append(emptyState({ title: "Todavía no has exportado ninguna evidencia",
      text: "Los paquetes que prepares aparecerán aquí para descargarlos. Se guardan en el PC hasta que un administrador los borre." }));
    return;
  }
  listBox.append(h("div", { class: "card table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Paquete", "Motivo", "Estado", "Tamaño"].map((x) => h("th", { scope: "col" }, x)),
      h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Acciones")))),
    h("tbody", {}, items.map((e) => {
      const [st, label] = STATE[e.state] || STATE.queued;
      const pct = progress.get(e.export_id) ?? e.progress;
      const actions = h("div", { class: "row-actions" });
      if (e.state === "done" && e.download_url) actions.append(h("a", { class: "btn btn-sm btn-primary", href: e.download_url, download: "" }, "Descargar"));
      if (user.role === "admin") {
        actions.append(h("button", { type: "button", class: "btn btn-sm btn-danger", onclick: async () => {
          if (!(await confirmDialog("Borrar paquete", "Se borrará el ZIP del PC (las grabaciones originales no se tocan).", { okText: "Borrar", danger: true }))) return;
          try { await del(`/api/evidence/exports/${enc(e.export_id)}`); loadList(); } catch (err) { toastError(err, "No se pudo borrar"); }
        } }, "Borrar"));
      }
      return h("tr", { dataset: { export: e.export_id } },
        h("td", {}, h("span", { class: "mono" }, e.export_id), h("div", { class: "muted small" }, `${e.created_by} · ${fmtWhen(e.created_at)}`),
          e.sha256_manifest ? h("div", { class: "muted small mono", title: "SHA-256 del manifiesto (queda en el registro)" },
            `huella ${e.sha256_manifest.slice(0, 16)}…`) : null),
        h("td", {}, e.request.reason, e.request.case_ref ? h("div", { class: "muted small" }, `Caso ${e.request.case_ref}`) : null),
        h("td", {}, statusPill(st, e.state === "running" ? `${label} ${Math.round(pct * 100)} %` : label),
          e.error ? h("div", { class: "muted small" }, e.error) : null),
        h("td", { class: "mono" }, e.bytes ? fmtBytes(e.bytes) : "—"),
        h("td", {}, actions));
    })))));
}

async function main() {
  if (!root) return;
  try { user = await me(); } catch { return; }
  if (!user || user.kiosk) return;
  const body = section(root, { id: "h-ops-evidence", title: "Exportar evidencia",
    subtitle: "Para la policía o un juzgado: originales, visor sin instalar nada, acta y firma digital" });
  let cams = [];
  try { cams = await get("/api/cameras"); } catch { cams = []; }
  if (!cams.length) {
    body.append(emptyState({ title: "No hay cámaras", text: "Cuando haya cámaras grabando podrás exportar sus grabaciones.",
      action: { label: "Añadir cámaras", href: "/" } }));
    return;
  }
  const camSel = h("select", { class: "input", multiple: true, size: String(Math.min(6, cams.length)), "aria-label": "Cámaras" },
    cams.map((c) => h("option", { value: c.id }, c.device_name ? `${c.name} · ${c.device_name}` : c.name)));
  const from = h("input", { class: "input mono", type: "datetime-local", step: "1", "aria-label": "Desde" });
  const to = h("input", { class: "input mono", type: "datetime-local", step: "1", "aria-label": "Hasta" });
  const reason = h("input", { class: "input", name: "reason", maxlength: "500", required: true, placeholder: "p. ej. Hurto en el pasillo 3", "aria-label": "Motivo" });
  const caseRef = h("input", { class: "input", maxlength: "64", "aria-label": "N.º de caso o atestado" });
  const recipient = h("input", { class: "input", maxlength: "120", placeholder: "p. ej. Policía Local", "aria-label": "Destinatario" });
  const mp4 = h("input", { type: "checkbox", checked: true });
  const err = h("p", { class: "form-error", role: "alert", hidden: true });
  const fill = () => {
    const cur = document.getElementById("pb-camera")?.value;
    for (const o of camSel.options) o.selected = o.value === cur;
    const pos = window.__vmsPlayback?.position;
    const base = pos ? new Date(pos) : new Date();
    from.value = localInput(new Date(base.getTime() - 5 * 60000));
    to.value = localInput(new Date(base.getTime() + 5 * 60000));
  };
  fill();
  const form = h("form", { class: "card card-pad ops-evidence-form", novalidate: true },
    h("div", { class: "form-grid" },
      h("div", { class: "field" }, h("label", {}, "Cámaras ", camSel), h("small", {}, "Ctrl o Mayúsculas para elegir varias")),
      h("div", { class: "field" }, h("label", {}, "Desde ", from), h("label", {}, "Hasta ", to),
        h("button", { type: "button", class: "btn btn-sm", onclick: fill }, "±5 min de la posición actual")),
      h("div", { class: "field span-2" }, h("label", {}, "Motivo (obligatorio) ", reason)),
      h("div", { class: "field" }, h("label", {}, "N.º de caso o atestado ", caseRef)),
      h("div", { class: "field" }, h("label", {}, "Quién lo recibe ", recipient)),
      h("div", { class: "field span-2" }, h("label", { class: "check" }, mp4, " Incluir además un MP4 unido por cámara (sin recomprimir)"))),
    err,
    h("div", { class: "ops-row" }, h("button", { type: "submit", class: "btn btn-primary" }, "Crear paquete"),
      h("span", { class: "muted small" }, "Queda registrado con tu usuario. Máximo 12 horas por paquete.")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    const ids = [...camSel.selectedOptions].map((o) => o.value).filter(isId);
    const s = new Date(from.value);
    const t = new Date(to.value);
    if (!ids.length) { err.textContent = "Elige al menos una cámara."; err.hidden = false; return; }
    if (!(t > s)) { err.textContent = "«Hasta» tiene que ser posterior a «Desde»."; err.hidden = false; return; }
    if (reason.value.trim().length < 3) { err.textContent = "Escribe el motivo de la exportación."; err.hidden = false; reason.focus(); return; }
    await busy(e.submitter, async () => {
      try {
        await post("/api/evidence/exports", { camera_ids: ids, start: s.toISOString(), end: t.toISOString(), reason: reason.value.trim(),
                                              case_ref: caseRef.value.trim(), recipient: recipient.value.trim(), include_mp4: mp4.checked });
        toast("Preparando el paquete. Cuando termine, aparecerá «Descargar».", "ok");
        loadList();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    });
  });
  listBox = h("div", { class: "ops-exports" });
  body.append(form, h("h3", {}, "Paquetes"), listBox);
  onOpsEvent("evidence", (ev) => {
    progress.set(ev.export_id, ev.progress ?? 0);
    if (ev.state === "done") toast("Paquete de evidencias listo para descargar", "ok");
    if (ev.state === "failed") toast(`No se pudo preparar el paquete: ${ev.message_es || ""}`, "bad", 8000);
    loadList();
  });
  loadList();
}

main();
