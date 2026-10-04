// Cliente de la API REST del backend (docs/CONTRATO.md §6).
// - Todas las peticiones llevan «X-Requested-With: vms» (protección CSRF del backend).
// - Los errores llegan como {"error": {code, message, details}} y se lanzan como ApiError.
// - Un 401 fuera del login redirige a /login?next=<página actual>.

export class ApiError extends Error {
  constructor(status, code, message, details = {}, headers = null) {
    super(message || `Error ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "error";
    this.details = details || {};
    this.headers = headers;
  }

  /** Errores de validación por campo: [{loc: [...], msg}] */
  get fields() {
    return Array.isArray(this.details.fields) ? this.details.fields : [];
  }
}

const CSRF_HEADER = { "X-Requested-With": "vms" };
let redirecting = false;

export function loginUrl(next = location.pathname + location.search + location.hash) {
  const safe = safeNext(next);
  return safe && safe !== "/login" ? `/login?next=${encodeURIComponent(safe)}` : "/login";
}

/** Solo rutas internas relativas: evita redirecciones abiertas (//otro-sitio, https://...). */
export function safeNext(next) {
  if (typeof next !== "string" || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) {
    return "/";
  }
  return next;
}

export function redirectToLogin() {
  if (redirecting) return;
  redirecting = true;
  location.assign(loginUrl());
}

async function parseError(res) {
  let code = "http_" + res.status;
  let message = `El servidor respondió ${res.status}`;
  let details = {};
  try {
    const data = await res.json();
    if (data && data.error) {
      code = data.error.code || code;
      message = data.error.message || message;
      details = data.error.details || {};
    } else if (data && typeof data.detail === "string") {
      message = data.detail;
    }
  } catch {
    // respuesta sin JSON (p. ej. un proxy intermedio): se queda el mensaje genérico
  }
  if (res.status === 429) {
    const retry = res.headers.get("Retry-After");
    if (retry) details = { ...details, retry_after: Number(retry) };
  }
  return new ApiError(res.status, code, message, details, res.headers);
}

/**
 * Petición JSON a la API.
 * @param {string} method
 * @param {string} path  ruta que empieza por /api
 * @param {object} [body]
 * @param {{signal?: AbortSignal, auth?: boolean, raw?: boolean, timeoutMs?: number}} [opts]
 */
export async function api(method, path, body, opts = {}) {
  const headers = { ...CSRF_HEADER, Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const ctrl = new AbortController();
  const timeoutMs = opts.timeoutMs ?? 30000;
  const timer = setTimeout(() => ctrl.abort(new DOMException("Tiempo de espera agotado", "TimeoutError")), timeoutMs);
  if (opts.signal) {
    if (opts.signal.aborted) ctrl.abort(opts.signal.reason);
    else opts.signal.addEventListener("abort", () => ctrl.abort(opts.signal.reason), { once: true });
  }
  init.signal = ctrl.signal;
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err && err.name === "AbortError" && opts.signal && opts.signal.aborted) throw err;
    const msg = err && err.name === "TimeoutError"
      ? "El servidor tardó demasiado en responder."
      : "No hay conexión con el servidor del VMS.";
    throw new ApiError(0, "network_error", msg);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401 && opts.auth !== false) {
    redirectToLogin();
    throw await parseError(res);
  }
  if (!res.ok) throw await parseError(res);
  if (opts.raw) return res;
  if (res.status === 204) return null;
  const type = res.headers.get("Content-Type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

export const get = (path, opts) => api("GET", path, undefined, opts);
export const post = (path, body, opts) => api("POST", path, body ?? {}, opts);
export const put = (path, body, opts) => api("PUT", path, body, opts);
export const patch = (path, body, opts) => api("PATCH", path, body, opts);
export const del = (path, opts) => api("DELETE", path, undefined, opts);

/** Sesión actual o redirección al login. Devuelve {username, role, kiosk}. */
export async function requireSession() {
  return get("/api/auth/me");
}

export async function logout() {
  try {
    await post("/api/auth/logout", undefined, { auth: false });
  } catch (err) {
    console.warn("Fallo al cerrar sesión en el servidor", err);
  }
  location.assign("/login");
}

/** Ids del contrato (§3.4): se validan antes de meterlos en una URL. */
export function isId(value) {
  return typeof value === "string" && /^[a-z0-9][a-z0-9-]{2,39}$/.test(value);
}

export function enc(value) {
  return encodeURIComponent(String(value));
}

/**
 * Suscripción a /api/events (SSE). EventSource se reconecta solo; aquí además se vigila
 * que lleguen pings y, si el flujo se queda mudo, se recrea.
 */
export function subscribeEvents(handlers, { staleMs = 45000 } = {}) {
  let es = null;
  let lastSeen = Date.now();
  let closed = false;
  let watchdog = null;

  const touch = () => { lastSeen = Date.now(); };
  const open = () => {
    if (closed) return;
    if (es) es.close();
    es = new EventSource("/api/events", { withCredentials: true });
    touch();
    es.onopen = () => { touch(); handlers.onOpen?.(); };
    es.onmessage = touch; // pings como comentario no disparan eventos; los datos sí
    es.onerror = () => { handlers.onError?.(); };
    for (const name of ["config", "status"]) {
      es.addEventListener(name, (ev) => {
        touch();
        let data = null;
        try { data = JSON.parse(ev.data); } catch (err) { console.warn("Evento SSE no válido", name, err); return; }
        handlers[name]?.(data);
      });
    }
  };
  open();
  // los eventos «status» llegan cada 5 s: si en staleMs no llega nada, la conexión está muerta
  watchdog = setInterval(() => {
    if (Date.now() - lastSeen > staleMs) {
      console.warn("Sin eventos del servidor; se reabre la conexión SSE");
      open();
    }
  }, Math.min(15000, staleMs));
  return {
    close() {
      closed = true;
      clearInterval(watchdog);
      if (es) es.close();
      es = null;
    },
  };
}
