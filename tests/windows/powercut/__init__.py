"""Cortes de luz de verdad en una VM Hyper-V (PLAN-V2 §4.4 bis). Dueño: B4.

    python -m tests.windows.powercut --vm VMS-Lab --checkpoint limpio-2.0.0 --source http://10.0.0.1:8000/ \
        --target 2.0.1 [--states all] [--repeat 3] [--wait-min 10] [--out tests/e2e/RESULTADOS-powercut.json]

Se ejecuta en el PC del laboratorio (Windows 11 Pro con Hyper-V, decisión D12), como administrador, desde el
anfitrión. Para cada estado del diario y cada repetición:
1. vuelve al punto de control limpio (2.0.0 instalada con cámaras simuladas grabando);
2. lanza la actualización con `VMS_UPDATER_PAUSE_AT=<estado>` (el actualizador espera 30 s en ese estado y lo
   anuncia en `public-status.json`);
3. en cuanto lo anuncia, `Stop-VM -TurnOff` (equivale a quitar el cable: la caché de disco se pierde);
4. arranca la VM, espera hasta 10 min y comprueba: hay versión activa, los servicios arrancan, la grabación vuelve,
   `config.json` y `active.json` son JSON válidos y el diario termina en `good` o `rolled_back`.

No se ejecuta en CI (la VM no existe en GitHub): requisito antes del piloto (decisión N5). La lógica (plan, veredicto
e informe) está probada en `test_powercut.py` con un doble de Hyper-V.
"""
