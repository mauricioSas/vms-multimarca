// Utilidades de las páginas locales del visor. Solo estas páginas (tauri://localhost o http://tauri.localhost)
// pueden llamar a los comandos del visor; las del servicio no tienen IPC (capabilities/local.json).

/** Llama a un comando del visor y devuelve su resultado (o lanza Error con el mensaje en español). */
export async function invocar(comando, args = {}) {
  const ipc = window.__TAURI_INTERNALS__;
  if (!ipc || typeof ipc.invoke !== "function") {
    throw new Error("Esta página solo funciona dentro del visor de VMS Multimarca.");
  }
  try {
    return await ipc.invoke(comando, args);
  } catch (err) {
    throw new Error(typeof err === "string" ? err : (err && err.message) || "Error desconocido");
  }
}

/** Atajo de document.getElementById. */
export const $ = (id) => document.getElementById(id);

/** Pone texto (nunca HTML) en un elemento. */
export function texto(id, valor) {
  const el = $(id);
  if (el) el.textContent = valor == null ? "" : String(valor);
}

/** Crea un elemento con atributos y texto. */
export function el(etiqueta, attrs = {}, contenido = "") {
  const e = document.createElement(etiqueta);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("data-")) e.setAttribute(k, v);
    else e[k] = v;
  }
  if (contenido instanceof Node) e.append(contenido);
  else if (contenido !== "") e.textContent = String(contenido);
  return e;
}

/** Parámetro de la URL de la página (p. ej. «ventana»). */
export function parametro(nombre) {
  return new URLSearchParams(location.search).get(nombre) || "";
}

/** Muestra un error en un elemento (o en la consola si no existe). */
export function mostrarError(id, err) {
  const msg = err && err.message ? err.message : String(err);
  const e = $(id);
  if (e) {
    e.hidden = false;
    e.textContent = msg;
  } else {
    console.error(msg);
  }
}
