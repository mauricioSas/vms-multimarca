# Driver.js 1.9.0 (copia local)

**Dueño:** B6 (recorridos guiados del onboarding, CONTRATO §18.14).

| Dato | Valor |
|---|---|
| Proyecto | [nilbuild/driver.js](https://github.com/nilbuild/driver.js) (antes `kamranahmedse/driver.js`) |
| Versión | 1.9.0 (npm, publicada el 03-10-2026) |
| Licencia | MIT, texto completo en [`LICENSE`](LICENSE) (Copyright (c) Kamran Ahmed) |
| Origen | `https://registry.npmjs.org/driver.js/-/driver.js-1.9.0.tgz` |
| Integridad del paquete (npm `dist.integrity`, comprobada al copiar) | `sha512-+YWVdNVUrY1/2spszQshohWYY4soIjNBnMFS6lfmousT8igos2lmswhlqyuuNMhr+qbV0EjJ+L6i2FECPr6Ygg==` |
| Archivos copiados sin cambios | `dist/driver.js.mjs` → `driver.js.mjs`, `dist/driver.css` → `driver.css`, `license` → `LICENSE` |

Se sirve desde el propio backend en `/vendor/driver.js/`: nunca desde un CDN, así funciona en una red
sin salida a Internet y cumple la CSP (`script-src 'self'`). No tiene dependencias.

Descartadas por licencia: Shepherd.js e Intro.js (AGPL-3.0 + licencia comercial de pago).

Para actualizarla: descargar el `.tgz` de npm, comprobar `dist.integrity` (`npm view driver.js@X dist.integrity`)
y la licencia, copiar los mismos tres archivos y cambiar esta tabla y el aviso de terceros.
