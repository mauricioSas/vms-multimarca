"use strict";
// Página «Versiones» del panel central (CONTRATO §15.7). Dueño: B4.
(async () => {
  const { api, el, clear, topbar, showMsg, fmtDate } = VMS;
  const errBox = document.getElementById("error");
  const me = await topbar("updates");
  const isAdmin = !!me && me.role === "admin";
  const CHANNELS = ["stable", "pilot"];
  const RESULT = {
    update_ok: ["ok", "Actualizada"], rollback_ok: ["degraded", "Vuelta atrás hecha"],
    update_failed: ["down", "Falló (volvió a la anterior)"], metadata_expired: ["down", "Metadatos caducados"],
    clock_skew: ["down", "Reloj desfasado"], error: ["down", "Error al comprobar"], disk_full: ["down", "Sin espacio"],
    waiting_window: ["degraded", "Esperando ventana"], reboot_pending: ["degraded", "Reinicio de Windows pendiente"],
    held: ["degraded", "Retenida"], min_from: ["degraded", "Necesita instalador completo"],
    no_update: ["ok", "Al día"], none: ["unknown", "Sin datos"],
  };
  const PROBLEMS = new Set(["update_failed", "metadata_expired", "clock_skew", "error", "disk_full", "min_from"]);
  let rows = [];

  function badge(result) {
    const [cls, text] = RESULT[result] || ["unknown", result || "—"];
    return el("span", { class: "badge " + cls }, text);
  }

  async function act(fn) {
    try { await fn(); await load(); } catch (e) { showMsg(errBox, e.message); }
  }

  function actions(r) {
    if (!isAdmin) return el("span", { class: "muted" }, "—");
    const path = "/api/updates/sites/" + encodeURIComponent(r.site_id);
    const channel = el("select", { "aria-label": `Canal de ${r.name}` },
      ...CHANNELS.map((c) => el("option", { value: c, selected: c === r.channel ? "" : null }, c)));
    channel.addEventListener("change", () => act(() => api(path, { method: "PUT", json: { channel: channel.value } })));
    const hold = el("button", { class: "btn", type: "button" }, r.hold ? "Reanudar" : "Retener");
    hold.addEventListener("click", () => act(() => api(path, { method: "PUT", json: { hold: !r.hold } })));
    const check = el("button", { class: "btn", type: "button" }, "Comprobar ahora");
    check.addEventListener("click", () => act(() => api(path + "/check", { method: "POST" })));
    const back = el("button", { class: "btn danger", type: "button" }, r.rollback_to ? "Cancelar vuelta atrás" : "Volver a la anterior");
    back.addEventListener("click", () => {
      if (r.rollback_to) { act(() => api(path + "/rollback", { method: "DELETE" })); return; }
      const msg = `¿Volver a la versión anterior en ${r.name}? Se hará en su próximo latido y se perderán los ` +
        "cambios de configuración hechos desde la última actualización.";
      if (confirm(msg)) act(() => api(path + "/rollback", { method: "POST", json: { to: null } }));
    });
    return el("div", { class: "upd-actions" }, channel, hold, check, back);
  }

  function render() {
    const q = document.getElementById("filter").value.trim().toLowerCase();
    const only = document.getElementById("only").value;
    const shown = rows.filter((r) => {
      if (q && !(`${r.name} ${r.code} ${r.site_id}`.toLowerCase().includes(q))) return false;
      if (only === "problems") return PROBLEMS.has(r.last_result);
      if (only === "pilot") return r.channel === "pilot";
      if (only === "held") return r.hold;
      return true;
    });
    const box = clear(document.getElementById("sites"));
    if (!shown.length) { box.append(el("div", { class: "empty" }, "No hay sedes que mostrar")); return; }
    box.append(el("table", { class: "upd-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Sede"), el("th", {}, "Versión"), el("th", {}, "Resultado"),
        el("th", {}, "Canal"), el("th", {}, "Ventana"), el("th", {}, "Último latido"), el("th", {}, "Acciones"))),
      el("tbody", {}, shown.map((r) => el("tr", {},
        el("td", {}, el("div", {}, r.name), el("div", { class: "muted small" }, r.code || r.site_id)),
        el("td", {}, r.installed || "—", r.available ? el("div", { class: "muted small" }, `Disponible: ${r.available}`) : null),
        el("td", {}, badge(r.last_result), r.message_es ? el("div", { class: "upd-msg muted" }, r.message_es) : null,
          r.reboot_pending ? el("div", { class: "upd-pending" }, "Windows tiene un reinicio pendiente") : null),
        el("td", {}, r.channel, r.hold ? el("div", { class: "upd-pending" }, "Retenida") : null,
          r.pending ? el("div", { class: "upd-pending" }, "Pendiente de aplicar en la tienda") : null,
          r.rollback_to ? el("div", { class: "upd-pending" }, "Vuelta atrás pedida") : null),
        el("td", {}, r.window),
        el("td", {}, fmtDate(r.last_seen)),
        el("td", {}, actions(r)))))));
    const fails = rows.filter((r) => PROBLEMS.has(r.last_result)).length;
    document.getElementById("summary").textContent =
      `${rows.length} sedes · ${rows.filter((r) => r.channel === "pilot").length} en pilot · ` +
      `${rows.filter((r) => r.hold).length} retenidas · ${fails} con problemas`;
  }

  async function load() {
    try { rows = await api("/api/updates/sites"); render(); }
    catch (e) { showMsg(errBox, e.message); }
  }

  document.getElementById("filter").addEventListener("input", render);
  document.getElementById("only").addEventListener("change", render);
  document.getElementById("filter-form").addEventListener("submit", (ev) => ev.preventDefault());
  await load();
  setInterval(load, 30000);
})();
