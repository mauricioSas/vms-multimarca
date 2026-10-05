"""`python -m vms.ops.evidence verify <paquete.zip | carpeta> [--key-id HEX] [--json]` (CONTRATO §18.7)."""
from __future__ import annotations

import argparse
import json
import sys

from .verify import verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vms.ops.evidence",
                                     description="Herramientas del paquete de evidencias")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="Comprueba la firma y las huellas SHA-256 de un paquete")
    v.add_argument("path", help="Archivo .zip o carpeta descomprimida")
    v.add_argument("--key-id", help="Exige que la firma sea de esta instalación (key_id en hex)")
    v.add_argument("--json", action="store_true", help="Salida en JSON")
    args = parser.parse_args(argv)
    res = verify(args.path, expect_key_id=args.key_id)
    m = res.manifest
    if args.json:
        print(json.dumps({"ok": res.ok, "export_id": m.export_id if m else None, "files": res.checked_files,
                          "key_id": m.signing_key.get("key_id") if m else None, "errors": res.errors},
                         ensure_ascii=False, indent=2))
    else:
        if m is not None:
            print(f"Paquete {m.export_id} · sede {m.site.get('name', '')} · exportado por {m.created_by} "
                  f"el {m.created_at.isoformat()}")
            print(f"Clave de firma: {m.signing_key.get('key_id', '')}")
        if res.ok:
            print(f"CORRECTO: firma válida y {res.checked_files} archivos íntegros.")
        else:
            print("NO COINCIDE:")
            for e in res.errors:
                print(f"  - {e}")
    if res.manifest is None:
        return 2
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
