#!/bin/bash
# M: ¿quién entrega las directivas del panel (canal, retener, rollback, check) a VMSUpdater? ¿quién escribe updater.json?
cd /Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion
echo "== llamadas a directive_for (fuera de su definición) =="; grep -rn "directive_for(" central vms | grep -v "def directive_for" || echo "(ninguna)"
echo "== respuesta de POST /api/heartbeat =="; grep -n 'app.post("/api/heartbeat"\|return Response(status_code=204)' central/app.py | sed -n 1,3p
echo "== el agente ¿lee el cuerpo de la respuesta del latido? =="; sed -n '/async def send/,/return False$/p' central/agent.py | grep -n "r.json\|directive" || echo "(no)"
echo "== escritores de central-directive.json / updater.json (inno_app_id) =="
grep -rn "directive_file\|central-directive" --include=*.py central vms updater distribution tools | grep -v "read_directive\|def directive_file\|tests" | grep -iv "^updater/vms_updater/layout.py" || true
grep -rn "updater.json\|inno_app_id" distribution native/vmsctl/src || echo "(el instalador y vmsctl no escriben updater\\updater.json)"
