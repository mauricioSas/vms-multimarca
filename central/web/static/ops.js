/* «Tiendas con problemas hoy» del panel central (CONTRATO §18.16). Dueño: B6. Sin dependencias externas. */
"use strict";

(() => {
  const { api, el, clear, fmtDate, ago, showMsg, topbar } = VMS;
  const LABEL = { critical: "Grave", warning: "Aviso", nodata: "Sin datos", ok: "Correcto" };
  const ICON = { critical: "✖", warning: "⚠", nodata: "?", ok: "✔" };
  const $ = (id) => document.getElementById(id);
  const selected = new Set();

  function badge(status) {
    const s = LABEL[status] ? status : "nodata";
    // el estado nunca solo con color: icono + texto
    return el("span", { class: "badge " + s, role: "status" }, `${ICON[s]} ${LABEL[s]}`);
  }

  function tile(label, value, hint) {
    return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, String(value)),
      el("div", { class: "hint" }, hint || ""));
  }

  async function load() {
    const day = $("day").value;
    const q = day ? `?date=${encodeURIComponent(day)}` : "";
    $("csv").href = `/api/ops/problems.csv${q}`;
    let data;
    try {
      data = await api(`/api/ops/problems${q}`);
    } catch (e) {
      showMsg($("error"), e.message || "No se pudo cargar el estado de las tiendas");
      return;
    }
    $("error").className = "msg";
    $("updated").textContent = "Actualizado " + fmtDate(data.generated_at);
    const s = data.summary;
    clear($("tiles")).append(
      tile("Graves", s.critical, "Cámaras tapadas, sin grabar o tienda sin latido"),
      tile("Con avisos", s.warning, "Imagen mejorable, hora o disco"),
      tile("Sin datos", s.nodata, "Tienda sin informe de ese día"),
      tile("Correctas", s.ok, ""));
    const rows = data.sites.map((r) => {
      const check = el("input", { type: "checkbox", class: "ops-check", "aria-label": `Incluir ${r.name} en los conteos` });
      check.checked = selected.has(r.site_id);
      check.addEventListener("change", () => (check.checked ? selected.add(r.site_id) : selected.delete(r.site_id)));
      const problems = r.problems.length
        ? el("ul", { class: "ops-problems" }, r.problems.map((p) => el("li", {}, p)))
        : el("span", { class: "muted" }, "Sin problemas");
      return el("tr", {},
        el("td", {}, check),
        el("td", {}, el("a", { href: `/sites/${encodeURIComponent(r.site_id)}` }, r.name), el("div", { class: "muted" }, r.code || r.site_id)),
        el("td", {}, badge(r.status)),
        el("td", { class: "num" }, r.score_min ?? "—"),
        el("td", { class: "num" }, `${r.cameras_critical} / ${r.cameras_warning}`),
        el("td", { class: "num" }, r.forecast_days == null ? "—" : Math.round(r.forecast_days) + " d"),
        el("td", {}, problems),
        el("td", { class: "muted" }, r.last_seen ? ago(r.age_s) : "nunca"));
    });
    const table = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Conteos")),
        el("th", { scope: "col" }, "Tienda"), el("th", { scope: "col" }, "Estado"),
        el("th", { scope: "col", title: "Peor puntuación de imagen del día (0-100)" }, "Imagen"),
        el("th", { scope: "col", title: "Cámaras graves / con aviso" }, "Cámaras"),
        el("th", { scope: "col", title: "Días de grabación que caben en el disco" }, "Previsión"),
        el("th", { scope: "col" }, "Qué pasa"), el("th", { scope: "col" }, "Último latido"))),
      el("tbody", {}, rows.length ? rows : [el("tr", {}, el("td", { colspan: "8", class: "muted" },
        "No hay tiendas dadas de alta. Las tiendas aparecen aquí cuando envían su primer latido."))]));
    clear($("table")).append(table);
  }

  function downloadCounts() {
    const ids = selected.size ? [...selected] : [...document.querySelectorAll("#table a[href^='/sites/']")]
      .map((a) => decodeURIComponent(a.getAttribute("href").split("/")[2]));
    if (!ids.length) {
      showMsg($("error"), "No hay tiendas para exportar.");
      return;
    }
    const p = new URLSearchParams({ sites: ids.join(","), bucket: $("c-bucket").value });
    if ($("c-from").value) p.set("from", $("c-from").value);
    if ($("c-to").value) p.set("to", $("c-to").value);
    window.location.href = `/api/counts.csv?${p}`;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    await topbar("ops");
    $("refresh").addEventListener("click", load);
    $("day").addEventListener("change", load);
    $("print").addEventListener("click", () => window.print());
    $("c-download").addEventListener("click", downloadCounts);
    await load();
  });
})();
