// Lector WebRTC por WHEP (RFC 9725) para el proxy del backend (/api/live/{id}/{stream}/whep).
//
// Pensado para muros que funcionan días sin intervención:
//  - una RTCPeerConnection por intento; al fallar se cierra, se libera el <video> y se avisa
//    al servidor (DELETE de la sesión) antes de reintentar con espera creciente (1, 2, 5, 10, 30 s);
//  - vigilancia activa (cada segundo): si dejan de decodificarse imágenes durante el tiempo que equivale a
//    ~30 fotogramas de esa cámara (entre minStallMs y stallMs: 3 s a 10 fps, 12 s a 1 fps), se reconecta
//    aunque el navegador crea que la conexión sigue viva;
//  - cada intento tiene un número de generación: las respuestas o eventos de un intento viejo
//    se ignoran, así no se acumulan conexiones fantasma.
//
// Estados (onState): "connecting" | "live" | "reconnecting" | "offline" | "stopped".
// "offline" = el servidor dice que no hay vídeo en esa ruta (cámara sin señal).
//
// Reconexión rápida tras una caída del motor (PLAN-V2 §5, pendiente 4a; objetivo: vídeo en ≤ 6 s):
//  - el muro avisa con noteEngineEvent() cuando llega el evento SSE «engine» (o el estado dice que el motor
//    cayó o volvió); durante ENGINE_RECOVERY_MS la espera entre intentos tiene un tope de FAST_RETRY_MS;
//  - un 503/502 del proxy (motor caído) también abre esa ventana y usa el tope;
//  - restart() reconecta ya, aunque el lector creyera seguir en vivo (las conexiones al motor anterior
//    están muertas);
//  - una conexión WebRTC «disconnected» más de disconnectGraceMs se da por perdida, sin esperar a que el
//    navegador la declare «failed» (eso tarda unos 30 s).

import { apiHeaders, redirectToLogin } from "./api.js";

const DEFAULT_BACKOFF = [1000, 2000, 5000, 10000, 30000];
export const FAST_RETRY_MS = 2000;
export const ENGINE_RECOVERY_MS = 60000;
let engineRecoveryUntil = 0;

/** El motor de vídeo se reinició, cayó o volvió: los reintentos de los próximos 60 s van con tope de 2 s. */
export function noteEngineEvent(now = performance.now()) {
  engineRecoveryUntil = Math.max(engineRecoveryUntil, now + ENGINE_RECOVERY_MS);
}

export function engineRecovering(now = performance.now()) {
  return now < engineRecoveryUntil;
}

/** Tiempo sin fotogramas nuevos que se considera vídeo parado, según los fps medidos (función pura). */
export function stallLimit(fps, minMs = 3000, maxMs = 12000) {
  if (!(fps > 0)) return maxMs;
  return Math.min(maxMs, Math.max(minMs, Math.round((30 / fps) * 1000)));
}

/** Espera antes del siguiente intento (función pura, la usan las pruebas). */
export function retryDelay(backoff, attempt, { status = 0, recovering = false, fastMs = FAST_RETRY_MS } = {}) {
  const base = backoff[Math.min(attempt, backoff.length - 1)];
  const engineDown = status === 503 || status === 502;
  return engineDown || recovering ? Math.min(base, fastMs) : base;
}

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

export class WhepHttpError extends Error {
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
    this.minStallMs = opts.minStallMs ?? 3000;
    this.watchMs = opts.watchMs ?? 1000;
    this.fps = 0;
    this.connectTimeoutMs = opts.connectTimeoutMs ?? 20000;
    this.gatherMs = opts.gatherMs ?? 1200;
    this.disconnectGraceMs = opts.disconnectGraceMs ?? 2500;
    this.fastRetryMs = opts.fastRetryMs ?? FAST_RETRY_MS;
    this.state = "stopped";
    this.attempt = 0;
    this.gen = 0;
    this.pc = null;
    this.sessionUrl = null;
    this.retryTimer = null;
    this.watchTimer = null;
    this.connectTimer = null;
    this.disconnectTimer = null;
    this.liveSince = 0;
    this.lastFrames = -1;
    this.lastProgressAt = 0;
    this.iceServers = null;
    this.lastError = "";
    this.stats = { attempts: 0, reconnects: 0, restarts: 0 };
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

  /**
   * Reconecta ya, aunque el lector esté «en vivo»: el motor se reinició y sus conexiones ya no sirven.
   * `delayMs` reparte en el tiempo las reconexiones de un muro con muchas celdas.
   */
  restart(delayMs = 0) {
    if (this.state === "stopped") return;
    this.gen++;
    this._teardown();
    this.attempt = 0;
    this.stats.restarts++;
    this._setState("reconnecting", { force: true, retryInMs: delayMs });
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._connect();
    }, delayMs);
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
        if (s === "failed" || s === "closed") {
          this._fail(gen, `conexión WebRTC ${s}`);
        } else if (s === "disconnected") {
          // sin paquetes del motor: si no se recupera enseguida, se reconecta sin esperar a «failed»
          clearTimeout(this.disconnectTimer);
          this.disconnectTimer = setTimeout(() => {
            if (gen === this.gen && pc.connectionState === "disconnected") this._fail(gen, "conexión WebRTC perdida");
          }, this.disconnectGraceMs);
        } else {
          clearTimeout(this.disconnectTimer);
        }
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitIceGathering(pc, this.gatherMs);
      if (gen !== this.gen) return;

      const res = await fetch(this.url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: apiHeaders({ "Content-Type": "application/sdp" }),
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
      this.watchTimer = setInterval(() => this._watch(gen), this.watchMs);
    } catch (err) {
      if (gen !== this.gen) return;
      const status = err instanceof WhepHttpError ? err.status : 0;
      const offline = status === 404 || status === 503 || status === 502;
      if (status === 503 || status === 502) noteEngineEvent();
      this._fail(gen, err && err.message ? err.message : String(err), offline, status);
    }
  }

  async _fetchIceServers(gen) {
    try {
      const res = await fetch(this.url, { method: "OPTIONS", credentials: "same-origin", cache: "no-store",
        headers: apiHeaders() });
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
      if (this.lastFrames > 0 && now > this.lastProgressAt) {
        const inst = ((frames - this.lastFrames) * 1000) / (now - this.lastProgressAt);
        this.fps = this.fps ? this.fps * 0.7 + inst * 0.3 : inst;
      }
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
    if (this.state === "live" && now - this.lastProgressAt > stallLimit(this.fps, this.minStallMs, this.stallMs)) {
      this._fail(gen, "el vídeo se detuvo");
    }
  }

  _fail(gen, reason, offline = false, status = 0) {
    if (gen !== this.gen) return;
    this.gen++;                       // invalida callbacks pendientes del intento fallido
    this.lastError = reason;
    if (this.state === "live") {
      this.stats.reconnects++;
      // se cortó algo que funcionaba: el primer reintento va con la espera corta (si vuelve a fallar, crece)
      if (performance.now() - this.liveSince > 5000) this.attempt = 0;
    }
    this._teardown();
    const delay = retryDelay(this.backoff, this.attempt,
      { status, recovering: engineRecovering(), fastMs: this.fastRetryMs });
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
    clearTimeout(this.disconnectTimer);
    this.disconnectTimer = null;
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
      headers: apiHeaders() }).catch((err) => {
      console.warn("No se pudo cerrar la sesión WHEP en el servidor", err);
    });
  }
}
