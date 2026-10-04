// Lector WebRTC por WHEP (RFC 9725) para el proxy del backend (/api/live/{id}/{stream}/whep).
//
// Pensado para muros que funcionan días sin intervención:
//  - una RTCPeerConnection por intento; al fallar se cierra, se libera el <video> y se avisa
//    al servidor (DELETE de la sesión) antes de reintentar con espera creciente (1, 2, 5, 10, 30 s);
//  - vigilancia activa: si dejan de decodificarse imágenes durante `stallMs`, se reconecta
//    aunque el navegador crea que la conexión sigue viva;
//  - cada intento tiene un número de generación: las respuestas o eventos de un intento viejo
//    se ignoran, así no se acumulan conexiones fantasma.
//
// Estados (onState): "connecting" | "live" | "reconnecting" | "offline" | "stopped".
// "offline" = el servidor dice que no hay vídeo en esa ruta (cámara sin señal).

import { redirectToLogin } from "./api.js";

const DEFAULT_BACKOFF = [1000, 2000, 5000, 10000, 30000];

/** Lee las cabeceras Link rel="ice-server" (formato del WHEP de MediaMTX). */
export function parseIceLinks(header) {
  if (!header) return [];
  const servers = [];
  for (const part of header.split(/,(?=\s*<)/)) {
    const m = part.match(/<([^>]+)>/);
    if (!m || !/rel="ice-server"/i.test(part)) continue;
    const server = { urls: [m[1]] };
    const user = part.match(/username="((?:[^"\\]|\\.)*)"/i);
    const cred = part.match(/credential="((?:[^"\\]|\\.)*)"/i);
    if (user) server.username = JSON.parse(`"${user[1]}"`);
    if (cred) server.credential = JSON.parse(`"${cred[1]}"`);
    servers.push(server);
  }
  return servers;
}

function waitIceGathering(pc, timeoutMs) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", onChange);
      resolve();
    };
    const onChange = () => { if (pc.iceGatheringState === "complete") done(); };
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener("icegatheringstatechange", onChange);
  });
}

class WhepHttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export class WhepReader {
  /**
   * @param {{url: string, video: HTMLVideoElement, onState?: (state: string, info: object) => void,
   *          backoff?: number[], stallMs?: number, connectTimeoutMs?: number, gatherMs?: number}} opts
   */
  constructor(opts) {
    this.url = opts.url;
    this.video = opts.video;
    this.onState = opts.onState || (() => {});
    this.backoff = opts.backoff || DEFAULT_BACKOFF;
    this.stallMs = opts.stallMs ?? 12000;
    this.connectTimeoutMs = opts.connectTimeoutMs ?? 20000;
    this.gatherMs = opts.gatherMs ?? 1200;
    this.state = "stopped";
    this.attempt = 0;
    this.gen = 0;
    this.pc = null;
    this.sessionUrl = null;
    this.retryTimer = null;
    this.watchTimer = null;
    this.connectTimer = null;
    this.liveSince = 0;
    this.lastFrames = -1;
    this.lastProgressAt = 0;
    this.iceServers = null;
    this.lastError = "";
    this.stats = { attempts: 0, reconnects: 0 };
  }

  start() {
    if (this.state !== "stopped") return;
    this.attempt = 0;
    this._connect();
  }

  /** Reintenta ya mismo si está esperando (p. ej. el servidor avisó de que la cámara volvió). */
  kick() {
    if (this.retryTimer && (this.state === "offline" || this.state === "reconnecting")) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      this._connect();
    }
  }

  stop() {
    this.gen++;
    this._teardown();
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this._setState("stopped");
  }

  // ------------------------------------------------------------------ interno
  _setState(state, info = {}) {
    if (this.state === state && !info.force) return;
    this.state = state;
    try {
      this.onState(state, { error: this.lastError, attempt: this.attempt, ...info });
    } catch (err) {
      console.error("Error en onState del lector WHEP", err);
    }
  }

  async _connect() {
    const gen = ++this.gen;
    this._teardown();
    this.stats.attempts++;
    this._setState(this.attempt === 0 ? "connecting" : "reconnecting");
    try {
      if (this.iceServers === null) this.iceServers = await this._fetchIceServers(gen);
      if (gen !== this.gen) return;
      const pc = new RTCPeerConnection({ iceServers: this.iceServers, bundlePolicy: "max-bundle" });
      this.pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.ontrack = (ev) => {
        if (gen !== this.gen) return;
        const stream = ev.streams && ev.streams[0] ? ev.streams[0] : new MediaStream([ev.track]);
        this.video.srcObject = stream;
        const p = this.video.play();
        if (p && p.catch) p.catch((err) => console.warn("play() rechazado", err));
      };
      pc.onconnectionstatechange = () => {
        if (gen !== this.gen) return;
        const s = pc.connectionState;
        if (s === "failed" || s === "closed") this._fail(gen, `conexión WebRTC ${s}`);
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitIceGathering(pc, this.gatherMs);
      if (gen !== this.gen) return;

      const res = await fetch(this.url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/sdp", "X-Requested-With": "vms" },
        body: pc.localDescription.sdp,
      });
      if (gen !== this.gen) {
        // el intento caducó mientras llegaba la respuesta: libera la sesión que se haya creado
        const loc = res.headers.get("Location");
        if (res.status === 201 && loc) this._deleteSession(new URL(loc, location.href).href);
        return;
      }
      if (res.status === 401) {
        redirectToLogin();
        throw new WhepHttpError(401, "sesión caducada");
      }
      if (res.status !== 201) {
        let msg = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body && body.error && body.error.message) msg = body.error.message;
        } catch {
          // cuerpo no JSON: se queda el código HTTP
        }
        throw new WhepHttpError(res.status, msg);
      }
      const loc = res.headers.get("Location");
      this.sessionUrl = loc ? new URL(loc, location.href).href : null;
      const answer = await res.text();
      if (gen !== this.gen) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answer });

      this.connectTimer = setTimeout(() => {
        if (gen === this.gen && this.state !== "live") this._fail(gen, "tiempo de conexión agotado");
      }, this.connectTimeoutMs);
      this.lastFrames = -1;
      this.lastProgressAt = performance.now();
      this.watchTimer = setInterval(() => this._watch(gen), 2000);
    } catch (err) {
      if (gen !== this.gen) return;
      const offline = err instanceof WhepHttpError && (err.status === 404 || err.status === 503 || err.status === 502);
      this._fail(gen, err && err.message ? err.message : String(err), offline);
    }
  }

  async _fetchIceServers(gen) {
    try {
      const res = await fetch(this.url, { method: "OPTIONS", credentials: "same-origin", cache: "no-store",
        headers: { "X-Requested-With": "vms" } });
      if (gen !== this.gen) return [];
      if (res.status === 401) {
        redirectToLogin();
        return [];
      }
      return parseIceLinks(res.headers.get("Link"));
    } catch (err) {
      console.warn("OPTIONS WHEP sin respuesta; se sigue sin servidores ICE", err);
      return [];
    }
  }

  async _watch(gen) {
    const pc = this.pc;
    if (gen !== this.gen || !pc) return;
    let frames = -1;
    try {
      const stats = await pc.getStats();
      stats.forEach((r) => {
        if (r.type === "inbound-rtp" && (r.kind === "video" || r.mediaType === "video")) {
          frames = Math.max(frames, r.framesDecoded ?? r.framesReceived ?? 0);
        }
      });
    } catch (err) {
      console.warn("getStats falló", err);
    }
    if (gen !== this.gen) return;
    const now = performance.now();
    if (frames > this.lastFrames) {
      this.lastFrames = frames;
      this.lastProgressAt = now;
      if (frames > 0 && this.state !== "live") {
        clearTimeout(this.connectTimer);
        this.liveSince = now;
        this.lastError = "";
        this._setState("live");
      }
      // tras 30 s estable, el siguiente fallo vuelve a empezar por la espera corta
      if (this.state === "live" && now - this.liveSince > 30000) this.attempt = 0;
      return;
    }
    if (this.state === "live" && now - this.lastProgressAt > this.stallMs) {
      this._fail(gen, "el vídeo se detuvo");
    }
  }

  _fail(gen, reason, offline = false) {
    if (gen !== this.gen) return;
    this.gen++;                       // invalida callbacks pendientes del intento fallido
    this.lastError = reason;
    if (this.state === "live") this.stats.reconnects++;
    this._teardown();
    const delay = this.backoff[Math.min(this.attempt, this.backoff.length - 1)];
    this.attempt++;
    const jitter = Math.round(delay * 0.2 * Math.random());
    this._setState(offline ? "offline" : "reconnecting", { force: true, retryInMs: delay + jitter });
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._connect();
    }, delay + jitter);
  }

  _teardown() {
    clearInterval(this.watchTimer);
    clearTimeout(this.connectTimer);
    this.watchTimer = null;
    this.connectTimer = null;
    const pc = this.pc;
    this.pc = null;
    if (pc) {
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
      try {
        pc.getReceivers().forEach((r) => r.track && r.track.stop());
        pc.close();
      } catch (err) {
        console.warn("Error al cerrar la conexión WebRTC", err);
      }
    }
    if (this.video.srcObject) {
      this.video.srcObject = null;
      this.video.removeAttribute("src");
    }
    if (this.sessionUrl) {
      this._deleteSession(this.sessionUrl);
      this.sessionUrl = null;
    }
  }

  _deleteSession(url) {
    fetch(url, { method: "DELETE", credentials: "same-origin", keepalive: true,
      headers: { "X-Requested-With": "vms" } }).catch((err) => {
      console.warn("No se pudo cerrar la sesión WHEP en el servidor", err);
    });
  }
}
