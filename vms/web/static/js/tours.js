// Recorridos guiados con Driver.js 1.9.0 (MIT, copia local en /vendor/driver.js/, sin CDN). Dueño: B6.
// Solo para administradores (quien configura) y NUNCA en los muros (/wall/N): un operador no puede tener una
// ventana tapando el vídeo. Lo visto se guarda por usuario en /api/onboarding/state.
import { get, put } from "./api.js";
import { me } from "./ops-common.js";

const TOURS = {
  panel: {
    page: "/",
    steps: [
      { element: "#btn-discover", popover: { title: "Buscar en la red", description: "Encuentra las cámaras y grabadores conectados a la misma red que este PC." } },
      { element: "#btn-add-device", popover: { title: "Añadir equipo", description: "Da de alta un equipo escribiendo su IP, usuario y contraseña. Usa un usuario de solo lectura creado para el VMS." } },
      { element: "#sec-devices", popover: { title: "Equipos", description: "Aquí ves si cada equipo responde. «Probar» comprueba la conexión y «¿Por qué no conecta?» te dice qué falla." } },
      { element: "#sec-cameras", popover: { title: "Cámaras", description: "Cada cámara de cada equipo: si llega vídeo y si se graba." } },
      { element: "#sec-monitors", popover: { title: "Monitores", description: "Arrastra cámaras a las celdas de cada monitor y pulsa «Guardar monitor»." } },
      { element: ".help-global", popover: { title: "Ayuda", description: "Si dudas, pulsa «Ayuda» o el «?» de cada sección: explica cada botón con palabras sencillas." } },
    ],
  },
  monitores: {
    page: "/",
    steps: [
      { element: "#monitor-tabs", popover: { title: "Elige el monitor", description: "Hay hasta 4 monitores. Cada uno es una pantalla de la tienda." } },
      { element: "#grid-buttons", popover: { title: "Cuántas cámaras", description: "1, 4, 9 o 16 cámaras a la vez en esa pantalla." } },
      { element: "#cam-palette-list", popover: { title: "Arrastra una cámara", description: "Arrástrala desde esta lista a una celda de la derecha." } },
      { element: "#btn-save-wall", popover: { title: "Guarda", description: "El muro cambia solo en unos segundos, sin recargar." } },
      { element: "#btn-open-wall", popover: { title: "Abre el muro", description: "Abre la pantalla del muro para colocarla en el monitor correspondiente." } },
    ],
  },
  reproduccion: {
    page: "/playback",
    steps: [
      { element: "#pb-camera", popover: { title: "Cámara y día", description: "Elige qué cámara y qué día quieres ver." } },
      { element: "#timeline", popover: { title: "Línea de tiempo", description: "Los tramos de color son lo grabado. Pulsa en un punto para ver ese momento. Las marcas de encima son eventos: huecos, marcadores, problemas de imagen…" } },
      { element: ".pb-transport", popover: { title: "Controles", description: "Saltos de 10 s y 1 min, pausa y velocidad." } },
      { element: "#h-ops-bookmarks", popover: { title: "Marcadores", description: "Apunta un momento importante. «Proteger» evita que la retención lo borre (con motivo y caducidad)." } },
      { element: "#h-ops-evidence", popover: { title: "Exportar evidencia", description: "Para la policía: vídeos originales, visor sin instalar nada, acta y firma digital." } },
    ],
  },
  analitica: {
    page: "/analytics",
    steps: [
      { element: "#an-camera", popover: { title: "Elige la cámara", description: "La de la puerta para contar entradas, la de cajas para medir la cola." } },
      { element: "#tool-line", popover: { title: "Línea de puerta", description: "Clic en un extremo y clic en el otro. La flecha ámbar marca hacia dónde es una ENTRADA." } },
      { element: "#tool-zone", popover: { title: "Zona de cola", description: "Un clic por esquina de la zona donde espera la gente. Nunca sobre el puesto de caja." } },
      { element: "#an-rules", popover: { title: "Reglas", description: "Selecciona una para moverla, renombrarla o cambiar el umbral de aviso." } },
    ],
  },
  estado: {
    page: "/status",
    steps: [
      { element: "#st-overall", popover: { title: "Resumen", description: "Si algo va mal, aquí lo verás en palabras de tienda." } },
      { element: "#h-ops-health", popover: { title: "Salud de la imagen", description: "Puntuación de 0 a 100 de cada cámara: tapada, desenfocada, movida… «Fijar referencia» enseña al sistema cómo debe verse." } },
      { element: "#h-ops-clock", popover: { title: "Hora", description: "Que la hora de las cámaras coincida con la del PC: si no, la grabación puede no valer como prueba." } },
      { element: "#h-ops-forecast", popover: { title: "Días de grabación", description: "Cuántos días caben en el disco con lo que se graba de verdad." } },
      { element: "#h-ops-notify", popover: { title: "Avisos", description: "Correo y webhook cuando una cámara se cae o se tapa, agrupados para no saturar." } },
    ],
  },
};

let cssLoaded = false;
function loadCss() {
  if (cssLoaded) return;
  cssLoaded = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/vendor/driver.js/driver.css";
  document.head.append(link);
}

export function toursForPage(path = location.pathname) {
  return Object.entries(TOURS).filter(([, t]) => t.page === path).map(([k]) => k);
}

export async function canTour() {
  if (location.pathname.startsWith("/wall")) return false;
  try {
    const u = await me();
    return u && !u.kiosk && u.role === "admin";
  } catch {
    return false;
  }
}

/** Lanza un recorrido. Si es de otra página, navega allí con ?tour=. */
export async function startTour(name) {
  const tour = TOURS[name];
  if (!tour || !(await canTour())) return false;
  if (tour.page !== location.pathname) {
    location.assign(`${tour.page}?tour=${encodeURIComponent(name)}`);
    return true;
  }
  const steps = tour.steps.filter((s) => {
    const el = document.querySelector(s.element);
    return el && el.getClientRects().length;
  });
  if (!steps.length) return false;
  loadCss();
  const { driver } = await import("/vendor/driver.js/driver.js.mjs");
  const d = driver({
    showProgress: true, progressText: "{{current}} de {{total}}", nextBtnText: "Siguiente", prevBtnText: "Anterior",
    doneBtnText: "Terminar", allowClose: true, steps,
  });
  window.__vmsTour = d;
  d.drive();
  markSeen(name);   // «visto» = mostrado: no se vuelve a proponer aunque se cierre a medias
  return true;
}

async function markSeen(name) {
  try {
    const st = await get("/api/onboarding/state");
    const seen = new Set(st.tours_seen || []);
    seen.add(name);
    await put("/api/onboarding/state", { tours_seen: [...seen] });
  } catch (err) {
    console.warn("No se pudo guardar el recorrido como visto", err);
  }
}

// ?tour=<nombre> en la URL: lanzarlo al cargar la página (cuando la página ya pintó su contenido)
const wanted = new URLSearchParams(location.search).get("tour");
if (wanted && TOURS[wanted]) {
  window.addEventListener("load", () => setTimeout(() => startTour(wanted), 600));
}
