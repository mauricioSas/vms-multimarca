import { $, el, invocar, mostrarError } from "./comun.js";

function nombreMonitor(m, i) {
  const n = m.nombre.replace(/^\\\\\.\\/, "") || "Monitor";
  return `${i + 1} · ${n}`;
}

async function cargar() {
  $("error").hidden = true;
  let d;
  try {
    d = await invocar("monitores");
  } catch (err) {
    mostrarError("error", err);
    return;
  }
  const tm = $("monitores");
  tm.replaceChildren();
  d.monitores.forEach((m, i) => {
    const tr = el("tr");
    tr.append(
      el("td", {}, String(i + 1)),
      el("td", {}, m.nombre || "Monitor"),
      el("td", { class: "mono" }, `${m.x}, ${m.y}`),
      el("td", { class: "mono" }, `${m.ancho} × ${m.alto}`),
      el("td", {}, `${Math.round(m.escala * 100)} %`),
    );
    tm.append(tr);
  });
  const tw = $("muros");
  tw.replaceChildren();
  for (const w of d.muros) {
    const sel = el("select", { id: `monitor-${w.muro}` });
    sel.setAttribute("aria-label", `Monitor del muro ${w.muro}`);
    sel.append(el("option", { value: "" }, "Automático (el monitor del mismo número)"));
    let encontrado = false;
    d.monitores.forEach((m, i) => {
      const actual = m.clave === w.clave;
      encontrado = encontrado || actual;
      sel.append(el("option", { value: m.clave, selected: actual }, nombreMonitor(m, i)));
    });
    if (w.clave && !encontrado) {
      sel.append(el("option", { value: w.clave, selected: true }, "Monitor desconectado"));
    }
    sel.addEventListener("change", async () => {
      try {
        await invocar("asignar_monitor", { muro: w.muro, clave: sel.value || null });
        await cargar();
      } catch (err) {
        mostrarError("error", err);
      }
    });
    const estado = !w.abierto ? "Cerrado" : w.conectado ? "Abierto" : "Oculto: su monitor no está";
    const tr = el("tr");
    tr.append(el("td", {}, `Muro ${w.muro}`), el("td"), el("td", {}, w.servidor), el("td", {}, estado));
    tr.children[1].append(sel);
    tw.append(tr);
  }
}

$("actualizar").addEventListener("click", cargar);
cargar();
