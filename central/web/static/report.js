"use strict";
(async () => {
  const { api, el, clear, markdown, topbar, showMsg, fmtDate } = VMS;
  const parts = location.pathname.split("/");
  const siteId = decodeURIComponent(parts[2] || ""), week = decodeURIComponent(parts[4] || "");
  const errBox = document.getElementById("error");
  await topbar("report");
  document.getElementById("back").href = "/sites/" + encodeURIComponent(siteId);
  try {
    const r = await api(`/api/sites/${encodeURIComponent(siteId)}/reports/${encodeURIComponent(week)}`);
    document.title = `Informe ${r.week_start} · ${r.site_name}`;
    document.getElementById("title").textContent = `Informe semanal · ${r.site_name}`;
    document.getElementById("meta").textContent = `Semana del ${r.week_start} · generado ${fmtDate(r.generated_at)}` +
      (r.provider && r.provider !== "none" ? ` · redacción automática (${r.provider}${r.model ? " " + r.model : ""})` : "");
    const status = clear(document.getElementById("status"));
    if (r.status !== "ok") {
      status.append(el("div", { class: "msg show error" },
        "La redacción automática no está disponible para esta semana; se muestran solo las cifras. ",
        r.error ? "Motivo: " + r.error : ""));
    }
    if (r.body_markdown) markdown(document.getElementById("body"), r.body_markdown);
    else document.getElementById("body").append(el("p", { class: "muted" }, "Sin texto redactado."));
    document.getElementById("metrics").textContent = JSON.stringify(r.metrics, null, 2);
  } catch (e) { showMsg(errBox, e.message); }
})();
