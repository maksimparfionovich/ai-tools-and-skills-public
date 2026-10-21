/**
 * End-to-end test through the real MCP protocol, offline: an MCP client starts this server from
 * src/ over stdio (as Claude does) and calls its tools against two fake CMS on this machine.
 *   - cms_set_target is gone, cms_list_targets is there, `target` is required by every stand tool;
 *   - parallel calls to two stands answer from their own stand, and every answer names it (`_target`);
 *   - errors name the stand too; a call without a stand or with an unknown one is refused.
 *   npm test
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { mkdtempSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { startFakeCms } from "./fake-cms.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const EMAIL = "t@example.com";
const PASSWORD = "pw-default-123";
const PROD_PASSWORD = "pw-prod-456";
// Tools that work without a stand.
const NO_TARGET = new Set(["cms_list_targets", "cms_list_locales", "cms_schema_doctor"]);

let bad = 0;
let total = 0;
function check(name: string, ok: boolean, detail = ""): void {
  total++;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}

type CallResult = { isError: boolean; text: string; json: any };

async function main(): Promise<void> {
  const a = await startFakeCms({ name: "a", mode: "jwt", email: EMAIL, password: PASSWORD, host: "127.0.0.1", loginDelayMs: 30 });
  const b = await startFakeCms({ name: "b", mode: "cookie", email: EMAIL, password: PROD_PASSWORD, host: "localhost", loginDelayMs: 30 });
  const dir = mkdtempSync(join(tmpdir(), "payload-cms-mcp-e2e-"));
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [createRequire(import.meta.url).resolve("tsx/cli"), join(ROOT, "src", "index.ts")],
    cwd: ROOT,
    env: {
      CMS_EMAIL: EMAIL,
      CMS_PASSWORD: PASSWORD,
      CMS_PASSWORD__PROD: PROD_PASSWORD,
      CMS_TARGETS: `qa1=${a.baseUrl},prod=${b.baseUrl}`,
      CMS_READONLY_HOSTS: "localhost",
      // Not your config.local.json: a missing file means no settings from file.
      CMS_CONFIG_FILE: join(dir, "none.json"),
    },
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (c: Buffer) => (stderr += c.toString("utf8")));
  const client = new Client({ name: "mcp-e2e-test", version: "1.0.0" });

  async function call(name: string, args: Record<string, unknown>): Promise<CallResult> {
    try {
      const res: any = await client.callTool({ name, arguments: args });
      const text = String(res.content?.[0]?.text ?? "");
      let json: any = null;
      try {
        json = JSON.parse(text);
      } catch {
        // not JSON: an error message
      }
      return { isError: !!res.isError, text, json };
    } catch (e: any) {
      // Schema validation may come back as a protocol error instead of an isError result.
      return { isError: true, text: String(e?.message ?? e), json: null };
    }
  }

  try {
    await client.connect(transport);
    check("m7 сервер стартует и пишет стенды в лог", /Stands: qa1, prod; default: none/.test(stderr), stderr.trim().split("\n")[0]);

    // tool list
    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name);
    check("m7 cms_set_target убран, cms_list_targets есть", !names.includes("cms_set_target") && names.includes("cms_list_targets"),
      names.join(", "));
    const withoutTarget = tools
      .filter((t) => !NO_TARGET.has(t.name))
      .filter((t) => !((t.inputSchema.required ?? []) as string[]).includes("target"))
      .map((t) => t.name);
    check("m7 target обязателен во всех инструментах стенда", withoutTarget.length === 0 && names.length === 16,
      withoutTarget.join(", ") || `${names.length} инструментов`);

    // list without network
    const list = await call("cms_list_targets", {});
    check("m7 cms_list_targets: два стенда, по умолчанию нет, без запросов к стендам",
      list.json?.targets?.map((t: any) => t.name).join() === "qa1,prod" && list.json?.defaultTarget === null
        && a.hits.length + b.hits.length === 0,
      `${list.text.slice(0, 80)}…`);

    // parallel calls to both stands
    const [w1, w2, f1, f2] = await Promise.all([
      call("cms_whoami", { target: "qa1" }),
      call("cms_whoami", { target: "prod" }),
      call("cms_find", { target: "qa1", collection: "tags" }),
      call("cms_find", { target: "PROD", collection: "tags" }),
    ]);
    check("m7 параллельные whoami: каждый со своего стенда и с пометкой _target",
      w1.json?._target === `qa1 (${a.baseUrl})` && w1.json?.user?.stand === "a"
        && w2.json?._target === `prod (${b.baseUrl})` && w2.json?.user?.stand === "b",
      `${w1.json?._target} / ${w2.json?._target}`);
    check("m7 параллельные find: каждый со своего стенда",
      f1.json?.stand === "a" && f2.json?.stand === "b" && f1.json?._target?.startsWith("qa1 (") && f2.json?._target?.startsWith("prod ("),
      `${f1.json?.stand} / ${f2.json?.stand}`);
    check("m7 вход по одному на стенд, учётки не смешаны", a.logins === 1 && b.logins === 1 && a.foreign.length + b.foreign.length === 0,
      `a=${a.logins}, b=${b.logins}; чужих: ${a.foreign.length + b.foreign.length}`);

    // writes
    const created = await call("cms_create_tag", { target: "qa1", title: "e2e tag" });
    const id = created.json?.doc?.id;
    check("m7 создание тега на qa1: ответ с пометкой стенда", !created.isError && created.json?._target?.startsWith("qa1 (") && a.tags.size === 1,
      created.text.slice(0, 80));
    b.hits.length = 0;
    const blocked = await call("cms_create_tag", { target: "prod", title: "e2e tag" });
    check("m7 запись на read-only стенд: ошибка называет стенд, запросов нет",
      blocked.isError && blocked.text.startsWith(`[prod (${b.baseUrl})]`) && blocked.text.includes("Write blocked") && b.hits.length === 0,
      blocked.text.slice(0, 90));
    const deleted = await call("cms_delete", { target: "qa1", collection: "tags", id });
    check("m7 удаление тега на qa1", !deleted.isError && a.tags.size === 0, deleted.text.slice(0, 60));
    const notFound = await call("cms_delete", { target: "qa1", collection: "tags", id });
    check("m7 ошибка стенда называет стенд", notFound.isError && notFound.text.startsWith(`[qa1 (${a.baseUrl})] CMS error (404)`),
      notFound.text.slice(0, 80));

    // refusals
    a.hits.length = 0;
    b.hits.length = 0;
    const noTarget = await call("cms_find", { collection: "tags" });
    check("m7 вызов без target отклоняется", noTarget.isError && /target/i.test(noTarget.text) && a.hits.length + b.hits.length === 0,
      noTarget.text.replace(/\s+/g, " ").slice(0, 90));
    const unknown = await call("cms_find", { target: "qa9", collection: "tags" });
    check("m7 неизвестный стенд отклоняется со списком", unknown.isError && unknown.text.includes('Unknown target "qa9"')
      && unknown.text.includes("qa1, prod"), unknown.text.slice(0, 90));

    const after = await call("cms_list_targets", {});
    check("m7 cms_list_targets после работы: оба стенда вошли",
      after.json?.targets?.every((t: any) => String(t.session).startsWith("logged in")), JSON.stringify(after.json?.targets?.map((t: any) => t.session)));
  } finally {
    await client.close().catch(() => undefined);
    a.close();
    b.close();
    rmSync(dir, { recursive: true, force: true });
  }

  console.log(`\n${total - bad}/${total} ok`);
  process.exitCode = bad ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
