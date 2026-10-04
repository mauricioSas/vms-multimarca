# Prueba S1 en tu PC Windows: 3 pasos

**Qué mide:** si el visor de la v2 (Tauri con WebView2) decodifica por hardware 4 ventanas × 16 cámaras
(H.264 640x360 a 15 fps) sin cargar la CPU. De esto depende seguir con Tauri o pasar al plan B (Electron).
Dura unos **35 minutos**. No instala nada: todo queda en `%LOCALAPPDATA%\vms-s1` y en la carpeta del kit.

**Antes de empezar:** usa el PC del laboratorio con sus monitores conectados (si tienes 4, mejor) y con
los controladores de la GPU al día. Cierra los navegadores y los programas pesados. Necesita Internet
solo al principio (descarga MediaMTX y un ffmpeg de pruebas, comprobando su SHA-256).

1. **Descarga y descomprime el kit.** En GitHub, en la ejecución más reciente del flujo «S1 kit» del
   repositorio, descarga el artefacto `s1-kit` y descomprímelo en `C:\s1` (debe quedar
   `C:\s1\s1-visor.exe`, `C:\s1\s1.ps1` y esta guía).
2. **Ejecuta la prueba.** Abre PowerShell (no hace falta que sea como administrador) y escribe:

   ```
   powershell -ExecutionPolicy Bypass -File C:\s1\s1.ps1
   ```

   Se abrirán 4 ventanas con 16 vídeos cada una. Déjalas abiertas hasta que se cierren solas (30 min) y
   después vendrán dos pruebas cortas de H.265 (unos 3 min). Si Windows pregunta por el firewall para
   `mediamtx`, puedes pulsar «Cancelar»: la prueba solo usa el propio equipo. Si SmartScreen avisa de
   `s1-visor.exe`, es porque aún no está firmado: «Más información» → «Ejecutar de todas formas».
3. **Envía el resultado.** Al terminar, la ventana de PowerShell muestra el veredicto. Envía la carpeta
   `C:\s1\resultado` (lo importante es `resumen-s1.txt` y `resultado-s1.json`).

**Aprobado si:** CPU media < 60 %, decodificación por hardware en al menos el 90 % de los flujos y menos
del 1 % de fotogramas perdidos. También sabremos si este PC reproduce H.265 por WebRTC y en `<video>`.

¿Quieres una prueba más corta para comprobar que todo funciona antes? Usa
`powershell -ExecutionPolicy Bypass -File C:\s1\s1.ps1 -Minutes 3` (no vale como veredicto).
