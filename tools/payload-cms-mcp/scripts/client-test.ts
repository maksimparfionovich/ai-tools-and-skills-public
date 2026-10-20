/**
 * Offline test of the CMS client against a fake Payload on 127.0.0.1 (no real CMS, no internet):
 *   - login falls back from /api/auth/login (404/405) to stock /api/users/login;
 *   - CMS_LOGIN_PATH is honoured;
 *   - a write to a read-only host is refused before any request leaves the client;
 *   - config.local.json fills in settings that env leaves unset, never credentials.
 *   npm test
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig, loadLocalSettings, type LocalSettings } from "../src/config.js";
import { PayloadClient } from "../src/client.js";

type Mode = { authLoginStatus: number; customLogin?: string };

const hits: string[] = [];
let mode: Mode = { authLoginStatus: 404 };

function send(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

const server = createServer((req: IncomingMessage, res: ServerResponse) => {
  const path = (req.url ?? "").split("?")[0];
  hits.push(`${req.method} ${path}`);
  req.resume();
  req.on("end", () => {
    const authed = req.headers.authorization === "JWT tok";
    if (req.method === "POST" && path === "/api/auth/login") {
      return send(res, mode.authLoginStatus, { errors: [{ message: "Not Found" }] });
    }
    if (req.method === "POST" && (path === "/api/users/login" || path === mode.customLogin)) {
      return send(res, 200, { token: "tok", exp: Math.floor(Date.now() / 1000) + 3600, user: { email: "t@example.com" } });
    }
    if (!authed) return send(res, 401, { errors: [{ message: "Unauthorized" }] });
    if (req.method === "GET" && path === "/api/users/me") return send(res, 200, { user: { email: "t@example.com" } });
    if (req.method === "POST" && path === "/api/tags") return send(res, 201, { doc: { id: 1 } });
    return send(res, 404, { errors: [{ message: "Not Found" }] });
  });
});

const NO_FILE: LocalSettings = { path: "(none)", values: {}, ignoredSecrets: [] };

// Тесты не зависят от вашего config.local.json: файл настроек передаётся явно.
function client(env: Record<string, string>, local: LocalSettings = NO_FILE): PayloadClient {
  return new PayloadClient(loadConfig({ CMS_EMAIL: "t@example.com", CMS_PASSWORD: "x", ...env }, local));
}

let bad = 0;
function check(name: string, ok: boolean, detail = ""): void {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}

async function main(): Promise<void> {
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  // 1. /api/auth/login отвечает 404 → вход через стандартный /api/users/login
  hits.length = 0;
  const me = await client({ CMS_BASE_URL: base }).me();
  check("вход: 404 на /api/auth/login → /api/users/login", me?.user?.email === "t@example.com",
    hits.join(", "));

  // 2. то же при 405
  mode = { authLoginStatus: 405 };
  hits.length = 0;
  await client({ CMS_BASE_URL: base }).me();
  check("вход: 405 на /api/auth/login → /api/users/login", hits.includes("POST /api/users/login"), hits.join(", "));

  // 3. CMS_LOGIN_PATH — только он, без перебора
  mode = { authLoginStatus: 404, customLogin: "/api/admins/login" };
  hits.length = 0;
  await client({ CMS_BASE_URL: base, CMS_LOGIN_PATH: "/api/admins/login" }).me();
  check("вход: CMS_LOGIN_PATH используется как есть", hits[0] === "POST /api/admins/login", hits.join(", "));

  // 4. запись на localhost разрешена и доходит до сервера
  mode = { authLoginStatus: 404 };
  hits.length = 0;
  const c = client({ CMS_BASE_URL: base });
  const created = await c.create("tags", { title: "t" });
  check("запись на 127.0.0.1 разрешена", hits.includes("POST /api/tags") && !!created, hits.join(", "));

  // 5. запись на хост из CMS_READONLY_HOSTS — отказ до любого запроса (даже до входа)
  hits.length = 0;
  let err = "";
  try {
    await client({ CMS_BASE_URL: base, CMS_READONLY_HOSTS: "127.0.0.1" }).create("tags", { title: "t" });
  } catch (e: any) {
    err = String(e?.message ?? e);
  }
  check("запись на read-only хост блокируется до сети", err.startsWith("Write blocked") && hits.length === 0,
    `${err.slice(0, 60)}; запросов: ${hits.length}`);

  // 6. чтение с read-only хоста работает
  hits.length = 0;
  const me2 = await client({ CMS_BASE_URL: base, CMS_READONLY_HOSTS: "127.0.0.1" }).me();
  check("чтение с read-only хоста работает", me2?.user?.email === "t@example.com", hits.join(", "));

  // 7–10. файл настроек config.local.json: заполняет только то, чего нет в env; секреты из него не берутся
  const dir = mkdtempSync(join(tmpdir(), "payload-cms-mcp-"));
  try {
    const file = join(dir, "config.local.json");
    writeFileSync(file, JSON.stringify({
      CMS_READONLY_HOSTS: "127.0.0.1",
      CMS_LOCALES: ["en", "de"],
      CMS_PASSWORD: "from-file",
      NOT_CMS: "x",
    }));
    const local = loadLocalSettings(file);
    check("файл: секреты и чужие ключи не читаются",
      !("CMS_PASSWORD" in local.values) && !("NOT_CMS" in local.values) && local.ignoredSecrets.includes("CMS_PASSWORD"),
      JSON.stringify(local.values));
    const cfg = loadConfig({ CMS_EMAIL: "t@example.com", CMS_PASSWORD: "x" }, local);
    check("файл: значения подхватываются, массив склеивается",
      cfg.readonlyHosts.includes("127.0.0.1") && cfg.locales.join() === "en,de" && cfg.password === "x"
        && cfg.settingsFile?.keys.join() === "CMS_READONLY_HOSTS,CMS_LOCALES",
      JSON.stringify(cfg.settingsFile));
    hits.length = 0;
    let err2 = "";
    try {
      await client({ CMS_BASE_URL: base }, local).create("tags", { title: "t" });
    } catch (e: any) {
      err2 = String(e?.message ?? e);
    }
    check("файл: read-only из файла блокирует запись до сети", err2.startsWith("Write blocked") && hits.length === 0,
      `${err2.slice(0, 50)}; запросов: ${hits.length}`);
    const cfg2 = loadConfig({ CMS_EMAIL: "t@example.com", CMS_PASSWORD: "x", CMS_READONLY_HOSTS: "cms.example.com" }, local);
    check("env сильнее файла", cfg2.readonlyHosts.join() === "cms.example.com", cfg2.readonlyHosts.join());
    writeFileSync(file, "{not json");
    let broken = "";
    try {
      loadLocalSettings(file);
    } catch (e: any) {
      broken = String(e?.message ?? e);
    }
    check("битый файл — понятная ошибка", broken.startsWith("Cannot read settings file"), broken.slice(0, 60));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }

  // без process.exit(): на Windows выход с живыми сокетами роняет libuv; ждём, пока цикл опустеет
  server.closeAllConnections();
  server.close();
  console.log(`\n${11 - bad}/11 ok`);
  process.exitCode = bad ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  server.closeAllConnections();
  server.close();
  process.exitCode = 1;
});
