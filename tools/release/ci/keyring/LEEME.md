# Llavero de CI (solo claves públicas)

Aquí irá `keyring.json` de **producción** para `publish-meta.yml` y `timestamp.yml`, cuando se haga la ceremonia de
las llaves (decisión D4, `docs/PUBLICAR-VERSION.md` §2.2). Contendrá:

- `root` y `targets`: **solo la clave pública** (las privadas están en las YubiKey; CI nunca las usa);
- `snapshot` y `timestamp`: `"uri": "envpem:VMS_TUF_SNAPSHOT_PEM"` y `"envpem:VMS_TUF_TIMESTAMP_PEM"` (secretos del
  entorno protegido `tuf-online` de GitHub).

No contiene secretos y se puede versionar. Hasta la ceremonia no hay llavero de producción: los flujos son plantillas.
