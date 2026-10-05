// Worker del repositorio de actualizaciones de VMS Multimarca (PLAN-V2 §1.7, CONTRATO §15.5). Dueño: B4.
//
// Rutas (todo GET/HEAD; cualquier otro método → 405):
//   /<cliente>/metadata/<archivo>   libre: los metadatos TUF van firmados y no son secretos (caché corta)
//   /<cliente>/targets/<ruta>       exige «Authorization: Bearer <token de sede>»
//   /metadata/… y /targets/…        igual, sin cliente en la ruta (el cliente sale del token)
//
// Token de sede: «<cliente>.<aleatorio>». En el KV SITE_TOKENS solo está su SHA-256:
//   clave «<cliente>:<sha256 hex>» → {"site": "S0042", "active": true}
// Sin token, token desconocido o revocado → 401 {"error":"unauthorized"}; token válido de OTRO cliente → 403.
// Cada descarga se registra (cliente, sede, ruta y versión) en el registro del Worker y, si existe, en
// Analytics Engine (binding DOWNLOADS). Todas las respuestas llevan cabecera Date (comprobación de reloj).
//
// Sin dependencias de terceros. El código usa solo sintaxis de TypeScript «borrable» (sin enums ni
// propiedades de parámetro) para que las pruebas puedan quitar los tipos con node:module.

export interface Env {
  BUCKET: R2Bucket;
  SITE_TOKENS: KVNamespace;
  DOWNLOADS?: AnalyticsEngineDataset;
  REPO_PREFIX?: string;               // carpeta del repositorio en R2; por defecto «online»
}

interface TokenRecord {
  site?: string;
  active?: boolean;
}

type AuthResult = { ok: true; client: string; site: string } | { ok: false; status: 401 | 403 };

const CLIENT_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
const SEGMENT_RE = /^[A-Za-z0-9._-]+$/;
const TOKEN_RE = /^Bearer ([A-Za-z0-9._~-]{16,256})$/;
const VERSIONED_META_RE = /^[0-9]+\.(root|snapshot|targets)\.json$/;
const VERSION_IN_NAME_RE = /-([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\.(?:zip|json|exe)$/;

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    try {
      return await handle(request, env, ctx);
    } catch (err) {
      console.error(JSON.stringify({ event: "error", message: err instanceof Error ? err.message : String(err) }));
      return json(500, { error: "internal" });
    }
  },
} satisfies ExportedHandler<Env>;

export async function handle(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return json(405, { error: "method_not_allowed" }, { Allow: "GET, HEAD" });
  }
  const url = new URL(request.url);
  const raw = url.pathname;
  // Nada codificado ni rutas raras: así no hay forma de colar «..» ni dobles barras
  if (raw.includes("%") || raw.includes("//") || raw.includes("\\")) return json(400, { error: "bad_path" });
  const parts = raw.split("/").filter((p) => p.length > 0);
  let client: string | null = null;
  let i = 0;
  if (parts.length > 0 && parts[0] !== "metadata" && parts[0] !== "targets") {
    client = parts[0];
    if (!CLIENT_RE.test(client)) return json(404, { error: "not_found" });
    i = 1;
  }
  const kind = parts[i];
  const rest = parts.slice(i + 1);
  if (rest.length === 0 || rest.some((s) => !SEGMENT_RE.test(s) || s === "." || s === "..")) {
    return json(404, { error: "not_found" });
  }
  const prefix = (env.REPO_PREFIX || "online").replace(/\/+$/, "");
  const path = rest.join("/");

  if (kind === "metadata") {
    if (rest.length !== 1) return json(404, { error: "not_found" });
    const cache = VERSIONED_META_RE.test(path) ? "public, max-age=31536000, immutable" : "public, max-age=60";
    return serve(request, env, `${prefix}/metadata/${path}`, cache);
  }
  if (kind === "targets") {
    const auth = await authorize(request, env, client);
    if (!auth.ok) {
      return json(auth.status, { error: auth.status === 401 ? "unauthorized" : "forbidden" },
        auth.status === 401 ? { "WWW-Authenticate": 'Bearer realm="vms-updates"' } : {});
    }
    const res = await serve(request, env, `${prefix}/targets/${path}`, "private, max-age=31536000, immutable");
    if (res.status === 200 && request.method === "GET") {
      const version = VERSION_IN_NAME_RE.exec(path)?.[1] ?? "";
      const record = { event: "download", client: auth.client, site: auth.site, target: path, version };
      console.log(JSON.stringify(record));
      if (env.DOWNLOADS) {
        ctx.waitUntil(Promise.resolve().then(() => env.DOWNLOADS?.writeDataPoint({
          blobs: [auth.client, auth.site, path, version], indexes: [auth.client],
        })));
      }
    }
    return res;
  }
  return json(404, { error: "not_found" });
}

export async function authorize(request: Request, env: Env, urlClient: string | null): Promise<AuthResult> {
  const m = TOKEN_RE.exec(request.headers.get("Authorization") || "");
  if (!m) return { ok: false, status: 401 };
  const token = m[1];
  const tokenClient = token.split(".", 1)[0];
  if (!CLIENT_RE.test(tokenClient)) return { ok: false, status: 401 };
  const stored = await env.SITE_TOKENS.get(`${tokenClient}:${await sha256Hex(token)}`);
  if (stored === null) return { ok: false, status: 401 };
  let rec: TokenRecord;
  try {
    rec = JSON.parse(stored) as TokenRecord;
  } catch {
    return { ok: false, status: 401 };
  }
  if (rec.active !== true) return { ok: false, status: 401 };
  if (urlClient !== null && urlClient !== tokenClient) return { ok: false, status: 403 };
  return { ok: true, client: tokenClient, site: String(rec.site || "") };
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function serve(request: Request, env: Env, key: string, cacheControl: string): Promise<Response> {
  const obj = request.method === "HEAD" ? await env.BUCKET.head(key) : await env.BUCKET.get(key);
  if (obj === null) return json(404, { error: "not_found" });
  const headers = baseHeaders();
  headers.set("Content-Type", contentType(key));
  headers.set("Content-Length", String(obj.size));
  headers.set("ETag", obj.httpEtag);
  headers.set("Cache-Control", cacheControl);
  const body = "body" in obj ? (obj as R2ObjectBody).body : null;
  return new Response(request.method === "HEAD" ? null : body, { status: 200, headers });
}

function contentType(key: string): string {
  if (key.endsWith(".json")) return "application/json";
  if (key.endsWith(".zip")) return "application/zip";
  return "application/octet-stream";
}

function baseHeaders(): Headers {
  const h = new Headers();
  h.set("Date", new Date().toUTCString());
  h.set("X-Content-Type-Options", "nosniff");
  return h;
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  const headers = baseHeaders();
  headers.set("Content-Type", "application/json");
  headers.set("Cache-Control", "no-store");
  for (const [k, v] of Object.entries(extra)) headers.set(k, v);
  return new Response(JSON.stringify(body), { status, headers });
}
