# `tests/windows/`

**Dueño:** B3 (arnés, pasos 1-5 y 10-12, capturas del asistente y v1 → v2) y B4 (`test_update_*.py`, pasos 6-9,
sobre este arnés; y `powercut/`).

| Qué | Dónde | Corre en |
|---|---|---|
| Payload y zips reproducibles | `test_layout.py` | cualquier sistema |
| `tools.build` (SBOM, firma, ISCC) | `test_build_tools.py` | cualquier sistema |
| Revisión estática del instalador | `test_installer_script.py` | cualquier sistema |
| Partes portables del arnés | `test_harness.py` | cualquier sistema |
| Dobles de vmshost/vmsctl/visor | `doubles/` (`cargo test`) | cualquier sistema; los `.exe`, en Windows |
| e2e de Windows | `e2e/` con `python -m tests.windows.run_e2e` | Windows (runner de CI o VM de pruebas) |

El e2e **instala y desinstala de verdad** y borra cualquier VMS Multimarca del equipo: solo en el runner de CI o
en una VM con instantánea. Arnés para B4: `harness.py` (`run_setup`, `run_uninstall`, registro, ACL por SID,
llamadas al doble de `vmsctl`, ventanas, `Results` con el formato de `tests/e2e/results-sistema.json`) y
`e2e/conftest.py` (fixture `e2e` con el estado compartido y fixture `step` + marca `paso(clave, título)`).

**Referencia:** PLAN-V2 §4.6.
