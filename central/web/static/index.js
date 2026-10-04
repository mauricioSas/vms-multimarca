"use strict";
(async () => {
  const { api, el, clear, num, num1, ago, duration, stateBadge, delta, hbarChart, topbar, showMsg, fmtDate } = VMS;
  const errBox = document.getElementById("error");
  let range = "last7";
  await topbar("index");

  function tile(label, value, hint) {
    return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value),
      hint ? el("div", { class: "hint" }, hint) : null);
  }

  async function loadSites() {
    const data = await api("/api/sites");
    const sites = data.sites;
    const online = sites.filter((s) => s.online).length;
    const down = sites.filter((s) => s.state === "down" || s.state === "unknown").length;
    const warn = sites.filter((s) => s.state === "degraded").length;
    const sum = (f) => sites.reduce((a, s) => a + (f(s) || 0), 0);
    clear(document.getElementById("tiles")).append(
      tile("Sedes en línea", `${online} / ${sites.length}`, down ? `${down} sin señal` : "Todas responden"),
      tile("Con avisos", num(warn), "Cámaras sin vídeo, disco o analítica"),
      tile("Entradas hoy", num(sum((s) => s.today.in)), "Suma de todas las sedes"),
      tile("Entradas esta semana", num(sum((s) => s.week.in)), "Desde el lunes"),
      tile("Alertas de cola hoy", num(sum((s) => s.today.alerts)), `${num(sum((s) => s.alerts_open))} abiertas ahora`));

    const box = clear(document.getElementById("sites"));
    if (!sites.length) {
      box.append(el("div", { class: "empty" }, "Todavía no hay sedes. Aparecen solas cuando envían su primer latido."));
    } else {
      box.append(el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Estado"), el("th", {}, "Sede"), el("th", {}, "Última señal"), el("th", { class: "num" }, "Cámaras"),
          el("th", { class: "num" }, "Disco"), el("th", { class: "num" }, "Temp."), el("th", { class: "num" }, "Entradas hoy"),
          el("th", { class: "num" }, "Semana"), el("th", {}, "vs. semana ant."), el("th", { class: "num" }, "Alertas hoy"),
          el("th", {}, "Versión"), el("th", {}, "Informe"))),
        el("tbody", {}, sites.map((s) => el("tr", {},
          el("td", {}, stateBadge(s.state)),
          el("td", {}, el("a", { href: "/sites/" + encodeURIComponent(s.site_id) }, s.name),
            s.code ? el("span", { class: "muted" }, " · " + s.code) : null),
          el("td", { title: fmtDate(s.last_seen, s.timezone) }, ago(s.age_s)),
          el("td", { class: "num" }, s.cameras_total == null ? "—" : `${num(s.cameras_online)} / ${num(s.cameras_total)}`),
          el("td", { class: "num" }, s.disk_percent == null ? "—" : num1(s.disk_percent) + " %"),
          el("td", { class: "num" }, s.temperature_c == null ? "—" : num1(s.temperature_c) + " °C"),
          el("td", { class: "num" }, num(s.today.in)),
          el("td", { class: "num" }, num(s.week.in)),
          el("td", {}, delta(s.week.in, s.week.prev_in)),
          el("td", { class: "num" }, num(s.today.alerts)),
          el("td", {}, s.version || "—"),
          el("td", {}, s.last_report_week
            ? el("a", { href: `/sites/${encodeURIComponent(s.site_id)}/reports/${s.last_report_week}` }, s.last_report_week)
            : el("span", { class: "muted" }, "—")))))));
    }
    document.getElementById("updated").textContent = "Actualizado " + fmtDate(data.generated_at, undefined, { timeStyle: "medium" });
  }

  async function loadCompare() {
    const [cmp, top] = await Promise.all([
      api("/api/compare?range=" + range), api("/api/queues/top?limit=10&range=" + range)]);
    hbarChart(document.getElementById("compare"),
      cmp.sites.map((s) => ({ label: s.name, value: s.entries, title: `${s.name}: ${num(s.entries)} entradas` })),
      { ariaLabel: "Entradas por sede", valueLabel: "Entradas", empty: "Sin conteos en este periodo" });

    clear(document.getElementById("compare-table")).append(cmp.sites.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Sede"), el("th", { class: "num" }, "Entradas"),
        el("th", { class: "num" }, "Salidas"), el("th", { class: "num" }, "Alertas de cola"),
        el("th", { class: "num" }, "Tiempo en alerta"), el("th", { class: "num" }, "Cola media"),
        el("th", { class: "num" }, "Cola máx."))),
      el("tbody", {}, cmp.sites.map((s) => el("tr", {},
        el("td", {}, el("a", { href: "/sites/" + encodeURIComponent(s.site_id) }, s.name)),
        el("td", { class: "num" }, num(s.entries)), el("td", { class: "num" }, num(s.exits)),
        el("td", { class: "num" }, num(s.alerts)), el("td", { class: "num" }, duration(s.alert_seconds)),
        el("td", { class: "num" }, num1(s.avg_people)), el("td", { class: "num" }, num(s.max_people))))))
      : el("div", { class: "empty" }, "Sin sedes activas"));

    clear(document.getElementById("queues")).append(top.queues.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Sede"), el("th", {}, "Zona"), el("th", { class: "num" }, "Alertas"),
        el("th", { class: "num" }, "Tiempo total"), el("th", { class: "num" }, "Pico"), el("th", {}, "Última"))),
      el("tbody", {}, top.queues.map((q) => el("tr", {},
        el("td", {}, el("a", { href: "/sites/" + encodeURIComponent(q.site_id) }, q.site_name)),
        el("td", {}, q.rule_name || q.rule_id, q.camera_name ? el("span", { class: "muted" }, " · " + q.camera_name) : null),
        el("td", { class: "num" }, num(q.alerts)), el("td", { class: "num" }, duration(q.alert_seconds)),
        el("td", { class: "num" }, num(q.peak_people)), el("td", {}, fmtDate(q.last_alert))))))
      : el("div", { class: "empty" }, "Ninguna alerta de cola en este periodo"));
  }

  async function refresh() {
    try {
      await Promise.all([loadSites(), loadCompare()]);
      errBox.className = "msg";
    } catch (e) {
      showMsg(errBox, e.message);
    }
  }

  document.getElementById("range").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-range]");
    if (!b) return;
    range = b.dataset.range;
    for (const x of document.querySelectorAll("#range button")) x.setAttribute("aria-pressed", String(x === b));
    loadCompare().catch((e) => showMsg(errBox, e.message));
  });
  document.getElementById("refresh").addEventListener("click", refresh);
  await refresh();
  setInterval(refresh, 30000);
})();
