import { $, el, invocar, mostrarError, parametro, texto } from "./comun.js";

const CLAVE = {
  legible: "Se puede leer (los muros entran sin contraseña)",
  sin_permiso: "Sin permiso: tu usuario no está en el grupo «VMS Operadores»",
  sin_token: "No existe en este equipo",
  token_invalido: "Está dañada",
  error_lectura: "No se pudo leer",
};

const RESULTADO = {
  update_ok: "La última actualización terminó bien",
  update_failed: "La última actualización falló y se volvió a la versión anterior",
  metadata_expired: "La información de versiones está caducada",
  clock_skew: "La hora del equipo no es correcta",
  none: "Sin novedades",
};

const volver = parametro("volver");
if (volver && /^[a-z-]+\.html(\?[\w=&%.-]*)?$/.test(volver)) {
  $("volver").href = volver;
  $("volver").hidden = false;
}

function fila(dl, nombre, valor) {
  if (valor === null || valor === undefined || valor === "") return;
  dl.append(el("dt", {}, nombre), el("dd", {}, valor));
}

async function cargar() {
  $("error").hidden = true;
  try {
    const d = await invocar("diagnostico");
    texto("visor-version", d.version_visor);
    texto("instalacion", d.instalacion || "Copia de desarrollo (sin instalar)");
    texto("datos", d.carpeta_datos);
    texto("visor-json", d.visor_json);
    texto("clave", CLAVE[d.clave_muros] || d.clave_muros);
    const s = d.servidor;
    const punto = $("punto");
    if (!s) {
      punto.className = "punto mal";
      texto("srv-estado", "No hay servidor configurado para el panel");
    } else {
      texto("srv-nombre", s.nombre);
      texto("srv-url", s.url);
      texto("srv-version", s.version || "—");
      texto("srv-detalle", s.detalle || "—");
      const estados = { ok: ["ok", "En marcha"], degraded: ["espera", "En marcha, con avisos"], down: ["mal", "Caído"] };
      const [clase, txt] = s.responde ? (estados[s.estado] || ["espera", s.estado]) : ["mal", "No responde"];
      punto.className = `punto ${clase}`;
      texto("srv-estado", txt);
    }
    texto("act-etiqueta", d.estado_actualizaciones);
    const dl = $("act-datos");
    dl.replaceChildren();
    const a = d.actualizaciones;
    if (a) {
      fila(dl, "Instalada", a.installed);
      fila(dl, "Canal", a.channel);
      fila(dl, "Estado", a.state);
      fila(dl, "Resultado", RESULTADO[a.last_result] || a.last_result);
      fila(dl, "Mensaje", a.message_es);
      fila(dl, "Última comprobación", a.last_check);
      fila(dl, "Disponible", a.available);
      if (a.reboot_pending) fila(dl, "Windows", "Tiene un reinicio pendiente: la actualización espera");
    } else {
      fila(dl, "Archivo", "No existe todavía (el actualizador no ha escrito su estado)");
    }
  } catch (err) {
    mostrarError("error", err);
  }
}

$("actualizar").addEventListener("click", cargar);
cargar();
