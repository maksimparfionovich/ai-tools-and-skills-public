/**
 * End-to-end smoke test against a real stand.
 *
 * Flow: login (whoami) -> create draft article -> read it back -> delete it.
 * Run AFTER `npm install`:
 *   npm run smoke
 * Reads credentials from .env (CMS_BASE_URL, CMS_EMAIL, CMS_PASSWORD).
 */
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "../src/config.js";
import { PayloadClient } from "../src/client.js";

// Minimal .env loader (no dependency).
function loadDotEnv(): void {
  const envPath = resolve(dirname(fileURLToPath(import.meta.url)), "..", ".env");
  if (!existsSync(envPath)) return;
  for (const line of readFileSync(envPath, "utf8").split("\n")) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/.exec(line);
    if (!m) continue;
    let val = m[2].trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    if (process.env[m[1]] === undefined) process.env[m[1]] = val;
  }
}

async function main(): Promise<void> {
  loadDotEnv();
  const cfg = loadConfig();
  const client = new PayloadClient(cfg);

  console.log(`Target: ${cfg.baseUrl}`);

  console.log("1) whoami ...");
  const me = await client.me();
  const user = (me?.user ?? me) as Record<string, any>;
  console.log(`   OK as ${user?.email} (roles: ${JSON.stringify(user?.roles)})`);

  const title = `[smoke] delete me ${new Date().toISOString()}`;
  console.log("2) create draft article ...");
  const created = await client.create(
    "articles",
    { title, status: "draft" },
    { draft: true },
  );
  const id = created?.doc?.id ?? created?.id;
  if (!id) throw new Error("No id returned from create: " + JSON.stringify(created).slice(0, 300));
  console.log(`   OK created article id=${id}`);

  console.log("3) read it back ...");
  const fetched = await client.findByID("articles", id, { draft: true });
  console.log(`   OK title="${fetched?.title}"`);

  console.log("4) delete it ...");
  await client.remove("articles", id);
  console.log("   OK deleted");

  console.log("\nSMOKE TEST PASSED ✅");
}

main().catch((e) => {
  console.error("\nSMOKE TEST FAILED ❌");
  console.error(e);
  process.exit(1);
});
