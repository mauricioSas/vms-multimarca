// Estado de las actualizaciones y vuelta atrás manual (PLAN-V2 §2.5 paso 9). Las dos acciones lanzan
// «vmsctl update …» con elevación (UAC): sin credenciales de administrador no se puede.
import { $, invocar, mostrarError, texto } from "./comun.js";

async function cargar() {
  try {
    const a = await invocar("actualizaciones");
    texto("etiqueta", a.etiqueta);
    texto("visor", a.version_visor);
    const st = a.estado || {};
    texto("instalada", st.installed || "—");
    texto("canal", st.channel || "—");
    texto("ultima", st.last_check || "—");
    $("buscar").disabled = !a.puede_actuar;
    $("volver").disabled = !a.puede_actuar;
    if (!a.puede_actuar) texto("res-buscar", "Disponible solo en un equipo con el programa instalado (Windows).");
  } catch (err) {
    mostrarError("error", err);
  }
}

async function accion(boton, salida, comando, args) {
  $(boton).disabled = true;
  texto(salida, "Esperando la confirmación de Windows…");
  try {
    const r = await invocar(comando, args);
    texto(salida, r.mensaje);
    $(salida).className = r.ok ? "ok" : "error";
  } catch (err) {
    texto(salida, err.message);
    $(salida).className = "error";
  } finally {
    $(boton).disabled = false;
    setTimeout(cargar, 3000);
  }
}

async function novedades() {
  texto("novedad-texto", "Buscando versiones nuevas…");
  $("instalar").hidden = true;
  $("notas").hidden = true;
  try {
    const n = await invocar("novedades");
    texto("novedad-texto", n.mensaje);
    if (n.novedad) {
      const mb = Math.round(n.novedad.tamano / 1048576);
      texto("novedad-titulo", `Versión nueva: ${n.novedad.version}`);
      texto("novedad-texto", `${n.mensaje} Descarga de unos ${mb} MB.`);
      $("instalar").hidden = !n.puede_instalar;
      texto("notas", `Novedades: ${n.novedad.notas_url}`);
      $("notas").hidden = false;
    } else {
      texto("novedad-titulo", "Versión nueva");
    }
  } catch (err) {
    texto("novedad-texto", err.message);
  }
}

$("comprobar").addEventListener("click", novedades);
$("instalar").addEventListener("click", async () => {
  if (!confirm("¿Descargar e instalar la versión nueva? La grabación se detendrá unos minutos mientras se instala.")) return;
  $("instalar").disabled = true;
  $("comprobar").disabled = true;
  texto("res-instalar", "Descargando y comprobando la versión nueva… (puede tardar unos minutos). Después Windows pedirá permiso.");
  $("res-instalar").className = "";
  try {
    const r = await invocar("instalar_novedad");
    texto("res-instalar", r.mensaje);
    $("res-instalar").className = r.ok ? "ok" : "error";
  } catch (err) {
    texto("res-instalar", err.message);
    $("res-instalar").className = "error";
  } finally {
    $("instalar").disabled = false;
    $("comprobar").disabled = false;
  }
});
novedades();

$("buscar").addEventListener("click", () => accion("buscar", "res-buscar", "buscar_actualizaciones", {}));
$("volver").addEventListener("click", () => {
  if (!confirm("¿Volver a la versión anterior? Se perderán los cambios de configuración hechos desde la última actualización.")) return;
  accion("volver", "res-volver", "volver_version_anterior", { motivo: $("motivo").value });
});
cargar();
setInterval(cargar, 10000);
