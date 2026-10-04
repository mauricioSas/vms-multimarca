# `spikes/s1-webview2/`

**Dueño:** arquitecto → referencia para B2. **La ejecuta el usuario** en el PC Windows del laboratorio
(decisión de la noche del 5/10: no bloquea la fase 0; B2 no arranca hasta tener el veredicto).

- `app/`: visor mínimo Tauri 2.12.1 (`src-tauri/`) con las páginas `dist/s1.html` (16 reproductores WHEP
  por ventana + `getStats`: `powerEfficientDecoder`, `decoderImplementation`, fotogramas perdidos) y
  `dist/s1-file.html` (fMP4 H.265 en `<video>` servido por el `/get` de MediaMTX). `make_icons.py` genera
  los iconos que exige tauri-build (no se guardan binarios en el repositorio).
- `s1.ps1`: descarga MediaMTX y un ffmpeg de pruebas (con SHA-256), genera los clips, arranca todo, mide
  CPU, CPU de WebView2 y el motor de decodificación de vídeo de la GPU (clases WMI, no contadores
  localizados) y escribe el veredicto en `resultado\`.
- `GUIA.md`: los 3 pasos para el usuario.
- El kit (`s1-visor.exe` + script + guía) lo compila el flujo `.github/workflows/s1-kit.yml`.

**Plan B si no aprueba:** Electron (PLAN-V2 §1.1) o subflujos de menor resolución en 4×4.
