#!/bin/bash
# H2/H3/H4: órdenes que el instalador (B3, pascal/events.pas) pasa al vmsctl REAL (B1). Instalación nueva simulada.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "$HERE/inst.XXXX")
PF="$T/pf"; PD="$T/pd"
mkdir -p "$PF/bin" "$PF/versions/2.0.0/bin"
cp "$HERE/target/debug/vmsctl" "$PF/versions/2.0.0/bin/vmsctl"
cp "$HERE/target/debug/vmshost" "$PF/bin/vmshost"
cp "$HERE/target/debug/vmsctl" "$T/vmsctl-tmp"   # {tmp}\vmsctl.exe (TempVmsctl)
run() { echo "\$ vmsctl $*"; "$@"; echo "   -> código $?"; }
echo "== events.pas:135 CheckPorts (TempVmsctl, instalación nueva) =="
run "$T/vmsctl-tmp" ports check --http-port 8600 --https-port 8643 --json
echo "   (aun añadiendo --role, que el instalador no pasa:)"
run "$T/vmsctl-tmp" ports check --http-port 8600 --https-port 8643 --role store --json
echo "== events.pas:261 cerrojo del actualizador =="
run "$PF/versions/2.0.0/bin/vmsctl" update lock --owner installer --ttl 3600 --json
run "$PF/versions/2.0.0/bin/vmsctl" update lock --json
echo "== events.pas:391 version switch en instalación nueva (sin active.json ni journal.json) =="
run "$PF/versions/2.0.0/bin/vmsctl" version switch 2.0.0 --data-dir "$PD" --json
ls -la "$PD/state" 2>/dev/null || echo "   (no existe state/: no se escribió puntero)"
