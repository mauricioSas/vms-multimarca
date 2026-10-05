// Pruebas del Worker con Miniflare (criterio 4 de B4): metadatos públicos; targets sin token → 401;
// token revocado → 401; token de otro cliente → 403; cabecera Date siempre; rutas maliciosas rechazadas.
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { startWorker } from "../scripts/miniflare.mjs";

let w;
const sha = (s) => createHash("sha256").update(s).digest("hex");
const token = (client) => `${client}.${randomBytes(32).toString("base64url")}`;
const TOK_OK = token("covert");
const TOK_REVOKED = token("covert");
const TOK_OTHER = token("otrocliente");
const TOK_UNKNOWN = token("covert");
const ZIP = "PK\u0003\u0004 contenido de prueba";
const TARGET = "components/app/0f1e2d3c.app-2.1.0.zip";

before(async () => {
  w = await startWorker();
  await w.bucket.put("online/metadata/timestamp.json", '{"signed":{"_type":"timestamp"}}');
  await w.bucket.put("online/metadata/3.root.json", '{"signed":{"_type":"root"}}');
  await w.bucket.put(`online/targets/${TARGET}`, ZIP);
  await w.kv.put(`covert:${sha(TOK_OK)}`, JSON.stringify({ site: "S0042", active: true }));
  await w.kv.put(`covert:${sha(TOK_REVOKED)}`, JSON.stringify({ site: "S0007", active: false }));
  await w.kv.put(`otrocliente:${sha(TOK_OTHER)}`, JSON.stringify({ site: "X1", active: true }));
});

after(async () => {
  await w?.mf.dispose();
});

const get = (path, headers = {}, method = "GET") => w.mf.dispatchFetch(new URL(path, w.url), { method, headers });

test("los metadatos son públicos, con Date y caché corta (inmutable si llevan versión)", async () => {
  for (const path of ["/covert/metadata/timestamp.json", "/metadata/timestamp.json"]) {
    const r = await get(path);
    assert.equal(r.status, 200);
    assert.equal(await r.text(), '{"signed":{"_type":"timestamp"}}');
    assert.ok(r.headers.get("Date"), "falta la cabecera Date");
    assert.ok(!Number.isNaN(Date.parse(r.headers.get("Date"))));
    assert.equal(r.headers.get("Cache-Control"), "public, max-age=60");
    assert.equal(r.headers.get("Content-Type"), "application/json");
  }
  const r = await get("/covert/metadata/3.root.json");
  assert.equal(r.status, 200);
  assert.match(r.headers.get("Cache-Control"), /immutable/);
  assert.equal((await get("/covert/metadata/no-existe.json")).status, 404);
});

test("targets sin token → 401", async () => {
  const r = await get(`/covert/targets/${TARGET}`);
  assert.equal(r.status, 401);
  assert.deepEqual(await r.json(), { error: "unauthorized" });
  assert.ok(r.headers.get("Date"));
  assert.match(r.headers.get("WWW-Authenticate"), /Bearer/);
});

test("token mal formado o desconocido → 401", async () => {
  for (const auth of ["Basic dXNlcjpwYXNz", "Bearer corto", `Bearer ${TOK_UNKNOWN}`, `Bearer ${"x".repeat(300)}`,
                      `bearer ${TOK_OK}`]) {
    const r = await get(`/covert/targets/${TARGET}`, { Authorization: auth });
    assert.equal(r.status, 401, auth.slice(0, 20));
  }
});

test("token revocado → 401", async () => {
  const r = await get(`/covert/targets/${TARGET}`, { Authorization: `Bearer ${TOK_REVOKED}` });
  assert.equal(r.status, 401);
  assert.deepEqual(await r.json(), { error: "unauthorized" });
});

test("token válido de otro cliente → 403", async () => {
  const r = await get(`/covert/targets/${TARGET}`, { Authorization: `Bearer ${TOK_OTHER}` });
  assert.equal(r.status, 403);
  assert.deepEqual(await r.json(), { error: "forbidden" });
});

test("token válido → el paquete, con caché privada; HEAD sin cuerpo", async () => {
  const r = await get(`/covert/targets/${TARGET}`, { Authorization: `Bearer ${TOK_OK}` });
  assert.equal(r.status, 200);
  assert.equal(await r.text(), ZIP);
  assert.equal(r.headers.get("Content-Type"), "application/zip");
  assert.match(r.headers.get("Cache-Control"), /^private/);
  assert.ok(r.headers.get("Date"));
  const h = await get(`/covert/targets/${TARGET}`, { Authorization: `Bearer ${TOK_OK}` }, "HEAD");
  assert.equal(h.status, 200);
  assert.equal(h.headers.get("Content-Length"), String(ZIP.length));
  // sin cliente en la ruta, el cliente sale del token
  const r2 = await get(`/targets/${TARGET}`, { Authorization: `Bearer ${TOK_OK}` });
  assert.equal(r2.status, 200);
  await r2.arrayBuffer();
});

test("métodos y rutas no permitidas", async () => {
  assert.equal((await get("/covert/metadata/timestamp.json", {}, "POST")).status, 405);
  assert.equal((await get("/covert/metadata/timestamp.json", {}, "PUT")).status, 405);
  for (const p of ["/covert/targets/..%2f..%2fsecreto", "/covert//metadata/timestamp.json", "/covert/metadata/a/b.json",
                   "/Covert/metadata/timestamp.json", "/covert/otra/cosa", "/", "/covert/metadata/"]) {
    const r = await get(p, { Authorization: `Bearer ${TOK_OK}` });
    assert.ok([400, 404].includes(r.status), `${p} → ${r.status}`);
    assert.ok(r.headers.get("Date"));
  }
  const r = await get(`/covert/targets/components/app/no-existe.zip`, { Authorization: `Bearer ${TOK_OK}` });
  assert.equal(r.status, 404);
});

test("la descarga queda registrada (cliente, sede, versión) y nunca el token", async () => {
  // el registro sale por la consola de workerd: se comprueba en el código que el registro no lleva el token
  const { workerScript } = await import("../scripts/miniflare.mjs");
  const src = workerScript();
  assert.match(src, /event: "download", client: auth\.client, site: auth\.site, target: path, version/);
  assert.match(src, /res\.status === 200 && request\.method === "GET"/);
  assert.doesNotMatch(src, /console\.(log|error)\([^)]*token/i);
});
