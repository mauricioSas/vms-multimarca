// #security-audit-root (status.html): auditoría de seguridad de los equipos dados de alta, sin atacarlos.
// Nunca dice «seguro». Solo administradores. Dueño: B6. Contrato: CONTRATO §18.12.
import { get, post, ApiError } from "./api.js";
import { toastError, busy } from "./ui.js";
import { h, clear, me, section, statusPill, emptyState, fmtWhen, modal } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("security-audit-root");
const CHECK = {
  weak_password: "Contraseña guardada", admin_user: "Usuario del VMS", anonymous_rtsp: "Vídeo sin contraseña (RTSP)",
  anonymous_onvif: "ONVIF sin contraseña", telnet: "Telnet", ssh: "SSH", http_no_tls: "Web cifrada (HTTPS)",
  sdk_port: "Puerto del SDK", upnp: "UPnP", p2p_cloud: "Nube del fabricante (P2P)", firmware_cve: "Firmware",
  clock: "Hora",
};
// Etiquetas fijas del contrato: nunca «seguro»
const VERDICT = {
  vulnerable: ["critical", "Vulnerable"], probably_vulnerable: ["warning", "Probablemente vulnerable"],
  ok: ["ok", "Sin problemas conocidos"], unknown: ["unknown", "Desconocido"],
};

function verdictPill(f) {
  const [st, text] = VERDICT[f.status] || VERDICT.unknown;
  const label = f.check === "firmware_cve" && f.status === "ok" ? "Sin CVE conocidos en la tabla"
    : f.check === "firmware_cve" && f.status === "vulnerable" && /KEV/.test(f.detail_es) ? "Vulnerable (KEV)" : text;
  return statusPill(f.status === "vulnerable" && f.severity !== "critical" ? "warning" : st, label);
}

function renderReport(out, rep, names) {
  clear(out);
  const bad = rep.findings.filter((f) => f.status === "vulnerable" || f.status === "probably_vulnerable");
  out.append(h("div", { class: `banner ${bad.length ? "bad" : "info"}`, role: "status" },
    `${rep.devices_checked} ${rep.devices_checked === 1 ? "equipo revisado" : "equipos revisados"} el ${fmtWhen(rep.at)} · ` +
    (bad.length ? `${bad.length} ${bad.length === 1 ? "punto a corregir" : "puntos a corregir"}` : "sin puntos a corregir en lo que se puede comprobar") +
    (rep.admin_credentials_used ? " · con usuario administrador temporal (no se guardó)" : "")));
  const byDev = new Map();
  for (const f of rep.findings) {
    if (!byDev.has(f.device_id)) byDev.set(f.device_id, []);
    byDev.get(f.device_id).push(f);
  }
  const order = { vulnerable: 0, probably_vulnerable: 1, unknown: 2, ok: 3 };
  for (const [did, list] of byDev) {
    list.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
    out.append(h("div", { class: "card ops-audit-dev" }, h("h3", {}, names[did] || did),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Comprobación", "Resultado", "Detalle", "Qué hacer"].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, list.map((f) => h("tr", {},
          h("td", {}, CHECK[f.check] || f.check), h("td", {}, verdictPill(f)), h("td", { class: "small" }, f.detail_es),
          h("td", { class: "small" }, f.action_es || "—"))))))));
  }
  out.append(h("p", { class: "muted small" }, `Tabla de avisos: ${rep.advisories_version}. ` +
    "This product uses the NVD API but is not endorsed or certified by the NVD."));
}

function adminCredentialsDialog(devices, onRun) {
  const m = modal("Auditoría con usuario administrador (opcional)", { wide: true });
  m.body.append(h("p", {}, "El usuario del VMS es de solo lectura y no puede leer algunos ajustes (Telnet, UPnP, nube P2P). " +
    "Si quieres revisarlos, escribe el usuario administrador de cada equipo. Se usa una vez para esta auditoría y NO se guarda ni se anota en ningún registro."));
  const rows = devices.map((d) => {
    const u = h("input", { class: "input", autocomplete: "off", placeholder: "admin", "aria-label": `Usuario administrador de ${d.name}` });
    const p = h("input", { class: "input", type: "password", autocomplete: "new-password", "aria-label": `Contraseña de administrador de ${d.name}` });
    return { d, u, p, row: h("tr", {}, h("td", {}, d.name), h("td", {}, u), h("td", {}, p)) };
  });
  m.body.append(h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Equipo"), h("th", {}, "Usuario administrador"), h("th", {}, "Contraseña"))),
    h("tbody", {}, rows.map((r) => r.row)))));
  m.foot.append(h("div", { class: "right" },
    h("button", { type: "button", class: "btn", onclick: m.close }, "Cancelar"),
    h("button", { type: "button", class: "btn btn-primary", onclick: async (e) => {
      const creds = {};
      for (const r of rows) if (r.u.value && r.p.value) creds[r.d.id] = { username: r.u.value, password: r.p.value };
      for (const r of rows) r.p.value = "";
      await busy(e.currentTarget, () => onRun(creds));
      m.close();
    } }, "Hacer auditoría")));
}

async function main() {
  if (!root) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk || u.role !== "admin") return;
  const out = h("div", { class: "ops-audit-out" });
  let devices = [];
  const run = async (creds) => {
    clear(out).append(h("p", { class: "muted" }, "Revisando los equipos… (unos segundos por equipo)"));
    try {
      const rep = await post("/api/security-audit/run", { admin_credentials: creds || {} });
      renderReport(out, rep, Object.fromEntries(devices.map((d) => [d.id, d.name])));
    } catch (err) {
      clear(out).append(h("p", { class: "form-error" }, err.message));
      toastError(err, "Auditoría");
    }
  };
  const body = section(root, { id: "h-ops-security", title: "Auditoría de seguridad",
    subtitle: "Sin atacar los equipos ni probar contraseñas",
    actions: [
      h("button", { type: "button", class: "btn btn-sm", onclick: () => adminCredentialsDialog(devices, run) }, "Con usuario administrador…"),
      h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: (e) => busy(e.currentTarget, () => run({})) }, "Hacer auditoría"),
    ] });
  body.append(out);
  try { devices = await get("/api/devices"); } catch { devices = []; }
  if (!devices.length) {
    out.append(emptyState({ title: "No hay equipos que auditar",
      text: "La auditoría revisa contraseñas, servicios abiertos y firmware de los equipos dados de alta.",
      action: { label: "Añadir equipo", href: "/" } }));
    return;
  }
  try {
    const latest = await get("/api/security-audit/latest");
    if (latest) {
      renderReport(out, latest, Object.fromEntries(devices.map((d) => [d.id, d.name])));
    } else {
      out.append(emptyState({ title: "Todavía no se ha hecho ninguna auditoría",
        text: "Pulsa «Hacer auditoría»: revisa contraseñas guardadas, acceso sin contraseña, Telnet/UPnP y firmware con fallos conocidos." }));
    }
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toastError(err, "Auditoría");
  }
}

main();
