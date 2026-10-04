// Prueba S1 (HEVC en <video>): reproduce un fMP4 H.265 servido por el /get de MediaMTX (la misma vía
// que usa la reproducción del producto) y dice si avanza y con qué tamaño decodifica.
"use strict";
const q = new URLSearchParams(location.search);
const src = q.get("src");
const W = Number(q.get("w") || 1);
const tauri = window.__TAURI__ && window.__TAURI__.core;
const v = document.querySelector("video");
const st = document.getElementById("st");
const canPlay = {
  hvc1: v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"'),
  hev1: v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"'),
};
let lastTime = -1; let advanced = 0;
v.src = src;
async function report(obj) {
  const line = JSON.stringify({ t: new Date().toISOString(), w: W, kind: "file", ...obj });
  if (tauri) await tauri.invoke("report", { line }); else console.log(line);
}
setInterval(async () => {
  if (v.currentTime > lastTime + 0.5) advanced++;
  lastTime = v.currentTime;
  const qv = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {};
  st.textContent = `HEVC: ${v.videoWidth}x${v.videoHeight} · t=${v.currentTime.toFixed(1)} s · error=${v.error ? v.error.code : "no"}`;
  await report({ src, can_play: canPlay, width: v.videoWidth, height: v.videoHeight, current_time: v.currentTime,
                 advanced_samples: advanced, error: v.error ? `${v.error.code} ${v.error.message || ""}` : "",
                 total_frames: qv.totalVideoFrames || 0, dropped_frames: qv.droppedVideoFrames || 0 });
}, 2000);
setTimeout(() => { if (tauri) tauri.invoke("quit"); }, Number(q.get("seconds") || 40) * 1000);
