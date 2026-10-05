// «Conectando con el servicio…»: pregunta al visor cada 2 s; cuando el servicio responde, el propio visor lleva
// la ventana a su destino (panel, muro con la entrada de kiosco o aviso de certificado).
import { $, invocar, parametro, texto } from "./comun.js";

const CADA_MS = 2000;
let temporizador = null;
let ocupado = false;

const ventana = parametro("ventana");
const muro = /^muro-(\d)$/.exec(ventana);
if (muro) document.title = `Muro ${muro[1]} · Conectando`;
$("diag").href = `diagnostico.html?volver=${encodeURIComponent(location.pathname.slice(1) + location.search)}`;

function pintar(r) {
  const punto = $("punto");
  if (r.estado === "listo") {
    punto.className = "punto ok";
    texto("titulo", r.titulo || "Abriendo…");
    texto("mensaje", "");
  } else if (r.estado === "error") {
    punto.className = "punto mal";
    texto("titulo", r.titulo);
    texto("mensaje", r.mensaje);
  } else {
    punto.className = "punto espera";
    texto("titulo", muro ? `Muro ${muro[1]}: conectando con el servicio…` : r.titulo || "Conectando con el servicio…");
    texto("mensaje", r.mensaje || "");
  }
  texto("detalle", r.url ? `Servidor «${r.servidor}» · ${r.url}` : "");
}

async function intentar() {
  if (ocupado) return;
  ocupado = true;
  clearTimeout(temporizador);
  try {
    const r = await invocar("reintentar");
    pintar(r);
    if (r.estado === "conectando" || r.estado === "error") temporizador = setTimeout(intentar, CADA_MS);
  } catch (err) {
    pintar({ estado: "error", titulo: "No se pudo comprobar el servicio", mensaje: err.message });
    temporizador = setTimeout(intentar, CADA_MS);
  } finally {
    ocupado = false;
  }
}

$("ahora").addEventListener("click", intentar);
intentar();
