# `infra/update-worker/`

**Dueño:** B4. **Referencia:** CONTRATO §15.5, PLAN-V2 §1.7.

Worker de Cloudflare (TypeScript, **sin dependencias en producción**) delante del bucket R2 `vms-updates`:

| Ruta | Acceso | Respuesta |
|---|---|---|
| `GET /<cliente>/metadata/<archivo>` | libre (los metadatos TUF van firmados) | caché 60 s; `N.root/snapshot/targets.json` inmutables |
| `GET /<cliente>/targets/<ruta>` | `Authorization: Bearer <token de sede>` | sin token, desconocido o revocado → **401**; token de **otro cliente** → **403** |
| `/metadata/…`, `/targets/…` | igual, sin cliente en la ruta (el cliente sale del token) | |
| cualquier otro método | — | 405 |

- Token de sede: `<cliente>.<aleatorio>`. En el KV `SITE_TOKENS` solo está su SHA-256 (`<cliente>:<sha256>` →
  `{"site", "active"}`). Los da de alta y los revoca Unmanned con `python -m tools.release site-token …`; el panel
  central de un cliente nunca tiene credenciales de Cloudflare.
- Cada descarga correcta se registra (cliente, sede, ruta, versión; nunca el token) en el registro del Worker y, si
  está el binding `DOWNLOADS`, en Analytics Engine.
- Todas las respuestas llevan `Date`: el actualizador la usa para detectar el reloj desfasado.
- La fuente del actualizador en una tienda es `VMS_UPDATE_SOURCE=https://updates.<dominio>/<cliente>/`.

## Pruebas (Miniflare, sin cuenta de Cloudflare)

```bash
cd infra/update-worker
npm ci            # miniflare (MIT) + workerd (Apache-2.0) + typescript; sharp se sustituye por un stub vacío
npm test          # 8 casos: metadatos libres, 401/401/403, HEAD, métodos, rutas maliciosas, registro
npm run typecheck
```

`stubs/sharp/` sustituye a `sharp` (lo usa Miniflare solo para el binding de imágenes): así no se instalan las
bibliotecas libvips (LGPL). Nada de esta carpeta se distribuye en el instalador.

Desde pytest, `tests/updater/test_worker_miniflare.py` ejecuta estas pruebas y además una **actualización de punta a
punta**: `scripts/dev-serve.mjs` carga en R2 el repositorio que genera `tools.release` y el actualizador real se
actualiza a través del Worker con un token de sede (y es rechazado con uno revocado o de otro cliente).

## Despliegue (pendiente de D5)

`wrangler.toml` está preparado (bucket, KV, Analytics Engine y ruta `updates.<dominio>/*`) pero **no se despliega**
hasta tener la cuenta. Pasos en `docs/PUBLICAR-VERSION.md`, sección «Servidor de actualizaciones».
