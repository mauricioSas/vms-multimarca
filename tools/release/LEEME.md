# `tools/release/`

**Dueño:** B4. **Guía completa:** [`docs/PUBLICAR-VERSION.md`](../../docs/PUBLICAR-VERSION.md).

Herramientas del PC de publicación (`python -m tools.release --help`). No se distribuyen en el instalador.

| Archivo | Qué hace |
|---|---|
| `keys.py` | Llavero (`keyring.json`, fuera del repositorio): `file2:` (desarrollo), `hsm:` (PKCS#11: SoftHSM o YubiKey) y `envpem:` (secretos de CI, solo snapshot/timestamp). `keys init-dev` |
| `tuf_repo.py` | Repositorios TUF `online` y `offline` (consistent snapshot, `root` 2 de 3 ECDSA P-256, `targets` ECDSA P-256, `snapshot`/`timestamp` ed25519; `offline` caduca a 60 días) |
| `package.py` | Zips de componente reproducibles con `MANIFEST.sha256` (mismo formato que comprueba el actualizador) |
| `publish.py` | `publish` (descriptor + componentes, herencia de lo que no cambia, migraciones no reversibles, `--dry-run`), canales y tabla de avisos |
| `channel.py` | Canales `pilot`/`stable`: mover, pausar, reanudar |
| `mirror.py` | Espejo USB o carpeta compartida (60 días) |
| `verify.py` | Valida un repositorio con el cliente real (`ngclient`) por HTTP o `file://` |
| `site_token.py` | Tokens de sede del Worker (KV de Cloudflare o `--kv-file` local) |
| `ci/` | Plantillas de `publish-meta.yml` y `timestamp.yml` (las instala B3 en `.github/workflows/`) y su llavero público |

Pruebas: `tests/updater/test_release_tools.py`, `test_key_compromise.py` y, con SoftHSM2 instalado, la firma PKCS#11
de punta a punta (`publish --dry-run`, criterio 3 de B4).
