// «Sin permiso para abrir los muros» (CONTRATO §17.1). Si se arregla el permiso, el visor abre el muro solo.
import { $, invocar, texto } from "./comun.js";

const CADA_MS = 30000;
let temporizador = null;

async function cargar() {
  try {
    const d = await invocar("sin_permiso");
    texto("titulo", d.muro ? `Muro ${d.muro}: ${d.titulo}` : d.titulo);
    document.title = `${d.titulo} · VMS Multimarca`;
    texto("mensaje", d.mensaje);
    texto("grupo", d.grupo);
    texto("archivo", d.archivo);
  } catch (err) {
    texto("mensaje", err.message);
  }
}

async function comprobar() {
  clearTimeout(temporizador);
  try {
    await invocar("reintentar");
  } catch (err) {
    console.warn(err);
  }
  temporizador = setTimeout(comprobar, CADA_MS);
}

$("ahora").addEventListener("click", comprobar);
cargar();
temporizador = setTimeout(comprobar, CADA_MS);
