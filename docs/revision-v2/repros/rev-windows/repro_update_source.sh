#!/bin/bash
# H: el instalador escribe VMS_UPDATE_SOURCE en <datos>\.env (events.pas:345); vmsctl run (B1) no pasa el .env al
# proceso y vms_updater (B4) solo mira os.environ (service.py:73). Además, nadie instala updater\trusted\*\1.root.json.
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "$HERE/src.XXXX"); SLOT="$T/pf/updater/slot-a"; PD="$T/pd"
mkdir -p "$SLOT/runtime/bin" "$PD"
cp "$HERE/target/debug/vmsctl" "$SLOT/vmsctl"
# «python» de la ranura: anota las variables que recibe y sale
cat > "$SLOT/runtime/bin/python3" <<EOS
#!/bin/sh
echo "argv: \$*" > "$T/child-env.txt"
echo "VMS_UPDATE_SOURCE=[\${VMS_UPDATE_SOURCE}]" >> "$T/child-env.txt"
echo "VMS_DATA_DIR=[\${VMS_DATA_DIR}]" >> "$T/child-env.txt"
EOS
chmod +x "$SLOT/runtime/bin/python3"
printf 'VMS_ENGINE_MODE=attach\nVMS_UPDATE_SOURCE=https://updates.ejemplo/cliente/\n' > "$PD/.env"
"$SLOT/vmsctl" run --service VMSUpdater --data-dir "$PD" --exit-on-crash >/dev/null 2>&1
echo "== entorno que recibe python -m vms_updater (lanzado por el vmsctl real) =="; cat "$T/child-env.txt"
echo "== root de confianza en el payload que copia el instalador =="
find "$HERE"/tmp*/out/payload -path '*trusted*' | head -3; echo "(fin de la lista)"
echo "== qué hace el actualizador con ese entorno =="
cd /Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion/updater && VMS_DATA_DIR="$PD" VMS_INSTALL_DIR="$T/pf" \
  /Users/mauriciosas/Documents/vms-multimarca/.venv/bin/python -c "
from vms_updater.service import build_engine
e = build_engine(); print(e.check())"
echo "== y si el .env sí llegara: =="
VMS_DATA_DIR="$PD" VMS_INSTALL_DIR="$T/pf" VMS_UPDATE_SOURCE=https://updates.ejemplo/cliente/ \
  /Users/mauriciosas/Documents/vms-multimarca/.venv/bin/python -c "
from vms_updater.service import build_engine
build_engine()" 2>&1 | tail -1
