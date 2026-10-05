import { $, el, invocar, mostrarError, texto } from "./comun.js";

let datos = null;
let probado = null; // {nombre, url, huella}

function uso(s) {
  const partes = [];
  if (s.panel) partes.push("panel");
  if (s.muros.length) partes.push(`muro${s.muros.length > 1 ? "s" : ""} ${s.muros.join(", ")}`);
  return partes.join(" · ") || "—";
}

function pintar(d) {
  datos = d;
  const tbody = $("lista");
  tbody.replaceChildren();
  for (const s of d.servidores) {
    const dir = el("td");
    dir.append(el("div", { class: "mono" }, s.url));
    if (s.huella) dir.append(el("div", { class: "mono small muted" }, s.huella));
    else if (s.remoto) dir.append(el("div", { class: "small error" }, "Sin huella confirmada"));
    const acc = el("td");
    if (s.borrable) {
      const b = el("button", { type: "button", class: "peligro" }, "Quitar");
      b.addEventListener("click", () => borrar(s.nombre));
      acc.append(b);
    }
    const tr = el("tr");
    tr.append(el("td", {}, s.nombre), dir, el("td", {}, uso(s)), acc);
    tbody.append(tr);
  }
  const caja = $("asignaciones");
  caja.replaceChildren();
  const opciones = d.servidores.map((s) => s.nombre);
  const selector = (id, etiqueta, actual, muro) => {
    const wrap = el("div");
    wrap.append(el("label", { htmlFor: id }, etiqueta));
    const sel = el("select", { id });
    for (const n of opciones) sel.append(el("option", { value: n, selected: n.toLowerCase() === actual.toLowerCase() }, n));
    sel.addEventListener("change", () => asignar(muro, sel.value));
    wrap.append(sel);
    return wrap;
  };
  caja.append(selector("asig-panel", "Panel", d.panel, 0));
  for (const [muro, servidor] of d.muros) caja.append(selector(`asig-muro-${muro}`, `Muro ${muro}`, servidor, muro));
}

async function cargar() {
  try {
    pintar(await invocar("servidores"));
  } catch (err) {
    mostrarError("error", err);
  }
}

async function probar(ev) {
  ev.preventDefault();
  $("error").hidden = true;
  $("guardar").disabled = true;
  probado = null;
  $("resultado").hidden = false;
  texto("res-mensaje", "Probando…");
  $("res-huella-caja").hidden = true;
  try {
    const r = await invocar("probar_servidor", { url: $("url").value });
    $("url").value = r.url;
    texto("res-mensaje", r.responde ? r.mensaje : `No responde: ${r.mensaje}`);
    $("res-mensaje").className = r.responde ? "" : "error";
    if (r.huella) {
      $("res-huella-caja").hidden = false;
      texto("res-huella", r.huella);
    }
    if (r.responde) {
      probado = { nombre: $("nombre").value.trim(), url: r.url, huella: r.huella || null };
      $("guardar").disabled = false;
      $("guardar").textContent = r.huella ? "La huella coincide: guardar" : "Guardar";
    }
  } catch (err) {
    texto("res-mensaje", "");
    mostrarError("error", err);
  }
}

async function guardar() {
  if (!probado) return;
  try {
    const nombre = $("nombre").value.trim() || probado.nombre;
    pintar(await invocar("guardar_servidor", { nombre, url: probado.url, huella: probado.huella }));
    $("form").reset();
    $("resultado").hidden = true;
    probado = null;
  } catch (err) {
    mostrarError("error", err);
  }
}

async function borrar(nombre) {
  if (!confirm(`¿Quitar el servidor «${nombre}»? Sus muros pasarán al servidor local.`)) return;
  try {
    pintar(await invocar("borrar_servidor", { nombre }));
  } catch (err) {
    mostrarError("error", err);
  }
}

async function asignar(muro, servidor) {
  try {
    pintar(await invocar("asignar_servidor_muro", { muro, servidor }));
  } catch (err) {
    mostrarError("error", err);
    if (datos) pintar(datos);
  }
}

$("form").addEventListener("submit", probar);
$("guardar").addEventListener("click", guardar);
$("url").addEventListener("input", () => {
  $("guardar").disabled = true;
  probado = null;
});
cargar();
