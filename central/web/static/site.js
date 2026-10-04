"use strict";
(async () => {
  const { api, el, clear, num, num1, ago, duration, stateBadge, delta, barChart, topbar, showMsg, fmtDate } = VMS;
  const siteId = decodeURIComponent(location.pathname.split("/")[2] || "");
  const base = "/api/sites/" + encodeURIComponent(siteId);
  const errBox = document.getElementById("error");
  let range = "today";
  let tz = undefined;
  await topbar("site");

  function tile(label, value, hint) {
    return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value),
      hint ? el("div", { class: "hint" }, hint) : null);
  }
  const yesNo = (v) => (v === true ? "Sí" : v === false ? "No" : "—");

  async function loadSite() {
    const s = await api(base);
    tz = s.timezone;
    document.title = s.name + " · Panel central";
    document.getElementById("title").textContent = s.name;
    clear(document.getElementById("badge")).append(stateBadge(s.state));
    document.getElementById("meta").textContent =
      [s.code, s.hostname, s.version && "v" + s.version, "última señal " + ago(s.age_s)].filter(Boolean).join(" · ");
    clear(document.getElementById("tiles")).append(
      tile("Entradas hoy", num(s.today.in), `${num(s.today.out)} salidas`),
      tile("Entradas esta semana", num(s.week.in), el("span", {}, "vs. semana anterior ", delta(s.week.in, s.week.prev_in))),
      tile("Alertas de cola hoy", num(s.today.alerts), `${num(s.alerts_open)} abiertas ahora`),
      tile("Cámaras con vídeo", s.cameras_total == null ? "—" : `${num(s.cameras_online)} / ${num(s.cameras_total)}`,
        s.engine && s.engine.restarts ? `Motor reiniciado ${s.engine.restarts} veces` : "Motor de vídeo"),
      tile("Disco", s.disk_percent == null ? "—" : num1(s.disk_percent) + " %",
        s.disk_free_gb == null ? "" : num1(s.disk_free_gb) + " GB libres"),
      tile("Analítica", s.analytics_running == null ? "—" : s.analytics_running ? "Activa" : "Parada",
        s.temperature_c == null ? "" : "Temperatura " + num1(s.temperature_c) + " °C"));

    clear(document.getElementById("cameras")).append(s.cameras.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Cámara"), el("th", {}, "Vídeo"), el("th", {}, "Grabando"))),
      el("tbody", {}, s.cameras.map((c) => el("tr", {}, el("td", {}, c.name),
        el("td", {}, c.online === false ? stateBadge("down") : yesNo(c.online)), el("td", {}, yesNo(c.recording))))))
      : el("div", { class: "empty" }, "La sede todavía no ha enviado su lista de cámaras"));

    clear(document.getElementById("rules")).append(s.rules.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Regla"), el("th", {}, "Tipo"), el("th", {}, "Cámara"), el("th", {}, "Activa"))),
      el("tbody", {}, s.rules.map((r) => el("tr", {}, el("td", {}, r.name),
        el("td", {}, r.kind === "line" ? "Línea de puerta" : "Zona de cola"), el("td", {}, r.camera_name || r.camera_id),
        el("td", {}, yesNo(r.active)))))) : el("div", { class: "empty" }, "Sin reglas de analítica"));
  }

  function label(iso, bucket) {
    return bucket === "hour" ? fmtDate(iso, tz, { hour: "2-digit", minute: "2-digit" })
      : fmtDate(iso, tz, { weekday: "short", day: "numeric" });
  }

  async function loadSeries() {
    const q = "?range=" + range;
    const [counts, occ, alerts] = await Promise.all([
      api(base + "/counts" + q), api(base + "/occupancy" + q), api(base + "/queue-alerts" + q)]);
    document.getElementById("counts-title").textContent = `Entradas (${num(counts.total.in)} en total)`;
    barChart(document.getElementById("counts"), counts.series.map((p) => ({
      label: label(p.start, counts.bucket), value: p.in,
      title: `${fmtDate(p.start, tz)}: ${num(p.in)} entradas, ${num(p.out)} salidas` })),
      { ariaLabel: "Entradas por tramo", valueLabel: "Entradas" });
    barChart(document.getElementById("occupancy"), occ.series.map((p) => ({
      label: label(p.start, occ.bucket), value: p.avg_people || 0,
      title: p.minutes ? `${fmtDate(p.start, tz)}: media ${num1(p.avg_people)}, máximo ${num(p.max_people)}, ` +
        `${duration(p.seconds_over_threshold)} por encima del umbral` : `${fmtDate(p.start, tz)}: sin datos` })),
      { ariaLabel: "Ocupación media de la cola", valueLabel: "Personas" });
    clear(document.getElementById("alerts")).append(alerts.alerts.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Inicio"), el("th", {}, "Fin"), el("th", { class: "num" }, "Duración"),
        el("th", {}, "Zona"), el("th", { class: "num" }, "Pico"), el("th", { class: "num" }, "Umbral"), el("th", {}, "Aviso"))),
      el("tbody", {}, alerts.alerts.map((a) => el("tr", {},
        el("td", {}, fmtDate(a.started_at, tz)), el("td", {}, a.ended_at ? fmtDate(a.ended_at, tz) : "En curso"),
        el("td", { class: "num" }, duration(a.duration_s)),
        el("td", {}, a.rule_name || a.rule_id, a.camera_name ? el("span", { class: "muted" }, " · " + a.camera_name) : null),
        el("td", { class: "num" }, num(a.peak_people)), el("td", { class: "num" }, num(a.threshold)),
        el("td", {}, a.notify_failed ? "Falló el envío" : a.notified_at ? "Enviado" : "Pendiente")))))
      : el("div", { class: "empty" }, "Ninguna alerta de cola en este periodo"));
  }

  async function loadReports() {
    const reports = await api(base + "/reports");
    clear(document.getElementById("reports")).append(reports.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Semana del"), el("th", {}, "Estado"), el("th", {}, "Generado"))),
      el("tbody", {}, reports.map((r) => el("tr", {},
        el("td", {}, el("a", { href: `/sites/${encodeURIComponent(siteId)}/reports/${r.week_start}` }, r.week_start)),
        el("td", {}, r.status === "ok" ? "Correcto" : "Solo cifras (sin redacción)"),
        el("td", {}, fmtDate(r.generated_at, tz))))))
      : el("div", { class: "empty" }, "Todavía no hay informes semanales"));
  }

  document.getElementById("range").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-range]");
    if (!b) return;
    range = b.dataset.range;
    for (const x of document.querySelectorAll("#range button")) x.setAttribute("aria-pressed", String(x === b));
    loadSeries().catch((e) => showMsg(errBox, e.message));
  });

  async function refresh() {
    try {
      await loadSite();
      await Promise.all([loadSeries(), loadReports()]);
      errBox.className = "msg";
    } catch (e) { showMsg(errBox, e.message); }
  }
  await refresh();
  setInterval(refresh, 60000);
})();
