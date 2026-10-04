# `infra/update-worker/`

**Dueño:** B4.

Worker de Cloudflare (TypeScript, sin dependencias) que sirve `metadata/` libre y `targets/` con token de sede validado contra el KV `site_tokens`. Se prueba con Miniflare; no se despliega hasta tener la cuenta (decisión D5).

**Referencia:** CONTRATO §15.5. Pruebas: sin token → 401; token revocado → 401; token de otro cliente → 403.
