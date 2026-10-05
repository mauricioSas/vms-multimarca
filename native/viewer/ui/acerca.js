import { invocar, texto } from "./comun.js";

try {
  const a = await invocar("acerca");
  texto("producto", a.producto);
  texto("version", a.version);
  texto("avisos", a.avisos);
  texto("ruta", a.ruta_avisos ? `Archivo: ${a.ruta_avisos}` : "");
} catch (err) {
  texto("avisos", err.message);
}
