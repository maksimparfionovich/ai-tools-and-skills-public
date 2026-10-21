/**
 * Live smoke test of several stands at once, through the real MCP protocol: starts this server
 * from src/ over stdio (as Claude does) and works with the given stands in parallel.
 *
 *   npm run smoke:targets -- qa1 qa2               stand names from CMS_TARGETS, or base URLs
 *   npm run smoke:targets -- qa1 qa2 --blocked prod   also check that writes to prod are refused
 *
 * Flow per stand, all stands in parallel: whoami -> create a tag -> find it by title on EVERY stand
 * (must exist only on its own) -> delete it -> get it (must be 404). For --blocked stands: a create
 * must fail with "Write blocked" (refused before any request, so nothing reaches that stand).
 * Credentials come from env or .env (CMS_EMAIL, CMS_PASSWORD, CMS_PASSWORD__<STAND>, ...);
 * stands and write rules from env or config.local.json, as for the server itself.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Minimal .env loader (no dependency), same as smoke-test.ts.
function loadDotEnv(): void {
  const envPath = join(ROOT, ".env");
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

type CallResult = { isError: boolean; text: string; json: any };

let failed = 0;
function step(stand: string, name: string, ok: boolean, detail = ""): void {
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} [${stand}] ${name}${detail ? ` — ${detail}` : ""}`);
}

async function main(): Promise<void> {
  loadDotEnv();
  const args = process.argv.slice(2);
  const blockedAt = args.indexOf("--blocked");
  const stands = (blockedAt >= 0 ? args.slice(0, blockedAt) : args).filter(Boolean);
  const blocked = blockedAt >= 0 ? args.slice(blockedAt + 1).filter(Boolean) : [];
  if (!stands.length) {
    console.error("Usage: npm run smoke:targets -- <stand> [<stand> ...] [--blocked <stand> ...]");
    process.exitCode = 2;
    return;
  }

  // Only CMS_* and proxy settings go to the server; the SDK adds PATH and the like itself.
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined && (k.startsWith("CMS_") || /^(https?_proxy|HTTPS?_PROXY)$/.test(k))) env[k] = v;
  }
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [createRequire(import.meta.url).resolve("tsx/cli"), join(ROOT, "src", "index.ts")],
    cwd: ROOT,
    env,
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (c: Buffer) => (stderr += c.toString("utf8")));
  const client = new Client({ name: "smoke-targets", version: "1.0.0" });

  async function call(name: string, toolArgs: Record<string, unknown>): Promise<CallResult> {
    try {
      const res: any = await client.callTool({ name, arguments: toolArgs });
      const text = String(res.content?.[0]?.text ?? "");
      let json: any = null;
      try {
        json = JSON.parse(text);
      } catch {
        // an error message
      }
      return { isError: !!res.isError, text, json };
    } catch (e: any) {
      return { isError: true, text: String(e?.message ?? e), json: null };
    }
  }

  const created = new Map<string, { id: number; title: string }>();
  try {
    await client.connect(transport);
    console.log(stderr.trim().split("\n")[0]);

    const list = await call("cms_list_targets", {});
    for (const t of list.json?.targets ?? []) {
      console.log(`     ${t.name}: ${t.baseUrl} — writes ${t.writes.allowed ? "allowed" : "blocked"}; ${t.login}`);
    }

    // 1. whoami on every stand at once
    const who = await Promise.all(stands.map((s) => call("cms_whoami", { target: s })));
    const labels = new Map<string, string>();
    stands.forEach((s, i) => {
      const r = who[i];
      labels.set(s, r.json?._target ?? "");
      step(s, "whoami", !r.isError && !!r.json?._target && r.json?.baseUrl && r.json._target.includes(r.json.baseUrl),
        r.isError ? r.text.slice(0, 160) : `${r.json._target} as ${r.json.user?.email}, writes ${r.json.writes?.allowed ? "allowed" : "blocked"}`);
    });
    const distinct = new Set(labels.values());
    step("all", "every stand answered from its own address", distinct.size === stands.length, [...distinct].join(" | "));
    if (failed) return;

    // 2. create a tag on every stand at once
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const made = await Promise.all(
      stands.map((s) => {
        const title = `[smoke] multi-target ${s} ${stamp}`;
        return call("cms_create_tag", { target: s, title, slug: `smoke-multi-target-${s.replace(/[^a-z0-9]+/gi, "-")}-${stamp}`.toLowerCase() })
          .then((r) => ({ s, title, r }));
      }),
    );
    for (const { s, title, r } of made) {
      const id = r.json?.doc?.id;
      if (id) created.set(s, { id, title });
      step(s, "create tag", !r.isError && !!id && r.json?._target === labels.get(s), r.isError ? r.text.slice(0, 160) : `id ${id}`);
    }
    if (failed) return;

    // 3. each tag exists on its own stand only
    const lookups = await Promise.all(
      stands.flatMap((owner) =>
        stands.map((where) =>
          call("cms_find", { target: where, collection: "tags", where: { title: { equals: created.get(owner)!.title } }, depth: 0 })
            .then((r) => ({ owner, where, r })),
        ),
      ),
    );
    for (const { owner, where, r } of lookups) {
      const n = r.json?.totalDocs;
      const expected = owner === where ? 1 : 0;
      step(where, `tag from ${owner} ${expected ? "is here" : "is not here"}`, !r.isError && n === expected,
        r.isError ? r.text.slice(0, 160) : `totalDocs ${n}`);
    }

    // 4. blocked stands refuse writes before any request
    for (const s of blocked) {
      const r = await call("cms_create_tag", { target: s, title: `[smoke] must be blocked ${stamp}` });
      step(s, "write refused", r.isError && r.text.includes("Write blocked"), r.text.slice(0, 160));
    }
  } finally {
    // 5. clean up whatever was created, then prove it is gone
    const removed = await Promise.all(
      [...created].map(([s, { id }]) => call("cms_delete", { target: s, collection: "tags", id }).then((r) => ({ s, id, r }))),
    );
    for (const { s, id, r } of removed) step(s, `delete tag ${id}`, !r.isError, r.isError ? r.text.slice(0, 160) : "");
    const gone = await Promise.all(
      [...created].map(([s, { id }]) => call("cms_get", { target: s, collection: "tags", id, depth: 0 }).then((r) => ({ s, r }))),
    );
    for (const { s, r } of gone) step(s, "tag is gone (404)", r.isError && r.text.includes("(404)"), r.text.slice(0, 120));
    await client.close().catch(() => undefined);
  }

  console.log(failed ? `\nSMOKE FAILED: ${failed} step(s)` : "\nSMOKE PASSED");
  process.exitCode = failed ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
