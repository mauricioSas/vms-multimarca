"use strict";
// Sustituto de sharp para las pruebas del Worker (ver package.json de la carpeta).
module.exports = function sharp() {
  throw new Error("sharp no está disponible en las pruebas del Worker de actualizaciones");
};
