// Aviso de certificado (fijación de la huella, PLAN-V2 §2.3). Los datos vienen del visor, no de la URL.
import { $, invocar, mostrarError, texto } from "./comun.js";

let cert = null;

function ultimos4(huella) {
  return huella.replace(/[^0-9A-F]/gi, "").slice(-4).toUpperCase();
}

async function cargar() {
  $("error").hidden = true;
  try {
    cert = await invocar("certificado");
  } catch (err) {
    mostrarError("error", err);
    return;
  }
  if (cert.primera_vez) {
    $("primera").hidden = false;
    $("cambiado").hidden = true;
    texto("p-servidor", cert.servidor);
    texto("p-url", cert.url);
    texto("p-observada", cert.observada);
    $("confiar").textContent = "La huella coincide: confiar en este servidor";
    $("confiar").className = "principal";
    $("confiar").disabled = false;
  } else {
    document.title = "Certificado cambiado · VMS Multimarca";
    $("cambiado").hidden = false;
    $("primera").hidden = true;
    texto("c-servidor", cert.servidor);
    texto("c-url", cert.url);
    texto("c-esperada", cert.esperada || "—");
    texto("c-observada", cert.observada);
    $("nota-confiar").hidden = false;
    $("confiar").disabled = true;
  }
}

$("ultimos").addEventListener("input", () => {
  $("confiar").disabled = !cert || $("ultimos").value.trim().toUpperCase() !== ultimos4(cert.observada);
});

$("confiar").addEventListener("click", async () => {
  if (!cert) return;
  $("confiar").disabled = true;
  try {
    await invocar("confiar_certificado", { huella: cert.observada });
  } catch (err) {
    mostrarError("error", err);
    $("confiar").disabled = false;
  }
});

$("reintentar").addEventListener("click", async () => {
  try {
    const r = await invocar("reintentar");
    if (r.estado === "certificado") await cargar();
  } catch (err) {
    mostrarError("error", err);
  }
});

cargar();
