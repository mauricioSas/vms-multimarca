// #counts-export-root (analytics.html): descarga CSV de conteos para Excel (separador «;», coma decimal,
// UTF-8 con BOM). Solo agregados. Dueño: B6. Contrato: CONTRATO §18.15.
import { localDateValue } from "./ui.js";
import { h, me, section } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("counts-export-root");

async function main() {
  if (!root) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk) return;
  const today = localDateValue();
  const weekAgo = localDateValue(new Date(Date.now() - 6 * 86400000));
  const from = h("input", { class: "input", type: "date", value: weekAgo, "aria-label": "Desde" });
  const to = h("input", { class: "input", type: "date", value: today, "aria-label": "Hasta" });
  const bucket = h("select", { class: "input", "aria-label": "Agrupar" },
    h("option", { value: "day" }, "Por día"), h("option", { value: "hour" }, "Por hora"));
  const msg = h("p", { class: "small", role: "status" });
  const link = h("a", { class: "btn btn-primary", href: "#", download: "" }, "Descargar CSV");
  const update = () => {
    link.href = `/api/analytics/counts.csv?from=${encodeURIComponent(from.value)}&to=${encodeURIComponent(to.value)}&bucket=${bucket.value}`;
  };
  for (const el of [from, to, bucket]) el.addEventListener("change", update);
  update();
  link.addEventListener("click", async (e) => {
    // Se comprueba antes para dar un mensaje claro (sin base de datos, fechas mal…) en vez de un archivo de error.
    e.preventDefault();
    msg.textContent = "Preparando…";
    try {
      const r = await fetch(link.href, { credentials: "same-origin", headers: { "X-Requested-With": "vms" } });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        msg.textContent = (data.error && data.error.message) || `No se pudo descargar (${r.status}).`;
        return;
      }
      const blob = await r.blob();
      const a = h("a", { href: URL.createObjectURL(blob), download: `conteos_${from.value}_${to.value}.csv` });
      document.body.append(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      msg.textContent = "Descargado.";
    } catch {
      msg.textContent = "No hay conexión con el servidor del VMS.";
    }
  });
  const body = section(root, { id: "h-ops-counts", title: "Descargar conteos para Excel",
    subtitle: "Entradas, salidas y cola media y máxima. Solo agregados, sin imágenes." });
  body.append(h("div", { class: "card card-pad" }, h("div", { class: "ops-row" },
    h("label", { class: "field" }, h("span", {}, "Desde"), from), h("label", { class: "field" }, h("span", {}, "Hasta"), to),
    h("label", { class: "field" }, h("span", {}, "Agrupar"), bucket), link), msg));
}

main();
