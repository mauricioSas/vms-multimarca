# `spikes/s3-tuf/`

**Dueño:** arquitecto → referencia para B4.

S3: firma TUF de `root`/`targets` con ECDSA P-256 (software o PKCS#11 con SoftHSM) y verificación con python-tuf 7 ngclient, con casos de ataque.

**Referencia:** Ejecutar: `python spikes/s3-tuf/s3_tuf.py --signer software` (o `--signer pkcs11`, como hace el job `spike-s3` de CI).
