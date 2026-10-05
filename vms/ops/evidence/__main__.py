"""`python -m vms.ops.evidence verify <paquete.zip | carpeta> [--key-id HEX] [--json]` (CONTRATO §18.7).

Sin `--key-id` solo se comprueba que el paquete es COHERENTE (huellas y firma con la clave que trae el propio
paquete): sale con 3 y lo dice. Para probar que lo exportó la tienda, pasa el `key_id` que muestra la central
(ficha de la tienda) o el acta original: entonces sale con 0 si todo cuadra.
"""
from __future__ import annotations

import argparse
import json
import sys

from .verify import verify

EXIT_OK, EXIT_MISMATCH, EXIT_UNREADABLE, EXIT_KEY_NOT_CHECKED = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vms.ops.evidence",
                                     description="Herramientas del paquete de evidencias")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="Comprueba la firma y las huellas SHA-256 de un paquete")
    v.add_argument("path", help="Archivo .zip o carpeta descomprimida")
    v.add_argument("--key-id", help="key_id (hex) de la instalación que exportó, tal como lo muestra la central "
                                    "o el acta original. Sin él, la clave no se comprueba (código 3)")
    v.add_argument("--json", action="store_true", help="Salida en JSON")
    args = parser.parse_args(argv)
    res = verify(args.path, expect_key_id=args.key_id)
    m = res.manifest
    if res.manifest is None:
        code = EXIT_UNREADABLE
    elif not res.ok:
        code = EXIT_MISMATCH
    else:
        code = EXIT_OK if res.key_checked else EXIT_KEY_NOT_CHECKED
    if args.json:
        print(json.dumps({"ok": res.ok, "key_checked": res.key_checked, "exit_code": code,
                          "export_id": m.export_id if m else None, "files": res.checked_files,
                          "key_id": m.signing_key.get("key_id") if m else None, "errors": res.errors},
                         ensure_ascii=False, indent=2))
    else:
        if m is not None:
            print(f"Paquete {m.export_id} · sede {m.site.get('name', '')} · exportado por {m.created_by} "
                  f"el {m.created_at.isoformat()}")
            print(f"Clave de firma (key_id): {m.signing_key.get('key_id', '')}")
        if code == EXIT_OK:
            print(f"CORRECTO: {res.checked_files} archivos íntegros y firma válida de la instalación esperada.")
        elif code == EXIT_KEY_NOT_CHECKED:
            print(f"COHERENTE, PERO LA CLAVE NO ESTÁ COMPROBADA: {res.checked_files} archivos íntegros y firma "
                  "válida con la clave que trae el propio paquete.")
            print("  Un paquete falsificado y vuelto a firmar con otra clave también daría este resultado.")
            print("  Compara el key_id de arriba con el de la tienda en la central (o el del acta original) y "
                  "repite con --key-id <key_id>.")
        else:
            print("NO COINCIDE:")
            for e in res.errors:
                print(f"  - {e}")
    return code


if __name__ == "__main__":
    sys.exit(main())
