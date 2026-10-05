# Pruebas de concepto de la fase 0 (S1-S5): veredictos

> Fase 0 de la v2, 5 de octubre de 2026. Código de cada prueba en [`spikes/`](../../spikes/). Lo que dice
> «medido» se ejecutó de verdad; lo que no se pudo ejecutar está marcado. Decisiones derivadas: PLAN-V2 §9 y
> CONTRATO §13-§17.

| Prueba | Dónde | Veredicto |
|---|---|---|
| **S1** WebView2 decodifica 4×16 flujos por hardware | PC Windows del laboratorio (la ejecuta el usuario) | **Pendiente.** Kit listo (visor Tauri mínimo + `s1.ps1` + guía de 3 pasos). B2 espera |
| **S2** MediaMTX v1.21.1 con el YAML como fuente única | macOS (MacBook M1 Pro) | **Aprobada** (7/7), con un matiz sobre la retención |
| **S3** Firma TUF `root`/`targets` ECDSA P-256 + python-tuf 7 | macOS (software) y CI Ubuntu (SoftHSM2) | **Aprobada** (6/6 casos con claves software y 6/6 con PKCS#11/SoftHSM2). Falta repetirla con YubiKey cuando se compren (D4) |
| **S4** `windows-service-rs` + `vmshost` + Job Object + cuenta virtual | CI `windows-latest` | **Aprobada** (9/9 pasos en Windows real), con un hallazgo de diseño para B1 |
| **S5** Fijación del certificado del visor remoto | — | No toca en la fase 0 (va en paralelo a B2 cuando S1 apruebe) |

---

## S1 · Decodificación por hardware en WebView2 (pendiente del usuario)

**Por qué no se ejecutó:** necesita el PC Windows con GPU y 4 monitores (decisión N4 del plan: no bloquea la
fase 0; B2 no arranca sin veredicto).

**Qué hay listo** (`spikes/s1-webview2/`):
- `app/`: visor Tauri 2.12.1 mínimo. Abre N ventanas (una por monitor; si hay menos monitores, en cuadrícula
  para que ninguna quede tapada, porque una ventana tapada podría decodificar menos y daría un resultado
  optimista). Cada ventana reproduce 16 flujos WHEP y envía cada 5 s sus estadísticas de `getStats`:
  `powerEfficientDecoder` (hardware sí/no), `decoderImplementation`, fotogramas decodificados, recibidos y
  perdidos, y los perdidos al pintar (`getVideoPlaybackQuality`). Todas las ventanas comparten la carpeta
  de datos de WebView2, como el visor de la v2. Compila en macOS (`cargo check`); el `.exe` de Windows lo
  compila el flujo `s1-kit.yml`.
- `s1.ps1`: descarga MediaMTX (SHA-256 contra la publicación oficial) y un ffmpeg de pruebas (SHA-256 de
  gyan.dev), genera clips H.264 y H.265 640x360 a 15 fps, los publica en bucle **sin recomprimir** (para no
  ensuciar la medida de CPU), y mide CPU total, CPU de WebView2 y uso del motor «VideoDecode» de la GPU con
  clases WMI (no con contadores, cuyos nombres cambian con el idioma de Windows). Fase A: 4 × 16 durante
  30 min. Fase B: 4 flujos H.265 por WebRTC. Fase C: fMP4 H.265 en `<video>` desde el `/get` de MediaMTX.
- `GUIA.md`: 3 pasos (descargar el kit, ejecutar, enviar `resultado\`).
- Comprobado en macOS: sintaxis de `s1.ps1` con el analizador de PowerShell 7.6 y PSScriptAnalyzer (sin
  errores; solo avisos de estilo por `Write-Host`, que aquí es lo que se quiere).

**Criterio:** aprobada si la CPU media < 60 %, ≥ 90 % de los flujos con decodificador por hardware y < 1 %
de fotogramas perdidos (y ≥ 98 % de flujos con vídeo). Si no: Electron (plan B) o subflujos más pequeños.

---

## S2 · MediaMTX v1.21.1 con el YAML como fuente única — **APROBADA**

**Montaje** (`spikes/s2-mediamtx/s2_mediamtx.py`): un MediaMTX «origen» que hace de cámaras (exige usuario
y contraseña para leer, como un NVR) con 3 flujos de prueba, y un MediaMTX «motor» cuyas rutas viven solo en
su `mediamtx.yml` (nadie usa la API para crearlas), grabando fMP4. Resultado completo:
[`spikes/s2-mediamtx/resultado-macos-2026-10-05.json`](../../spikes/s2-mediamtx/resultado-macos-2026-10-05.json)
(ejecutado dos veces con el mismo resultado; números de la última).

| Comprobación | Resultado medido |
|---|---|
| Graba solo con el YAML | Sí: segmentos de las dos rutas sin ninguna llamada a la API |
| El renombrado atómico (temporal + `os.replace`) dispara la recarga | Sí: **0,22 s** desde el renombrado hasta tener vídeo en la ruta nueva |
| La recarga solo reinicia lo que cambió | Sí: la ruta sin cambios mantiene `readyTime` y **no abre segmento nuevo**; la ruta con origen cambiado se reinicia (segmento nuevo, hueco 0,5 s) |
| Ajuste de grabación en caliente (`recordDeleteAfter` en `pathDefaults`) | Se aplica (la API devuelve `2d`) **sin reiniciar ninguna ruta**, pero **abre un segmento nuevo en todas** (hueco 0,69-0,79 s con GOP de 1 s). Igual con `recordSegmentDuration` y con `record: false` en una sola ruta (sin reinicios) |
| Motor matado con SIGKILL y arrancado con el mismo YAML, sin backend | Vuelve a grabar las 3 rutas en **0,23 s** |
| ¿Escribe MediaMTX contraseñas en su registro? | No, ni con un 401 (`bad status code: 401 (Unauthorized)` sin URL). Ojo: la **API sí devuelve el `source` con la contraseña** |

**Veredicto:** el diseño de PLAN-V2 §2.5 vale tal cual con v1.21.1. Consecuencias (CONTRATO §13.10):
1. La retención (`recordDeleteAfter`) se queda en el YAML, para que funcione con el backend caído; cambiarla
   cuesta un hueco de ≤ 1 GOP en todas las cámaras, así que es una acción excepcional y se avisa.
2. La API de MediaMTX sigue solo en 127.0.0.1 con usuarios internos (ya lo estaba en la v1): expone las
   contraseñas de las cámaras.
3. La redacción del registro en `vmsctl` sigue siendo obligatoria como defensa, aunque MediaMTX no las
   escriba.
4. Hallazgo menor: los usuarios internos de MediaMTX no admiten `:`, `/` ni `%` en la contraseña. Los del
   producto se derivan en hexadecimal, así que no afecta.

**No probado aquí** (queda en el checklist del laboratorio, PLAN-V2 §4.7): 16 cámaras reales grabando y
recarga con una cámara 17; Windows (NTFS y antivirus con el renombrado).

---

## S3 · Firma TUF `root`/`targets` ECDSA P-256 y verificación con python-tuf 7

**Montaje** (`spikes/s3-tuf/s3_tuf.py`): `root` con 3 claves ECDSA P-256 y umbral 2, `targets` ECDSA P-256,
`snapshot`/`timestamp` ed25519; repositorio con `consistent_snapshot` servido por HTTP local; cliente
`ngclient.Updater`. Con `--signer pkcs11`, dos claves de `root` y la de `targets` viven en un token PKCS#11
(SoftHSM2 en CI; YubiKey en producción) y firman con `HSMSigner`; la tercera de `root` es software (la «de
papel»). Versiones: tuf 7.0.1, securesystemslib 1.5.1, python-pkcs11 0.10.0.

| Caso | Software (macOS, medido) | PKCS#11 con SoftHSM2 (CI Ubuntu) |
|---|---|---|
| Descarga válida y verificada | OK | igual: OK / rechazado |
| Target alterado en el servidor | Rechazado (`LengthOrHashMismatchError`) | igual: OK / rechazado |
| `targets` firmado con una clave no autorizada | Rechazado (`UnsignedMetadataError`) | igual: OK / rechazado |
| `timestamp` caducado | Rechazado (`ExpiredMetadataError`) | igual: OK / rechazado |
| Rollback (timestamp anterior al ya visto) | Rechazado (`BadVersionNumberError`) | igual: OK / rechazado |
| Rotación de `root` 1 → 2 con 2 de 3 claves viejas | El cliente sigue la cadena y deja de aceptar la clave sustituida | igual: OK / rechazado |

Hallazgo de licencia: `HSMSigner` de securesystemslib 1.5.1 usa **python-pkcs11 (MIT)**, no PyKCS11 (GPL);
leído en el código instalado. Además, como `tools/release` no se distribuye, la cadena de firma no toca lo
que llega a las tiendas.

**Ejecución en CI** (job `spike-s3`, run [37245437660](https://github.com/mauricioSas/vms-multimarca/actions/runs/37245437660),
Ubuntu, `softhsm2` de apt; repetida en [37245939158](https://github.com/mauricioSas/vms-multimarca/actions/runs/37245939158) con la clase de cada firmante en la salida: `root` = `HSMSigner`, `HSMSigner`, `CryptoSigner` (la «de papel») y `targets` = `HSMSigner`; token `vms-dev` creado y destruido con el runner): los 6 casos dan lo mismo con
`--signer software` (0,58 s) y con `--signer pkcs11` (0,66 s). Las claves ECDSA P-256 de `root` (2 de 3) y
`targets` se generaron dentro del token con python-pkcs11 y firmaron con `HSMSigner` (`CKM_ECDSA` sobre el
SHA-256 y conversión a DER); python-tuf 7 las verificó con `ecdsa-sha2-nistp256`.

**Veredicto:** el diseño de PLAN-V2 §1.6 funciona con PKCS#11. Pendiente solo lo que depende del hardware:
repetirlo con 2 YubiKey (módulo `ykcs11`, ranura PIV 9c) y medir el flujo con PIN y toque físico.

---

## S4 · Servicio de Windows: `windows-service-rs`, `vmshost`, Job Object y cuenta virtual

**Montaje** (`spikes/s4-servicio-windows/`): `vmshost-s4.exe` (windows-service 0.8.1 + windows-sys 0.61)
hospeda el servicio `VMSS4Hello` con la cuenta virtual `NT SERVICE\VMSS4Hello`; lee `state\active.json`
(si falta o está corrupto lo reconstruye con `last_good` de `journal.json`), lanza
`versions\<activa>\hola.exe` dentro de un Job Object «kill on close» y vigila la versión a prueba: 3 caídas
en 10 min o sin confirmar en el plazo → vuelve a la anterior. El puntero se escribe con
`vms_common::atomic_write` (`MoveFileExW` con `WRITE_THROUGH`). `run-s4.ps1` instala con `sc.exe`, pone ACL
por SID (el SID del servicio; sin herencia), arranca, consulta, rompe y desinstala. Lógica portable con 5
pruebas unitarias (`cargo test`, verdes en macOS) y `clippy -D warnings` limpio para
`x86_64-pc-windows-msvc`.

**Ejecución en CI** (job `spike-s4`, `windows-latest`, run
[37245939158](https://github.com/mauricioSas/vms-multimarca/actions/runs/37245939158), artefacto `spike-s4`):

| Paso de `run-s4.ps1` | Resultado |
|---|---|
| Disposición en disco; `active.json` creado desde el diario | OK |
| `sc create` con `obj= "NT SERVICE\VMSS4Hello"`, recuperación 1/5/30 s, ACL solo SYSTEM + Administradores + SID del servicio | OK |
| Arrancar y consultar: el hijo corre como `NT SERVICE\VMSS4Hello`, **dentro del Job Object** (`IsProcessInJob`) y **no puede escribir en `bin\`** | OK |
| Matar el arrancador (`Stop-Process -Force`): el hijo muere con él (kill-on-close) y el SCM relanza el servicio | OK |
| Versión rota a prueba (2.0.0): 3 caídas en ~4 s → vuelta atrás sola a 1.0.0 | OK |
| `active.json` corrupto → reconstruido con `last_good` del diario | OK |
| Versión buena sin confirmar → vuelta atrás al pasar el plazo (25 s en la prueba, 30 min en el producto) | OK |
| Versión confirmada → se queda y el diario la marca como buena | OK |
| Parar (sin procesos huérfanos) y desinstalar | OK |

La primera ejecución falló en el paso del puntero corrupto por una carrera **de la prueba** (leía el latido
anterior); el registro del servicio mostraba que el puntero sí se había reconstruido. Corregida la prueba,
9/9.

**Hallazgo de diseño** (para B1, CONTRATO §13.3): `vmshost` corre con la cuenta del servicio que hospeda;
con el diseño de la prueba, todas las cuentas virtuales necesitan permiso de modificar `state\` para poder
volver atrás. En el producto solo el actualizador (LocalSystem) y el instalador escriben `active.json`; un
servicio sin privilegios deja una petición (`state\rollback-request.json`). Además, el hijo debe crearse
suspendido (o con `PROC_THREAD_ATTRIBUTE_JOB_LIST`) para que no haya un instante fuera del job.

---

## Cómo repetirlas

```bash
.venv/bin/python spikes/s2-mediamtx/s2_mediamtx.py --out /tmp/s2.json         # macOS/Linux, ~45 s
python spikes/s3-tuf/s3_tuf.py --signer software                                # en un venv con tuf 7
# S3 con SoftHSM2 y S4 en Windows: jobs spike-s3 y spike-s4 de .github/workflows/ci.yml
# S1: kit del flujo «S1 kit» + spikes/s1-webview2/GUIA.md
```
