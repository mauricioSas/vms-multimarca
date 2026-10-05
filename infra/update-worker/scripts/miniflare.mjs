// Arranca el Worker en Miniflare (workerd local, el mismo motor que Cloudflare) para pruebas.
// Quita los tipos de src/worker.ts con node:module (sin compilador) y crea los bindings R2 y KV en memoria.
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Miniflare } from "miniflare";

const HERE = dirname(fileURLToPath(import.meta.url));

export function workerScript() {
  const ts = readFileSync(join(HERE, "..", "src", "worker.ts"), "utf8");
  return stripTypeScriptTypes(ts, { mode: "strip" });
}

export async function startWorker({ port = undefined, analytics = false } = {}) {
  const opts = {
    modules: true,
    script: workerScript(),
    compatibilityDate: "2026-08-01",
    r2Buckets: ["BUCKET"],
    kvNamespaces: ["SITE_TOKENS"],
    bindings: { REPO_PREFIX: "online" },
  };
  if (port !== undefined) {
    opts.host = "127.0.0.1";
    opts.port = port;
  }
  if (analytics) opts.analyticsEngineDatasets = { DOWNLOADS: { dataset: "vms_update_downloads" } };
  const mf = new Miniflare(opts);
  const url = await mf.ready;
  return {
    mf,
    url: url.toString(),
    bucket: await mf.getR2Bucket("BUCKET"),
    kv: await mf.getKVNamespace("SITE_TOKENS"),
  };
}
