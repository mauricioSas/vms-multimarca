"""Captura de fixtures de un equipo real (solo lecturas, anonimizadas). Ver `vms/vendors/capture.py`.

    python -m tools.capture_device --host 192.168.1.64 --driver hikvision --user admin \
        --out tests/vendors/fixtures/hikvision/

La contraseña se pide por teclado (getpass), no se guarda ni se escribe en el registro. La lógica vive en
`vms.vendors.capture`, que va dentro del programa instalado: allí `python -m vms.vendors.capture …` hace lo mismo
sin instalar nada más.
"""
from __future__ import annotations

from vms.vendors.capture import main

if __name__ == "__main__":
    raise SystemExit(main())
