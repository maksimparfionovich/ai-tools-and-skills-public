/**
 * Offline test of several stands at once (src/targets.ts, src/config.ts) against two fake CMS on this machine:
 *   - CMS_TARGETS is parsed from env and from config.local.json, bad values fail with a readable error;
 *   - a stand may have its own login (CMS_PASSWORD__PROD), never taken from the settings file;
 *   - `target` resolves by name or URL; an unknown or missing target is an error, not a silent default;
 *   - parallel calls to two stands log in once per stand and never carry one stand's token or cookie to the other;
 *   - the write guard is checked per stand; cms_list_targets needs no network and shows no secrets.
 *   npm test
 */
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig, loadLocalSettings, parseTargets, type LocalSettings } from "../src/config.js";
import { TargetRegistry } from "../src/targets.js";
import { startFakeCms } from "./fake-cms.js";

const NO_FILE: LocalSettings = { path: "(none)", values: {}, ignoredSecrets: [] };
const EMAIL = "t@example.com";
const PASSWORD = "pw-default-123";
const PROD_PASSWORD = "pw-prod-456";

let bad = 0;
let total = 0;
function check(name: string, ok: boolean, detail = ""): void {
  total++;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}

function errorOf(fn: () => unknown): string {
  try {
    fn();
    return "";
  } catch (e: any) {
    return String(e?.message ?? e);
  }
}

async function errorOfAsync(fn: () => Promise<unknown>): Promise<string> {
  try {
    await fn();
    return "";
  } catch (e: any) {
    return String(e?.message ?? e);
  }
}

function config(env: Record<string, string>, local: LocalSettings = NO_FILE) {
  return loadConfig({ CMS_EMAIL: EMAIL, CMS_PASSWORD: PASSWORD, ...env }, local);
}

async function main(): Promise<void> {
  // ---- m1: CMS_TARGETS ----
  const parsed = parseTargets(" QA1=https://cms-qa1.example.com/ , local=http://localhost:3000");
  check("m1 CMS_TARGETS: имя в нижнем регистре, адрес без / в конце",
    JSON.stringify(parsed) === JSON.stringify({ qa1: "https://cms-qa1.example.com", local: "http://localhost:3000" }),
    JSON.stringify(parsed));
  for (const [value, expected] of [
    ["qa1", "must look like name=https://"],
    ["q a=https://cms.example.com", "may contain only"],
    ["qa1=cms-qa1.example.com", "is not a URL"],
    ["qa1=ftp://cms.example.com", "must start with http:// or https://"],
    ["qa1=https://a.example.com,QA1=https://b.example.com", "listed twice"],
  ]) {
    const err = errorOf(() => parseTargets(value));
    check(`m1 CMS_TARGETS ошибка: ${value}`, err.includes(expected), err);
  }
  const badBase = errorOf(() => config({ CMS_BASE_URL: "cms-qa1.example.com" }));
  check("m1 CMS_BASE_URL без схемы — ошибка при старте", badBase.startsWith("CMS_BASE_URL"), badBase);

  // ---- m1, m2: settings file ----
  const dir = mkdtempSync(join(tmpdir(), "payload-cms-mcp-"));
  try {
    const file = join(dir, "config.local.json");
    writeFileSync(file, JSON.stringify({
      CMS_TARGETS: { qa1: "https://cms-qa1.example.com", prod: "https://cms.example.com" },
      CMS_PASSWORD__PROD: "from-file",
    }));
    const local = loadLocalSettings(file);
    check("m1 файл: CMS_TARGETS объектом превращается в строку name=url",
      local.values.CMS_TARGETS === "qa1=https://cms-qa1.example.com,prod=https://cms.example.com", local.values.CMS_TARGETS);
    check("m2 файл: CMS_PASSWORD__PROD не читается, попадает в ignoredSecrets",
      !("CMS_PASSWORD__PROD" in local.values) && local.ignoredSecrets.includes("CMS_PASSWORD__PROD"),
      JSON.stringify(local.ignoredSecrets));
    const cfg = config({}, local);
    check("m2 файл: учётка прода из файла не применяется", !cfg.targetCredentials.prod,
      JSON.stringify(cfg.targetCredentials));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }

  // ---- m2: per-stand login from env ----
  const credCfg = config({
    CMS_TARGETS: "qa1=https://cms-qa1.example.com,prod-eu=https://cms-eu.example.com",
    CMS_PASSWORD__PROD_EU: PROD_PASSWORD,
  });
  check("m2 env: CMS_PASSWORD__PROD_EU задаёт только пароль prod-eu",
    credCfg.targetCredentials["prod-eu"]?.password === PROD_PASSWORD && !credCfg.targetCredentials["prod-eu"]?.email
      && !credCfg.targetCredentials.qa1,
    Object.keys(credCfg.targetCredentials).join());

  // ---- m3: resolving `target` ----
  const resolveCfg = config({ CMS_TARGETS: "qa1=https://cms-qa1.example.com,qa2=https://cms-qa2.example.com" });
  const reg = new TargetRegistry(resolveCfg);
  check("m3 имя без учёта регистра", reg.resolve(" QA1 ").name === "qa1", reg.resolve("QA1").baseUrl);
  const viaUrl = reg.resolve("https://CMS-QA2.example.com/");
  check("m3 URL стенда из списка даёт его имя", viaUrl.name === "qa2" && viaUrl.listed, JSON.stringify(viaUrl));
  check("m3 URL и имя — один и тот же клиент", reg.client("https://cms-qa2.example.com").client === reg.client("qa2").client);
  const other = reg.resolve("http://localhost:3000");
  check("m3 URL не из списка — имя = хост, свой клиент", other.name === "localhost:3000" && !other.listed,
    JSON.stringify(other));
  const unknown = errorOf(() => reg.resolve("qa9"));
  check("m3 неизвестный стенд — ошибка со списком стендов", unknown.includes('Unknown target "qa9"') && unknown.includes("qa1, qa2"),
    unknown);
  const missing = errorOf(() => reg.resolve(undefined));
  check("m3 без стенда и без CMS_BASE_URL — ошибка", missing.startsWith("No target given"), missing);
  const withDefault = new TargetRegistry(config({ CMS_TARGETS: "qa1=https://cms-qa1.example.com", CMS_BASE_URL: "https://cms-qa1.example.com" }));
  check("m3 с CMS_BASE_URL вызов без стенда идёт на него", withDefault.resolve().name === "qa1", JSON.stringify(withDefault.resolve()));

  // ---- m4, m5, m6: two fake stands side by side ----
  const a = await startFakeCms({ name: "a", mode: "jwt", email: EMAIL, password: PASSWORD, host: "127.0.0.1", loginDelayMs: 50 });
  const b = await startFakeCms({ name: "b", mode: "cookie", email: EMAIL, password: PROD_PASSWORD, host: "localhost", loginDelayMs: 50 });
  try {
    const cfg = config({
      CMS_TARGETS: `qa1=${a.baseUrl},prod=${b.baseUrl}`,
      CMS_PASSWORD__PROD: PROD_PASSWORD,
      CMS_READONLY_HOSTS: "localhost",
    });
    const targets = new TargetRegistry(cfg);

    // m6 before any call: no network, nothing logged in, no secret values
    const before = targets.list();
    check("m6 cms_list_targets без вызовов: ни одного запроса к стендам", a.hits.length + b.hits.length === 0,
      `${a.hits.length + b.hits.length} запросов`);
    check("m6 список: стенды, запрет записи, своя учётка прода",
      before.map((t) => t.name).join() === "qa1,prod"
        && before[0].writes.allowed && !before[1].writes.allowed
        && before[1].login.startsWith("own: CMS_PASSWORD__PROD") && before[0].login.startsWith("default")
        && before.every((t) => t.session === "not used yet"),
      JSON.stringify(before.map((t) => [t.name, t.writes.allowed, t.login, t.session])));

    // m4: 5 + 5 parallel reads
    const calls = [
      ...Array.from({ length: 5 }, () => targets.client("qa1").client.find("tags")),
      ...Array.from({ length: 5 }, () => targets.client("prod").client.find("tags")),
    ];
    const results = await Promise.all(calls);
    check("m4 параллельно: каждый ответ — от своего стенда",
      results.slice(0, 5).every((r) => r.stand === "a") && results.slice(5).every((r) => r.stand === "b"),
      results.map((r) => r.stand).join(""));
    check("m4 параллельно: один вход на стенд", a.logins === 1 && b.logins === 1, `a=${a.logins}, b=${b.logins}`);
    check("m4 учётка прода — свой пароль, qa1 — общий (иначе 401)", a.foreign.length === 0 && b.foreign.length === 0,
      [...a.foreign, ...b.foreign].join("; ") || "чужих учёток нет");

    // m5: write guard per stand, in one process
    a.hits.length = 0;
    b.hits.length = 0;
    const created = await targets.client("qa1").client.create("tags", { title: "t-a" });
    check("m5 запись на разрешённый стенд проходит", created?.doc?.stand === "a" && a.tags.size === 1, JSON.stringify(created));
    const blocked = await errorOfAsync(() => targets.client("prod").client.create("tags", { title: "t-b" }));
    check("m5 запись на read-only стенд блокируется до сети", blocked.startsWith("Write blocked") && b.hits.length === 0,
      `${blocked.slice(0, 70)}; запросов к prod: ${b.hits.length}`);

    // m6 after calls: sessions are shown, secrets are not
    const after = targets.list();
    const text = JSON.stringify(after);
    check("m6 после вызовов: видно, кто и как вошёл",
      after[0].session === `logged in (jwt) as ${EMAIL}` && after[1].session === `logged in (cookie) as ${EMAIL}`,
      after.map((t) => t.session).join(" | "));
    check("m6 в списке нет паролей, токенов и кук",
      !text.includes(PASSWORD) && !text.includes(PROD_PASSWORD) && !text.includes("tok-") && !text.includes("sess-"));
    check("m4 итог: ни одного запроса с чужой учёткой", a.foreign.length + b.foreign.length === 0,
      [...a.foreign, ...b.foreign].join("; "));
  } finally {
    a.close();
    b.close();
  }

  console.log(`\n${total - bad}/${total} ok`);
  process.exitCode = bad ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
