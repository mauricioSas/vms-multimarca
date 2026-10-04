// Prueba S1: M reproductores WHEP por ventana + estadísticas de decodificación cada 5 s.
// «powerEfficientDecoder» y «decoderImplementation» de getStats dicen si la decodificación es por
// hardware (D3D11/Media Foundation) o por software (FFmpeg/libavcodec/OpenH264).
"use strict";
const q = new URLSearchParams(location.search);
const W = Number(q.get("w") || 1);
const CELLS = Number(q.get("cells") || 16);
const BASE = q.get("base") || "http://127.0.0.1:8889";
const PREFIX = q.get("prefix") || "h264-";
const MINUTES = Number(q.get("minutes") || 30);
const IS_LAST = W === Number(q.get("windows") || 1);
const tauri = window.__TAURI__ && window.__TAURI__.core;
const t0 = performance.now();

async function report(obj) {
  const line = JSON.stringify({ t: new Date().toISOString(), w: W, ...obj });
  if (tauri) {
    try { await tauri.invoke("report", { line }); } catch (e) { console.error(e); }
  } else {
    console.log(line);
  }
}

const cols = Math.ceil(Math.sqrt(CELLS));
const grid = document.getElementById("grid");
grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
grid.style.gridTemplateRows = `repeat(${Math.ceil(CELLS / cols)}, 1fr)`;

const players = [];
for (let i = 0; i < CELLS; i++) {
  const path = `${PREFIX}${String(i + 1).padStart(2, "0")}`;
  const cell = document.createElement("div");
  cell.className = "cell";
  cell.innerHTML = `<video muted autoplay playsinline></video><span class="tag">${path}</span>`;
  grid.appendChild(cell);
  const p = { path, video: cell.querySelector("video"), pc: null, state: "nuevo", error: "" };
  players.push(p);
  start(p);
}

async function start(p) {
  try {
    const pc = new RTCPeerConnection();
    p.pc = pc;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (ev) => { if (ev.track.kind === "video") p.video.srcObject = ev.streams[0] || new MediaStream([ev.track]); };
    pc.onconnectionstatechange = () => { p.state = pc.connectionState; };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await new Promise((r) => {             // espera a los candidatos ICE (red local, < 1 s)
      if (pc.iceGatheringState === "complete") return r();
      const t = setTimeout(r, 1500);
      pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") { clearTimeout(t); r(); } };
    });
    const res = await fetch(`${BASE}/${p.path}/whep`, {
      method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp,
    });
    if (!res.ok) throw new Error(`WHEP ${res.status}`);
    await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
  } catch (e) {
    p.state = "error";
    p.error = String(e && e.message || e);
    setTimeout(() => start(p), 3000);
  }
}

async function sample() {
  const out = { cells: CELLS, playing: 0, decoded: 0, dropped: 0, received: 0, rendered_total: 0, rendered_dropped: 0,
                fps: [], decoders: {}, power_efficient: 0, codecs: {}, errors: {} };
  for (const p of players) {
    if (p.error) out.errors[p.error] = (out.errors[p.error] || 0) + 1;
    const qv = p.video.getVideoPlaybackQuality ? p.video.getVideoPlaybackQuality() : null;
    if (qv) { out.rendered_total += qv.totalVideoFrames; out.rendered_dropped += qv.droppedVideoFrames; }
    if (!p.pc) continue;
    let stats;
    try { stats = await p.pc.getStats(); } catch { continue; }
    const codecs = {};
    stats.forEach((s) => { if (s.type === "codec") codecs[s.id] = s.mimeType; });
    stats.forEach((s) => {
      if (s.type !== "inbound-rtp" || s.kind !== "video") return;
      if ((s.framesDecoded || 0) > 0) out.playing++;
      out.decoded += s.framesDecoded || 0;
      out.dropped += s.framesDropped || 0;
      out.received += s.framesReceived || 0;
      if (s.framesPerSecond) out.fps.push(s.framesPerSecond);
      const impl = s.decoderImplementation || "desconocido";
      out.decoders[impl] = (out.decoders[impl] || 0) + 1;
      if (s.powerEfficientDecoder) out.power_efficient++;
      const mime = codecs[s.codecId] || "?";
      out.codecs[mime] = (out.codecs[mime] || 0) + 1;
    });
  }
  out.fps_avg = out.fps.length ? out.fps.reduce((a, b) => a + b, 0) / out.fps.length : 0;
  delete out.fps;
  document.getElementById("hud").textContent =
    `Muro ${W}: ${out.playing}/${CELLS} con vídeo · ${out.fps_avg.toFixed(1)} fps · hardware ${out.power_efficient}/${CELLS} · ` +
    `${Math.round((performance.now() - t0) / 60000)}/${MINUTES} min`;
  await report({ kind: "webrtc", ...out });
}

setInterval(sample, 5000);
setTimeout(async () => {
  await sample();
  await report({ kind: "end" });
  if (IS_LAST && tauri) setTimeout(() => tauri.invoke("quit"), 3000);
}, MINUTES * 60000);
