// Sirve un repositorio TUF a través del Worker real en Miniflare (pruebas de punta a punta del actualizador).
//
//   node scripts/dev-serve.mjs --repo <carpeta online> --kv <kv.json> [--port 0]
//
// <carpeta online> = la que deja «python -m tools.release» (metadata/ y targets/); se carga en R2 bajo
// «online/». <kv.json> = el formato de «tools.release site-token --kv-file» ({"cliente:sha256": "{…}"}).
// Escribe una línea JSON {"url": "http://127.0.0.1:<puerto>/"} cuando está listo y sigue hasta que se
// cierra su entrada estándar o recibe SIGTERM.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { parseArgs } from "node:util";
import { startWorker } from "./miniflare.mjs";

const { values } = parseArgs({ options: { repo: { type: "string" }, kv: { type: "string" }, port: { type: "string" } } });
if (!values.repo) {
  console.error("Falta --repo");
  process.exit(2);
}

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else yield p;
  }
}

const w = await startWorker({ port: Number(values.port || 0) });
for (const file of walk(values.repo)) {
  const key = "online/" + relative(values.repo, file).split(sep).join("/");
  await w.bucket.put(key, readFileSync(file));
}
if (values.kv) {
  const entries = JSON.parse(readFileSync(values.kv, "utf8"));
  for (const [k, v] of Object.entries(entries)) await w.kv.put(k, String(v));
}
process.stdout.write(JSON.stringify({ url: w.url }) + "\n");

const stop = async () => {
  await w.mf.dispose();
  process.exit(0);
};
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
process.stdin.on("end", stop);
process.stdin.resume();
